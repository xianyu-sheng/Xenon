"""Derive verifiable obligations from the task text (#420).

V2 (task-level) verification had nothing to verify: ``TaskSpec.obligations``
existed but nothing ever filled it, so global coverage was always empty.  This
module fills it deterministically from the user's own words.

What is derived, and what is deliberately **not**:

* **derived**: a filename the task names ("改 calc.py") becomes
  ``file_changed(calc.py)``; a test entry point ("跑 pytest test_calc.py")
  becomes ``test_passed(target)``.  Both are checked by the existing resolver
  against real facts — no model involvement, no judging whether the edit was
  *correct*.
* **not derived**: prose requirements ("不要删测试", "三处都要改").  There is no
  deterministic way to bind those to a fact, and inventing a check would be
  worse than absence: it would report coverage that does not exist.

This is the harness layer: it reads human text and emits kernel contracts.  The
kernel keeps matching claims to facts and never parses prose itself.
"""

from __future__ import annotations

import re

from xenon.kernel.contracts import Obligation, PropositionKind

__all__ = ["derive_obligations"]

#: Cap so a long document cannot turn into hundreds of checks.
_MAX_OBLIGATIONS = 12

#: A code/config file named in the task.  Deliberately narrow: an extension we
#: can act on, no globs, no directories.
_FILE = re.compile(
    r"(?<![\w/\\])([A-Za-z0-9_.\-/\\]+\.(?:py|ts|tsx|js|jsx|md|json|ya?ml|toml|"
    r"go|rs|java|rb|c|cpp|h|hpp|sh|cfg|ini))(?![\w])"
)

#: Test entry points.  A concrete file wins; otherwise the runner alone.
_TEST_RUNNER = re.compile(r"\b(pytest|npm\s+test|yarn\s+test|go\s+test|ctest|tox)\b")

_NON_SOURCE = re.compile(r"^(?:README|LICENSE|CHANGELOG|TASK)\.", re.IGNORECASE)


def _normalise(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def derive_obligations(objective: str, *, since_seq: int = 0) -> tuple[Obligation, ...]:
    """Verifiable obligations implied by the task text, in a stable order."""

    if not objective.strip():
        return ()

    seen: set[tuple[str, str]] = set()
    obligations: list[Obligation] = []

    def add(kind: PropositionKind, target: str) -> None:
        key = (kind.value, target)
        if key in seen or len(obligations) >= _MAX_OBLIGATIONS:
            return
        seen.add(key)
        obligations.append(
            Obligation(
                id=f"v2-{kind.value}-{len(obligations) + 1}",
                kind=kind,
                target=target,
                since_seq=since_seq,
            )
        )

    raw_files: list[str] = []
    for match in _FILE.finditer(objective):
        path = _normalise(match.group(1))
        if not path or _NON_SOURCE.match(path):
            continue
        raw_files.append(path)

    # A task often names a full path once and then refers to the same file by
    # basename ("common.py:188").  Treating the basename as a second target
    # invented an obligation nobody could satisfy: the run dogfooded on
    # SmartBench #26 finished blocked on a third "common.py" no one was asked
    # to change.  When a longer path already ends with the bare name, the
    # longer path wins.
    files: list[str] = []
    for path in raw_files:
        if any(
            other != path and other.endswith("/" + path)
            for other in raw_files
        ):
            continue
        files.append(path)

    for path in files:
        add(PropositionKind.FILE_CHANGED, path)

    runner = _TEST_RUNNER.search(objective)
    if runner:
        # Prefer a test file the task named; otherwise the runner itself.
        test_file = next(
            (path for path in files if "test" in path.lower()), ""
        )
        add(PropositionKind.TEST_PASSED, test_file or runner.group(1))

    return tuple(obligations)
