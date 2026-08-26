"""A strict, engine-independent output contract for coding benchmarks."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


PatchSource = Literal["workspace", "unified_diff", "empty"]


@dataclass(slots=True)
class CodingRunResult:
    answer: str
    patch: str
    patch_source: PatchSource
    apply_success: bool
    apply_error: str | None = None
    tool_trace: list[dict[str, Any]] = field(default_factory=list)


_DIFF_FENCE = re.compile(r"```(?:diff|patch)\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def _git_diff(worktree: Path) -> str:
    return subprocess.check_output(["git", "diff", "--binary"], cwd=worktree, text=True)


def extract_unified_diff(answer: str) -> str | None:
    """Extract only an explicit git-style unified diff, never prose/code."""

    fenced = _DIFF_FENCE.search(answer)
    candidate = fenced.group(1).strip() if fenced else answer.strip()
    start = candidate.find("diff --git ")
    if start < 0:
        return None
    candidate = candidate[start:].rstrip() + "\n"
    if "--- a/" not in candidate or "+++ b/" not in candidate or "@@" not in candidate:
        return None
    return candidate


def finalize_coding_run(
    worktree: Path,
    answer: str,
    *,
    tool_trace: list[dict[str, Any]] | None = None,
) -> CodingRunResult:
    """Prefer real workspace changes; otherwise apply one validated diff.

    ``git apply --check`` runs before mutation.  Invalid or natural-language
    output is recorded as empty and is never repaired or guessed by Xenon.
    """

    worktree = worktree.resolve()
    workspace_patch = _git_diff(worktree)
    if workspace_patch:
        return CodingRunResult(
            answer=answer,
            patch=workspace_patch,
            patch_source="workspace",
            apply_success=True,
            tool_trace=list(tool_trace or []),
        )

    candidate = extract_unified_diff(answer)
    if candidate is None:
        return CodingRunResult(
            answer=answer,
            patch="",
            patch_source="empty",
            apply_success=False,
            apply_error="engine produced neither workspace changes nor a unified diff",
            tool_trace=list(tool_trace or []),
        )

    check = subprocess.run(
        ["git", "apply", "--check", "--whitespace=nowarn", "-"],
        cwd=worktree,
        input=candidate,
        capture_output=True,
        text=True,
    )
    if check.returncode != 0:
        return CodingRunResult(
            answer=answer,
            patch="",
            patch_source="empty",
            apply_success=False,
            apply_error=(check.stderr or check.stdout).strip(),
            tool_trace=list(tool_trace or []),
        )

    applied = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", "-"],
        cwd=worktree,
        input=candidate,
        capture_output=True,
        text=True,
    )
    if applied.returncode != 0:
        return CodingRunResult(
            answer=answer,
            patch="",
            patch_source="empty",
            apply_success=False,
            apply_error=(applied.stderr or applied.stdout).strip(),
            tool_trace=list(tool_trace or []),
        )
    return CodingRunResult(
        answer=answer,
        patch=_git_diff(worktree),
        patch_source="unified_diff",
        apply_success=True,
        tool_trace=list(tool_trace or []),
    )
