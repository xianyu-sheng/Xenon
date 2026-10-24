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


# ── Test-command evidence ────────────────────────────────────────────
# A command that merely starts a test runner is not proof that tests ran.
# ``pytest --version`` and ``--collect-only`` are common false positives.  The
# same helpers are shared by TaskState, ClaimGate and delivery prompts so the
# meaning of "verification" cannot drift.

_TEST_COMMAND_RE = re.compile(
    r"(?:^|[\s;&|])(?:python(?:3(?:\.\d+)?)?\s+-m\s+)?(?:pytest|unittest)\b"
    r"|\b(?:npm|pnpm|yarn)\s+(?:run\s+)?test\b"
    r"|\b(?:cargo|go)\s+test\b"
    r"|\btox\b",
    re.IGNORECASE,
)

_NO_TEST_FLAGS = re.compile(
    r"(?:^|\s)(?:--version|--help|-h|--collect-only|--co|--fixtures|"
    r"--setup-plan|--setup-show|--trace-config|--markers|--funcargs)\b",
    re.IGNORECASE,
)

_TEST_COUNT_PATTERNS = (
    re.compile(r"(?P<count>\d+)\s+passed\b", re.IGNORECASE),
    re.compile(r"(?P<count>\d+)\s+failed\b", re.IGNORECASE),
    re.compile(r"(?P<count>\d+)\s+error(?:s)?\b", re.IGNORECASE),
)


def command_is_test_like(command: Any) -> bool:
    """Whether a command text is intended to execute tests."""
    return bool(_TEST_COMMAND_RE.search(str(command or "")))


def is_non_test_invocation(command: Any) -> bool:
    """Whether a test-runner command explicitly asks for metadata, not tests."""
    text = str(command or "")
    return command_is_test_like(text) and bool(_NO_TEST_FLAGS.search(text))


def parse_test_counts(text: Any) -> dict[str, int]:
    """Extract deterministic test counts from common runner output.

    Returns ``tests_run`` as passed + failed + errors.  ``collected`` alone is
    deliberately not enough: collection proves discovery, not execution.
    """
    value = str(text or "")
    counts = {"passed": 0, "failed": 0, "errors": 0, "tests_run": 0}
    for pattern in _TEST_COUNT_PATTERNS:
        for match in pattern.finditer(value):
            key = (
                "passed"
                if "passed" in match.group(0).lower()
                else "failed"
                if "failed" in match.group(0).lower()
                else "errors"
            )
            try:
                counts[key] = max(counts[key], int(match.group("count")))
            except (TypeError, ValueError):
                continue
    # unittest prints ``Ran 3 tests`` rather than an N-passed summary.
    ran = re.search(r"\bRan\s+(\d+)\s+tests?\b", value, re.IGNORECASE)
    if ran:
        try:
            counts["tests_run"] = max(counts["tests_run"], int(ran.group(1)))
        except (TypeError, ValueError):
            pass
    counts["tests_run"] = max(
        counts["tests_run"],
        counts["passed"] + counts["failed"] + counts["errors"],
    )
    return counts


def tests_run_from_metadata(
    metadata: dict[str, Any] | None, text: Any = ""
) -> int:
    """Read a structured test count, falling back to output parsing."""
    meta = metadata if isinstance(metadata, dict) else {}
    for key in ("tests_run", "test_count"):
        value = meta.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return max(0, value)
    return parse_test_counts(text)["tests_run"]
