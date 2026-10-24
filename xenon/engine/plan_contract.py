"""Structured plan-step contracts.

Plan text is for humans.  Execution, verification and display should consume a
small deterministic contract instead of guessing from prose:

    kind: inspect | edit | verify | commit | clean | execute | think
    requires_write: bool
    verify_cmd: exact command to rerun after a repair
    target_paths: files this step is expected to touch

Planner output may already provide these fields.  Older plans are normalised
here once, at the planning boundary, so the executor does not need a growing
set of regexes.
"""

from __future__ import annotations

import re
from typing import Any

from xenon.engine.evidence_gate import WRITE_TOOL_NAMES

STEP_KINDS = frozenset(
    {"inspect", "edit", "verify", "commit", "clean", "execute", "think"}
)

_VERIFY_RE = re.compile(
    r"(?:pytest|py\.test|unittest|tox|nox|ruff|mypy|pyright|"
    r"npm\s+(?:test|run\s+test)|pnpm\s+test|yarn\s+test|"
    r"cargo\s+test|go\s+test|make\s+test|run_tests)",
    re.I,
)
_COMMIT_RE = re.compile(r"\bgit\s+commit\b|\bcommit\b|提交", re.I)
_CLEAN_RE = re.compile(r"\bgit\s+status\b|工作区|干净|clean|dirty", re.I)
_WRITE_TEXT_RE = re.compile(
    r"(?:write_file|edit_file|batch_write|batch_edit|"
    r"修改|写入|插入|补齐|落盘|创建文件|新增文件)",
    re.I,
)
_INSPECT_TOOLS = frozenset(
    {
        "read_file",
        "read_document",
        "list_files",
        "search_files",
        "code_index",
        "ast_analyze",
        "lsp_goto_def",
        "lsp_find_refs",
        "lsp_hover",
        "lsp_diagnostics",
        "lsp_symbols",
        "github_fetch",
        "docs_fetch",
        "web_fetch",
        "diff_preview",
    }
)


def _text(value: Any) -> str:
    return str(value or "")


def _command_from_step(step: dict[str, Any]) -> str:
    if _text(step.get("tool")).strip() == "run_tests":
        return "run_tests"
    params = step.get("params")
    if not isinstance(params, dict):
        return ""
    for key in ("command", "action", "cmd", "script"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def is_verification_command(command: str) -> bool:
    return bool(_VERIFY_RE.search(command or ""))


def infer_step_kind(step: dict[str, Any]) -> str:
    """Infer one contract kind without looking at a language-specific phrase."""
    explicit = _text(step.get("kind")).strip().lower()
    if explicit in STEP_KINDS:
        return explicit
    tool = _text(step.get("tool")).strip()
    task = _text(step.get("task") or step.get("step"))
    command = _command_from_step(step)
    if tool in WRITE_TOOL_NAMES:
        return "edit"
    if tool == "run_tests":
        return "verify"
    if tool == "git":
        if _COMMIT_RE.search(task + " " + command):
            return "commit"
        if _CLEAN_RE.search(task + " " + command):
            return "clean"
        return "inspect"
    if tool in {"command", "run_script"}:
        if is_verification_command(task + " " + command):
            return "verify"
        if _COMMIT_RE.search(task + " " + command):
            return "commit"
        if _CLEAN_RE.search(task + " " + command):
            return "clean"
        return "execute"
    if tool in _INSPECT_TOOLS:
        return "inspect"
    if _VERIFY_RE.search(task):
        return "verify"
    if _COMMIT_RE.search(task):
        return "commit"
    if _CLEAN_RE.search(task):
        return "clean"
    if _WRITE_TEXT_RE.search(task):
        return "edit"
    return "think" if not tool or tool == "null" else "execute"


def normalize_step_contract(
    step: dict[str, Any],
    *,
    plan_version: int = 1,
    origin: str = "plan",
) -> dict[str, Any]:
    """Return a planner step with stable execution fields filled in."""
    if not isinstance(step, dict):
        return {}
    out = dict(step)
    task = _text(out.get("task") or out.get("step"))
    kind = infer_step_kind(out)
    out["task"] = task
    out["step"] = task
    out["kind"] = kind
    explicit_write = out.get("requires_write")
    out["requires_write"] = (
        bool(explicit_write) if explicit_write is not None else kind == "edit"
    )
    verify_cmd = _text(out.get("verify_cmd") or "").strip()
    command = _command_from_step(out)
    if not verify_cmd and kind == "verify" and is_verification_command(command):
        verify_cmd = command
    out["verify_cmd"] = verify_cmd
    paths = out.get("target_paths")
    if isinstance(paths, str):
        paths = [paths]
    out["target_paths"] = [
        _text(path) for path in (paths or []) if _text(path).strip()
    ]
    out["plan_version"] = int(plan_version)
    out["origin"] = _text(out.get("origin") or origin)
    out.setdefault("status", "pending")
    return out


def step_requires_write(step: dict[str, Any]) -> bool:
    """Structured write requirement with a conservative legacy fallback."""
    if not isinstance(step, dict):
        return False
    if step.get("requires_write") is True:
        return True
    if _text(step.get("tool")).strip() in WRITE_TOOL_NAMES:
        return True
    return infer_step_kind(step) == "edit"


def step_verify_cmd(step: dict[str, Any]) -> str:
    if not isinstance(step, dict):
        return ""
    explicit = _text(step.get("verify_cmd")).strip()
    if explicit:
        return explicit
    if infer_step_kind(step) == "verify":
        return _command_from_step(step)
    return ""


def normalize_plan_steps(
    steps: list[dict[str, Any]],
    *,
    plan_version: int = 1,
    origin: str = "plan",
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for step in steps:
        item = normalize_step_contract(
            step,
            plan_version=plan_version,
            origin=origin,
        )
        if item and item.get("task"):
            normalized.append(item)
    return normalized
