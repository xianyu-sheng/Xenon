"""Structured execution evidence shared by combined engines.

Combined engines must hand state between phases without treating prose as proof.
This module derives a compact, cache-friendly record from verified tool calls and,
when available, the bound Git worktree.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from xenon.engine.tool_tracker import ToolCall, ToolExecutionTracker


MUTATING_TOOLS = frozenset({
    "write_file",
    "edit_file",
    "append_file",
    "batch_write",
    "batch_edit",
    "create_directory",
    "refactor",
})

_TEST_COMMAND = re.compile(
    r"(?:^|[;&|\s])(?:(?:python3?|py)\s+-m\s+|(?:[\w.-]+/)+)?"
    r"(?:pytest|tox|nox|unittest|go\s+test|cargo\s+test|"
    r"npm\s+(?:run\s+)?test|pnpm\s+(?:run\s+)?test|yarn\s+test|"
    r"mvn\s+(?:test|verify)|gradle\s+test|make\s+(?:test|check)|"
    r"ruff\s+check|mypy|pyright)(?:\s|$)",
    re.IGNORECASE,
)


def _call_paths(call: ToolCall) -> set[str]:
    """Extract target paths from a successful tool call without reading files."""

    params = call.params
    paths: set[str] = set()
    for key in ("file_path", "path"):
        value = params.get(key)
        if isinstance(value, str) and value:
            paths.add(value)
    for container_key in ("files", "edits"):
        values = params.get(container_key)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            value = item.get("file_path") or item.get("path")
            if isinstance(value, str) and value:
                paths.add(value)
    return paths


def _command_text(call: ToolCall) -> str:
    value = call.params.get("action") or call.params.get("command") or ""
    return value if isinstance(value, str) else ""


def _workspace_snapshot(workspace_root: Path | None) -> tuple[str, str]:
    """Return Git status and diff using read-only commands, bounded for prompts."""

    if workspace_root is None:
        return "", ""
    try:
        status = subprocess.run(
            ["git", "status", "--short"],
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        diff = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--unified=3"],
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "", ""
    if status.returncode != 0:
        return "", ""
    return status.stdout.strip(), diff.stdout.strip() if diff.returncode == 0 else ""


@dataclass(slots=True)
class ExecutionEvidence:
    """Verified phase state; natural-language engine output is deliberately absent."""

    calls: list[ToolCall] = field(default_factory=list)
    changed_files: set[str] = field(default_factory=set)
    successful_tests: list[str] = field(default_factory=list)
    failed_calls: list[ToolCall] = field(default_factory=list)
    workspace_status: str = ""
    workspace_diff: str = ""

    @classmethod
    def capture(
        cls,
        tracker: ToolExecutionTracker | None,
        workspace_root: Path | None = None,
    ) -> "ExecutionEvidence":
        calls = list(tracker.calls) if tracker is not None else []
        changed_files: set[str] = set()
        tests: list[str] = []
        failures: list[ToolCall] = []
        for call in calls:
            if not call.success:
                failures.append(call)
                continue
            if call.tool_name in MUTATING_TOOLS:
                changed_files.update(_call_paths(call))
            if call.tool_name == "command":
                command = _command_text(call)
                if _TEST_COMMAND.search(command):
                    tests.append(command)
        status, diff = _workspace_snapshot(workspace_root)
        return cls(calls, changed_files, tests, failures, status, diff)

    @property
    def mutation_count(self) -> int:
        return sum(
            1 for call in self.calls
            if call.success and call.tool_name in MUTATING_TOOLS
        )

    @property
    def has_workspace_change(self) -> bool:
        return bool(self.changed_files or self.workspace_status)

    @property
    def implementation_verified(self) -> bool:
        """A strong completion signal: this run changed state and tests passed."""

        return self.mutation_count > 0 and bool(self.successful_tests)

    def exact_call_succeeded(self, tool: Any, params: Any) -> bool:
        if not isinstance(tool, str) or not isinstance(params, dict):
            return False
        return any(
            call.success and call.tool_name == tool and call.params == params
            for call in self.calls
        )

    def render(self, *, max_diff_chars: int = 16_000) -> str:
        """Render stable facts first and volatile diff last for prompt-cache locality."""

        successful = sum(1 for call in self.calls if call.success)
        lines = [
            f"工具调用: {successful}/{len(self.calls)} 成功",
            f"状态变更工具: {self.mutation_count}",
            "变更目标: " + (", ".join(sorted(self.changed_files)) or "(无已验证目标)"),
            "成功测试: " + (
                "; ".join(command[:300] for command in self.successful_tests)
                or "(无已验证测试)"
            ),
        ]
        if self.failed_calls:
            lines.append(
                "失败工具: "
                + "; ".join(
                    f"{call.tool_name}: {(call.error or call.result_summary)[:200]}"
                    for call in self.failed_calls[-8:]
                )
            )
        if self.workspace_status:
            lines.append("工作区状态:\n" + self.workspace_status[:4_000])
        if self.workspace_diff:
            diff = self.workspace_diff
            if len(diff) > max_diff_chars:
                diff = diff[:max_diff_chars] + f"\n... [diff 截断，共 {len(self.workspace_diff)} 字符]"
            lines.append("工作区 diff（真实状态，只读证据）:\n" + diff)
        return "\n".join(lines)


def workspace_root_for(engine: Any) -> Path | None:
    runtime = getattr(engine, "tool_runtime", None)
    root = getattr(runtime, "workspace_root", None)
    return root if isinstance(root, Path) else None
