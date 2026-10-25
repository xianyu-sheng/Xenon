#!/usr/bin/env python3
"""Kernel purity guard.

The kernel must stay a semantic layer: it may know capabilities, obligations and
receipts, but not concrete tool names, legacy engine modules, or mutable context
keys.  This script is deliberately outside ``xenon/kernel`` so its own denylist
does not trip the check.

Usage:

    python scripts/check_kernel_purity.py
    python scripts/check_kernel_purity.py --kernel-dir xenon/kernel

Exit code 0 = clean, 1 = violations.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Legacy model-facing names.  A new tool may be called anything; the kernel must
# not branch on these strings.
TOOL_NAME_DENYLIST = frozenset(
    {
        "write_file",
        "edit_file",
        "append_file",
        "batch_write",
        "batch_edit",
        "apply_patch",
        "run_tests",
        "run_script",
        "read_file",
        "read_document",
        "list_files",
        "search_files",
        "create_directory",
        "diff_preview",
        "code_index",
        "ast_analyze",
        "mcp_call",
        "spawn_agent",
        "bash",
        "command",
        "git",
    }
)

FORBIDDEN_MODULE_PREFIXES = (
    "xenon.engine",
    "xenon.nodes",
    "xenon.repl",
)

FORBIDDEN_IMPORT_NAMES = frozenset(
    {
        "WRITE_TOOL_NAMES",
        "CORE_TOOL_NAMES",
        "ADAPTER_TOOL_NAMES",
        "ORCHESTRATION_TOOL_NAMES",
        "_WRITE_TOOLS",
        "_SENSITIVE_TOOLS",
        "_INSPECT_TOOLS",
        "AgentContext",
    }
)

FORBIDDEN_CONTEXT_KEYS = frozenset(
    {
        "_tool_profile",
        "_task_recipe",
        "_evidence_ledger",
        "_evidence_session_id",
    }
)

FORBIDDEN_CALLS = frozenset(
    {
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "os.system",
        "os.popen",
        "os.remove",
        "os.unlink",
        "os.rmdir",
        "shutil.rmtree",
        "Path.write_text",
        "Path.write_bytes",
        "pathlib.Path.write_text",
        "pathlib.Path.write_bytes",
    }
)

_WRITE_MODE = re.compile(r"[wax+]")


@dataclass(frozen=True, slots=True)
class Violation:
    path: Path
    line: int
    code: str
    message: str

    def render(self, root: Path | None = None) -> str:
        shown = self.path
        if root is not None:
            try:
                shown = self.path.relative_to(root)
            except ValueError:
                shown = self.path
        return f"{shown}:{self.line}: {self.code} {self.message}"


def _dotted_name(node: ast.AST) -> str:
    parts: list[str] = []
    current: ast.AST | None = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _constant_text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _check_open_mode(node: ast.Call) -> bool:
    """Return True when this is an ``open``/``Path.open`` call in write mode."""

    name = _dotted_name(node.func)
    if name not in {"open", "Path.open", "pathlib.Path.open"}:
        return False
    mode = ""
    if len(node.args) >= 2:
        mode = _constant_text(node.args[1]) or ""
    for keyword in node.keywords:
        if keyword.arg == "mode":
            mode = _constant_text(keyword.value) or ""
    return bool(_WRITE_MODE.search(mode))


def check_path(path: Path) -> list[Violation]:
    violations: list[Violation] = []
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [
            Violation(path, 0, "K000", f"cannot read source: {exc}"),
        ]

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [
            Violation(path, exc.lineno or 0, "K000", f"cannot parse source: {exc.msg}"),
        ]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name
                if module.startswith(FORBIDDEN_MODULE_PREFIXES):
                    violations.append(
                        Violation(
                            path,
                            node.lineno,
                            "K001",
                            f"forbidden legacy import: {module}",
                        )
                    )
                if alias.name in FORBIDDEN_IMPORT_NAMES:
                    violations.append(
                        Violation(
                            path,
                            node.lineno,
                            "K002",
                            f"forbidden vocabulary import: {alias.name}",
                        )
                    )

        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith(FORBIDDEN_MODULE_PREFIXES):
                violations.append(
                    Violation(
                        path,
                        node.lineno,
                        "K001",
                        f"forbidden legacy import: {module}",
                    )
                )
            for alias in node.names:
                if alias.name in FORBIDDEN_IMPORT_NAMES:
                    violations.append(
                        Violation(
                            path,
                            node.lineno,
                            "K002",
                            f"forbidden vocabulary import: {alias.name}",
                        )
                    )

        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if value in TOOL_NAME_DENYLIST:
                violations.append(
                    Violation(
                        path,
                        node.lineno,
                        "K003",
                        f"concrete tool name in kernel: {value!r}",
                    )
                )
            if value in FORBIDDEN_CONTEXT_KEYS:
                violations.append(
                    Violation(
                        path,
                        node.lineno,
                        "K004",
                        f"mutable context key in kernel: {value!r}",
                    )
                )

        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_CONTEXT_KEYS:
            violations.append(
                Violation(
                    path,
                    node.lineno,
                    "K004",
                    f"mutable context key in kernel: {node.attr!r}",
                )
            )

        elif isinstance(node, ast.Call):
            name = _dotted_name(node.func)
            if name in FORBIDDEN_CALLS:
                violations.append(
                    Violation(
                        path,
                        node.lineno,
                        "K005",
                        f"direct side effect in kernel: {name}",
                    )
                )
            elif _check_open_mode(node):
                violations.append(
                    Violation(
                        path,
                        node.lineno,
                        "K005",
                        "direct file write in kernel: open(..., write mode)",
                    )
                )

    return violations


def check_tree(root: Path) -> list[Violation]:
    if not root.exists():
        return [Violation(root, 0, "K000", "kernel directory does not exist")]
    violations: list[Violation] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        violations.extend(check_path(path))
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check xenon/kernel purity")
    parser.add_argument(
        "--kernel-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "xenon" / "kernel",
        help="kernel directory to scan (default: <repo>/xenon/kernel)",
    )
    args = parser.parse_args(argv)

    violations = check_tree(args.kernel_dir)
    for violation in violations:
        print(violation.render(args.kernel_dir.parent.parent))
    if violations:
        print(f"\n{len(violations)} kernel purity violation(s)")
        return 1
    print(f"kernel purity OK: {args.kernel_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
