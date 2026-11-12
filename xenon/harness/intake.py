"""Derive verifiable obligations from the task text (#420).

V2 (task-level) verification had nothing to verify: ``TaskSpec.obligations``
existed but nothing ever filled it, so global coverage was always empty.  This
module fills it deterministically from the user's own words.

What is derived, and what is deliberately **not**:

* **derived**: a filename the task asks you to *change* ("改 calc.py") becomes
  ``file_changed(calc.py)``; a test entry point ("跑 pytest test_calc.py") or a
  file the task asks to *pass* ("让 test_x.py 通过") becomes
  ``test_passed(target)``.  Both are checked by the existing resolver against
  real facts — no model involvement, no judging whether the edit was *correct*.
* **not derived**: prose requirements ("不要删测试", "三处都要改").  There is no
  deterministic way to bind those to a fact, and inventing a check would be
  worse than absence: it would report coverage that does not exist.

This is the harness layer: it reads human text and emits kernel contracts.  The
kernel keeps matching claims to facts and never parses prose itself.

**Classification, not pattern matching.**  Matching a filename and demanding it
change is wrong whenever the file was merely named.  A real task — "Fix it so
test_calc.py passes" — produced ``file_changed(test_calc.py)``; editing a test
file is the opposite of the ask, so a correct run that only touched the source
file was refused.  An unanswerable-but-wrong contract is worse than no contract:
it manufactures false assurance.

So every filename is classified by what the surrounding words ask of it, using
the same read/write split the permission layer already enforces (chapter 8 of
the architecture doc) rather than a second vocabulary:

* **write**  — the file is what you must change  -> ``file_changed``
* **verify** — the file is what must pass        -> ``test_passed``
* **read**   — the file is only looked at        -> no obligation
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

#: Words that ask for a change.  English verbs are matched on word boundaries
#: so ``create`` does not fire inside ``created_at`` and ``read`` does not fire
#: inside ``readme``.
_WRITE_EN = re.compile(
    r"\b(?:fix|fixes|fixed|fixing|update|updates|updated|editing|edit|edits|"
    r"modify|modifies|modified|change|changes|changed|implement|implements|"
    r"implemented|add|adds|added|create|creates|created|remove|removes|removed|"
    r"delete|deletes|deleted|rename|renames|renamed|refactor|refactors|"
    r"refactored|replace|replaces|replaced|write|writes|wrote|patch|patches|"
    r"adjust|adjusts|adjusted)\b"
    r"|needs?\s+(?:a\s+)?(?:work|fix|update|change|edit)"
    r"|\bwork\s+on\b"
)
_WRITE_ZH = ("修改", "修", "改", "修复", "修正", "更新", "实现", "添加", "新增",
             "删除", "移除", "重命名", "重构", "替换", "调整", "补充",
             "创建", "新建", "生成", "写入", "写")

_READ_EN = re.compile(
    r"\b(?:review|reviews|reviewed|read|reads|reading|look|looking|check|"
    r"checks|checking|analy[sz]e|analy[sz]es|inspect|inspects|examine|examines|"
    r"study|studies|browse|see|understand)\b"
)
_READ_ZH = ("查看", "看", "读", "阅读", "检查", "分析", "审查", "研究",
            "了解", "浏览", "参考")

_VERIFY_EN = re.compile(
    r"\b(?:run|runs|ran|running|execute|executes|executed|test|tests|tested|"
    r"testing|verify|verifies|verified|pytest)\b"
)
_VERIFY_ZH = ("跑", "运行", "执行", "测试", "验证")

#: A filename followed by one of these is describing a *result* the task wants,
#: not an artifact to change: "…so test_calc.py passes", "让 test_x.py 通过".
#: Matched on a word boundary: ``password`` must not read as ``pass``.
_VERIFY_OUTCOME_RE = re.compile(
    r"^(?:passes|pass|passing|failed|fails|failing|通过|失败|走通)(?![\w])",
    re.IGNORECASE,
)

#: Phrases that introduce a file the task *describes* rather than edits:
#: "fix the bug described in BUG.md".  A verb before one of these governs the
#: described thing, not the file it lives in.
_DESCRIBING = re.compile(
    r"described\s+in|mentioned\s+in|根据|描述|详见|参见", re.IGNORECASE
)

#: Negation markers.  "don't change calc.py" is an instruction to LEAVE the
#: file alone; turning it into ``file_changed`` made the obligation impossible
#: to satisfy for a compliant agent.  The turn policy layer (:mod:`xenon.repl.
#: execution_policy`) registers the same paths as write-protected.
_NEGATION_ZH = re.compile(r"(?:不要|别|勿|不得|禁止|切勿|无需)")
_NEGATION_EN = re.compile(r"\b(?:don'?t|do\s+not|never|avoid)\b", re.IGNORECASE)
#: A keep-unchanged result stated *after* the filename.
_AFTER_KEEP = re.compile(
    r"^\s*(?:不变|保持不变|原样|不动|别动|unchanged|untouched|as\s+is)",
    re.IGNORECASE,
)

#: How far back/forward to look for the word that governs a filename.  The
#: backward window is wider to span a verb plus a long directory path; the
#: forward window only needs to reach a post-positioned verb ("文件 需要修复").
_WINDOW = 32
_BACK_WINDOW = 64

#: Sentence/clause terminators.  Search for a governing verb never crosses
#: these: commas (including Chinese 、，) deliberately are NOT terminators —
#: one verb governs a comma-separated file list ("更新 a.py、b.py").
#: A Latin "." only counts when followed by whitespace/end — the dot inside
#: "a.py" is immediately followed by letters and must not cut the clause; a
#: sentence period is followed by a space.
_CLAUSE_END = re.compile(r"[。！？；\n!?;]|\.(?=\s|$)")

#: Sequential connectors.  A verb after one of these starts a *new* action and
#: does not govern the file just named ("c.py then run tests" — the tests are
#: not what c.py must do as an obligation); only the verb the file is the
#: subject/object of may govern it ("shapes.py 需要修复").
_AFTER_CONNECTOR = re.compile(
    r"\bthen\b|\bafterwards?\b|然后|接着|之后|随后|接下来"
)


def _normalise(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _looks_like_test(path: str) -> bool:
    """A test *entry point*, judged by naming convention rather than substring.

    ``latest.py`` and ``contest.py`` contain "test" without being tests; a
    wrongly attributed ``test_passed(latest.py)`` can never be satisfied by a
    real test receipt.
    """

    normalised = _normalise(path)
    name = normalised.rsplit("/", 1)[-1].lower()
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name in {"test.py", "tests.py", "conftest.py"}
        or normalised.startswith("tests/")
        or "/tests/" in f"/{normalised}"
    )


def _clause_before(text: str, start: int) -> str:
    """The current clause's text immediately before the filename.

    Capped at ``_BACK_WINDOW`` and cut at the last sentence terminator, so a
    verb governing an earlier sentence cannot win, and long paths cannot push
    the governing verb out of reach.  A describing phrase ("described in",
    "根据") also cuts: the verb before it governs the *bug*, not the file it
    is written in.
    """

    segment = text[max(0, start - _BACK_WINDOW) : start]
    boundaries = [item.end() for item in _CLAUSE_END.finditer(segment)]
    if boundaries:
        segment = segment[boundaries[-1]:]
    describing = list(_DESCRIBING.finditer(segment))
    if describing:
        segment = segment[describing[-1].end():]
    return segment.lower()


def _clause_after(text: str, end: int) -> str:
    """The current clause's text immediately after the filename."""

    segment = text[end : end + _WINDOW]
    boundary = _CLAUSE_END.search(segment)
    if boundary is not None:
        segment = segment[: boundary.start()]
    # A sequential connector starts a new action; verbs beyond it cannot
    # govern this file.
    connector = _AFTER_CONNECTOR.search(segment)
    if connector is not None:
        segment = segment[: connector.start()]
    return segment.lower()


