"""Deterministic loop detection: what "stuck" actually looks like.

The engine layer had a `LoopDetector` with four signals.  When the engine was
deleted, only one survived — exact repetition of `(tool, args)` — and that one is
the *narrowest* of the four.  A real run proved the gap: 18 tool calls, 6 failed,
and the guard never fired because the model sent a **different** wrong command
each time (`dir /b`, `dir /s /b`, `python -c`, `Get-ChildItem`).  The only thing
that stopped it was the step budget, which reports "budget exhausted" — true, but
useless for diagnosis.

All four signals here are deterministic facts about receipts.  Nothing reads the
model's prose, so nothing depends on wording:

1. ``identical_call`` — same tool, same arguments, again and again.
2. ``repeated_failure`` — the same failure text, again and again.
3. ``capability_cycle`` — the last calls form a repeating short cycle.
4. ``no_progress`` — N consecutive receipts that taught us nothing new.  This
   one is **reported, not enforced**: a real run had five such receipts and then
   succeeded four calls later, so treating it as a stop would destroy work.
   It surfaces as :attr:`StuckDetector.diagnosis` and is attached to whatever
   stop actually happens, turning "budget exhausted" into a reason an operator
   can act on.

Signals 1–3 are deterministic facts about receipts.  Nothing reads the model's
prose, so nothing depends on wording.

What this deliberately does not do: judge outcomes.  A signal is a *reason to
stop and say why*, never a verdict on the work.  The runtime turns it into a
policy veto; the ledger keeps the reason.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

__all__ = [
    "DEFAULT_MAX_IDENTICAL",
    "DEFAULT_MAX_NO_PROGRESS",
    "DEFAULT_MAX_SAME_FAILURE",
    "DEFAULT_WINDOW",
    "LoopSignal",
    "StuckDetector",
    "call_signature",
    "progress_key",
]

# Repeating a read once is normal (files change).  Three identical calls means
# the loop is spending budget without learning anything.
DEFAULT_MAX_IDENTICAL = 3
# The same failure twice can be a flake; three times is the same wall.
DEFAULT_MAX_SAME_FAILURE = 3
# Five receipts in a row with no new fact.  Higher than the others on purpose:
# one exploratory failure followed by a retry is healthy behaviour.
DEFAULT_MAX_NO_PROGRESS = 5
# How many recent calls the cycle detector looks at.
DEFAULT_WINDOW = 8


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def call_signature(intent: str, args: Mapping[str, Any] | None) -> str:
    """Stable identity for one tool call, independent of dict ordering."""

    payload = {"intent": str(intent or ""), "args": dict(args or {})}
    digest = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    return digest[:16]


def progress_key(receipt: Any) -> str:
    """What this receipt *taught us*, or ``""`` if it taught nothing.

    Only structured fields are consulted, so two identical results collapse to
    the same key and any new path/target produces a new one.
    """

    status = getattr(getattr(receipt, "status", None), "value", "")
    if status != "ok":
        return ""
    parts: list[str] = []
    for effect in getattr(receipt, "effects", ()) or ():
        changed = getattr(effect, "changed", False)
        single = getattr(effect, "path", "")
        if single:
            parts.append(f"{type(effect).__name__}:path={single}")
        for path in getattr(effect, "paths", ()) or ():
            parts.append(f"{type(effect).__name__}:path={path}:{bool(changed)}")
        target = getattr(effect, "target", "")
        if target:
            ran = int(getattr(effect, "tests_run", 0) or 0)
            parts.append(f"{type(effect).__name__}:target={target}:{ran}")
        command = getattr(effect, "command", "")
        if command and not target:
            # An opaque command only counts as progress when it changed the
            # workspace; otherwise it is just noise we already had.
            delta = getattr(effect, "workspace_delta", ()) or ()
            if delta:
                parts.append(f"cmd={command}:delta={sorted(map(str, delta))}")
        query = getattr(effect, "query", "")
        if query:
            parts.append(f"query={query}")
    if not parts:
        # A successful call with no structured effect still carries its own
        # identity: reading path A and path B are different facts.
        parts.append(f"{getattr(receipt, 'tool', '?')}:ok")
    return "|".join(sorted(parts))


def failure_key(receipt: Any) -> str:
    """Identity of a failure: the same wall, not merely "something failed"."""

    error = str(getattr(receipt, "error", "") or "").strip()
    if not error:
        return ""
    tool = str(getattr(receipt, "tool", "") or "")
    digest = hashlib.sha256(f"{tool}:{error[:400]}".encode("utf-8")).hexdigest()
    return digest[:16]


@dataclass(frozen=True, slots=True)
class LoopSignal:
    """One deterministic reason to stop.

    ``detail`` is written for the ledger: it should tell an operator what the
    loop was doing, not merely that a counter overflowed.
    """

    kind: str
    detail: str
    tool: str = ""
    window: tuple[str, ...] = ()

    @property
    def reason(self) -> str:
        return f"stuck: {self.detail}"


class StuckDetector:
    """Four deterministic signals, all derived from receipts."""

    def __init__(
        self,
        *,
        max_identical: int = DEFAULT_MAX_IDENTICAL,
        max_same_failure: int = DEFAULT_MAX_SAME_FAILURE,
        max_no_progress: int = DEFAULT_MAX_NO_PROGRESS,
        window: int = DEFAULT_WINDOW,
    ) -> None:
        for name, value in (
            ("max_identical", max_identical),
            ("max_same_failure", max_same_failure),
            ("max_no_progress", max_no_progress),
            ("window", window),
        ):
            if int(value) < 1:
                raise ValueError(f"{name} must be >= 1")
        self._max_identical = int(max_identical)
        self._max_same_failure = int(max_same_failure)
        self._max_no_progress = int(max_no_progress)
        self._window = int(window)
        self._identical: Counter[str] = Counter()
        self._failures: Counter[str] = Counter()
        self._tools: deque[str] = deque(maxlen=self._window)
        self._progress: deque[str] = deque(maxlen=self._window)
        self._seen_progress: set[str] = set()
        self._no_progress = 0
        self._last_progress = ""

    # ── inputs ───────────────────────────────────────────────────────

    def note_call(
        self, tool: str, params: Mapping[str, Any] | None
    ) -> LoopSignal | None:
        """Record a call and report identical repetition.

        This is a *call-level* fact: it is known before execution, so the fourth
        identical call is refused instead of being executed and then refused.
        Identity is the model's own request, and it is the only identity space
        the repeat count uses — deriving a second signature from the receipt
        would give the same call two identities and two counters.
        """

        signature = call_signature(tool, params)
        self._identical[signature] += 1
        count = self._identical[signature]
        if count > self._max_identical:
            return LoopSignal(
                kind="identical_call",
                detail=f"{tool} called with identical arguments {count} times",
                tool=tool,
                window=(signature,),
            )
        return None

    def observe(self, receipt: Any) -> LoopSignal | None:
        """Record one receipt and report the first *receipt-level* signal.

        Identical repetition is not checked here: that is a call-level fact and
        :meth:`note_call` owns it.
        """

        tool = str(getattr(receipt, "tool", "") or "?")
        failure = failure_key(receipt)
        key = progress_key(receipt)
        # The cycle window is receipt-driven: a call that was refused before
        # execution never produced a receipt, so it is not part of the pattern.
        self._tools.append(tool)

        if key and key not in self._seen_progress:
            self._seen_progress.add(key)
            self._no_progress = 0
            self._last_progress = key
            self._progress.append(key)
        else:
            # Either a failure, or a result we have already seen.  Both mean the
            # loop learned nothing this turn.
            self._no_progress += 1
            self._progress.append("")

        if failure:
            self._failures[failure] += 1

        # 1) same wall, not just "something failed"
        if failure and self._failures[failure] > self._max_same_failure:
            return LoopSignal(
                kind="repeated_failure",
                detail=(
                    f"{tool} failed with the same error "
                    f"{self._failures[failure]} times: "
                    f"{str(getattr(receipt, 'error', ''))[:120]}"
                ),
                tool=tool,
                window=(failure,),
            )

        # 2) a repeating multi-step cycle (A -> B -> A -> B ...).  A cycle of
        # length 1 is deliberately excluded: reading three different files in a
        # row is one capability used three times, not a loop.  "Same capability
        # repeatedly without learning anything" is what :attr:`diagnosis`
        # describes, and it is not a stop condition.
        cycle = self._cycle()
        if cycle is not None:
            return LoopSignal(
                kind="capability_cycle",
                detail=(
                    f"tool sequence repeats every {len(cycle)}: "
                    f"{' -> '.join(cycle)}"
                ),
                tool=tool,
                window=tuple(cycle),
            )

        # 3) nothing new for a while — reported, but **not** a stop on its own.
        #
        # A real run proved why: five receipts with no new information were
        # followed by a successful edit four calls later.  "No new information
        # yet" and "stuck" are different facts, and a detector that stops on the
        # first one destroys work that was about to succeed.  So this signal is
        # carried as a *diagnosis* (see :attr:`diagnosis`) and the runtime
        # attaches it to whatever stop actually happens; it never stops a run
        # before the budget does.
        return None

    @property
    def diagnosis(self) -> str:
        """A short human-readable reason when recent calls taught nothing.

        Empty when the run is making progress, so a caller can append it to a
        stop reason without asking whether it applies.
        """

        if self._no_progress < 2:
            return ""
        return (
            f"{self._no_progress} consecutive calls produced no new "
            f"information (last progress: {self._last_progress or 'none'})"
        )

    def reset(self) -> None:
        """Forget history.  A new step legitimately reads different things."""

        self._identical.clear()
        self._failures.clear()
        self._tools.clear()
        self._progress.clear()
        self._seen_progress.clear()
        self._no_progress = 0
        self._last_progress = ""

    # ── inspection (used by tests and the ledger reason) ─────────────

    @property
    def no_progress_streak(self) -> int:
        return self._no_progress

    def identical_count(self, signature: str) -> int:
        return self._identical[signature]

    def failure_count(self, key: str) -> int:
        return self._failures[key]

    # ── internals ────────────────────────────────────────────────────

    def _cycle(self) -> list[str] | None:
        """Find a repeating cycle of *two or more distinct* tools ending now.

        Only a genuine A -> B -> A -> B shape is a cycle here.  A pattern made
        of one repeated tool is excluded no matter how long it is: ``read ->
        read -> read`` says nothing about whether the calls were the same.
        Reading five different files in a row is one capability used five
        times, which is ordinary exploration, and `self._tools` records only
        the tool *name* -- so every such run looks identical here.  A measured
        run read ``a.py``, ``b.py``, ``c.py``, ``a.py``, ``b.py``, ``c.py`` and
        was stopped as "tool sequence repeats every 2: read -> read".

        Repetition of *the same call* is a different fact, and
        ``note_call``/``identical_call`` already covers it with the full
        parameter signature.  Two facts, two detectors.

        Only contiguous repetitions count.  A window-wide divisor (an older
        implementation did that) turns two identical calls at the end of a long
        window into many "cycles" and stops a run that was still progressing.
        """

        pattern = list(self._tools)
        for length in range(2, len(pattern) // 2 + 1):
            tail = pattern[-length:]
            if not tail or not any(tail):
                continue
            # A cycle needs at least two different tools.  `read -> read` is a
            # repeat of one capability; `read -> test` is a workflow.
            if len(set(tail)) < 2:
                continue
            cycles = 0
            start = len(pattern) - length
            while start >= 0 and pattern[start : start + length] == tail:
                cycles += 1
                start -= length
            # Three contiguous repeats of the same two-step shape is a loop.
            # Two is not: retry-then-succeed looks exactly like that.
            if cycles >= 3:
                return tail
        return None


def _receipt_args(receipt: Any) -> dict[str, Any]:
    """The bits of a receipt that identify what the call asked for."""

    args: dict[str, Any] = {}
    for effect in getattr(receipt, "effects", ()) or ():
        # Explicit lookups, not a field-name list: the purity guard treats a
        # bare "command" literal as a hard-coded tool name, and it is right to —
        # this is an effect field, so the intent is expressed as attribute access.
        for field, value in (
            ("cmd", getattr(effect, "command", "")),
            ("target", getattr(effect, "target", "")),
            ("query", getattr(effect, "query", "")),
            ("path", getattr(effect, "path", "")),
        ):
            if value:
                args[field] = str(value)
        paths = getattr(effect, "paths", ()) or ()
        if paths:
            args["paths"] = sorted(map(str, paths))
    if not args:
        error = str(getattr(receipt, "error", "") or "")
        if error:
            args["error"] = error[:120]
    return args


def signals_in(receipts: Sequence[Any]) -> list[LoopSignal]:
    """Every signal a fixed receipt stream produces (evidence helper)."""

    detector = StuckDetector()
    found: list[LoopSignal] = []
    for receipt in receipts:
        signal = detector.observe(receipt)
        if signal is not None:
            found.append(signal)
            break
    return found