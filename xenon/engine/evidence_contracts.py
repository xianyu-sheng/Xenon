"""Canonical low-level evidence contracts.

These names were previously copied across TaskState, EvidenceGate, the claim
gate and combined-engine evidence.  Keeping one definition prevents the copies
from drifting into different notions of "the agent wrote something".
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# Tools whose successful call can change files in the workspace.  ``command``
# and ``git`` are deliberately absent: they can be read-only (``pytest``,
# ``git status``) and their workspace effects are checked separately.
WRITE_TOOL_NAMES = frozenset(
    {
        "write_file",
        "edit_file",
        "append_file",
        "batch_write",
        "batch_edit",
        "create_directory",
        "refactor",
        "edit_with_llm",
        "clone_repo",
    }
)

# Generated caches must not make a clean worktree look dirty.
IGNORED_DIRTY_RE = re.compile(
    r"^\?\? (?:.*/)?(?:__pycache__/|\.pytest_cache/)|^\?\? .*\.pyc$"
)


def path_key(value: Any) -> str:
    """Case-insensitive, separator-normalized key for path comparisons."""
    return str(value or "").replace("\\", "/").strip().lower()


def display_path(value: Any) -> str:
    """Normalize a path for display without changing its case."""
    text = str(value or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text.rstrip("/")


def paths_match(expected: Any, actual: Any) -> bool:
    """Compare relative/absolute paths without depending on separators/case."""
    left = path_key(expected).lstrip("./").rstrip("/")
    right = path_key(actual).lstrip("./").rstrip("/")
    if not left or not right:
        return False
    return (
        left == right
        or right.endswith("/" + left)
        or left.endswith("/" + right)
    )


def is_test_path(path: Any) -> bool:
    """Whether a path belongs to a test artifact, not source delivery."""
    normalized = path_key(path)
    parts = [part for part in normalized.split("/") if part]
    if "tests" in parts or "test" in parts:
        return True
    return bool(parts and Path(parts[-1]).name.startswith("test_"))