def _nearest_verb(segment: str, *, side: str) -> tuple[int, str] | None:
    """The closest governing verb in one side-clause and its kind.

    Distance is measured from the filename edge: end-of-match on the left
    (``side="before"``), start-of-match on the right — so before/after results
    are directly comparable.  ``None`` when this side carries no verb.
    """

    candidates = [
        (_VERIFY_EN, _VERIFY_ZH, "verify"),
        (_WRITE_EN, _WRITE_ZH, "write"),
        (_READ_EN, _READ_ZH, "read"),
    ]
    best: tuple[int, str] | None = None
    length = len(segment)
    for en_pattern, zh_suffixes, kind in candidates:
        en_match = None
        for item in en_pattern.finditer(segment):
            en_match = item
            if side == "after":
                break  # first match is the nearest on the right
        if en_match is not None:
            distance = (
                en_match.start()
                if side == "after"
                else length - en_match.end()
            )
            if best is None or distance < best[0]:
                best = (distance, kind)
        for suffix in zh_suffixes:
            position = segment.rfind(suffix) if side == "before" else segment.find(suffix)
            if position < 0:
                continue
            distance = (
                position
                if side == "after"
                else length - (position + len(suffix))
            )
            if best is None or distance < best[0]:
                best = (distance, kind)
    return best


def _classify(text: str, start: int, end: int) -> str:
    """What does the task ask of the file at ``text[start:end]``?

    The outcome check comes first on purpose.  "Fix it so test_calc.py passes"
    has a write verb ("Fix") earlier in the sentence, but it governs a different
    file; what governs *this* one is "passes" immediately after it.

    Then the nearest governing verb on either side wins — verbs follow the
    filename as often as they precede it ("shapes.py 需要修复").  When **no**
    verb governs the file in either direction, the file was merely named; the
    safe classification is read (no obligation): an unsatisfiable contract is
    worse than no contract (design doc §1.5, calibrated 2026-11-10).
    """

    after = _clause_after(text, end)
    if _VERIFY_OUTCOME_RE.match(after.lstrip()):
        return "verify"

    before = _clause_before(text, start)
    nearest: tuple[int, str] | None = None
    for side, segment in (
        ("before", before),
        ("after", after),
    ):
        found = _nearest_verb(segment, side=side)
        if found is not None and (nearest is None or found[0] < nearest[0]):
            nearest = found

    kind = nearest[1] if nearest is not None else "read"
    # A negated write is an instruction to leave the file alone: the turn
    # policy registers it as write-protected, and demanding ``file_changed``
    # would make compliance impossible.
    if kind == "write" and (
        _AFTER_KEEP.match(after.lstrip())
        or _NEGATION_ZH.search(before)
        or _NEGATION_EN.search(before)
    ):
        return "read"
    return kind


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
    roles: dict[str, set[str]] = {}
    for match in _FILE.finditer(objective):
        path = _normalise(match.group(1))
        if not path or _NON_SOURCE.match(path):
            continue
        raw_files.append(path)
        roles.setdefault(path, set()).add(
            _classify(objective, match.start(1), match.end(1))
        )

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

    # One role per file, strongest ask wins: a file mentioned twice, once to
    # read and once to change, still has to change.
    for path in files:
        wanted = roles.get(path) or {"write"}
        if "write" in wanted:
            add(PropositionKind.FILE_CHANGED, path)
        elif "verify" in wanted:
            add(PropositionKind.TEST_PASSED, path)

    runner = _TEST_RUNNER.search(objective)
    if runner:
        # Prefer a test file the task named; otherwise the runner itself.
        test_file = next(
            (path for path in files if _looks_like_test(path)), ""
        )
        add(PropositionKind.TEST_PASSED, test_file or runner.group(1))

    return tuple(obligations)
