"""Outer task loop (design doc §4.3): up to 3 rounds, state carried.

The defects these tests pin down, all observed in the first implementation:

* retry rights were classified by **phase**, so step-budget exhaustion — the
  common case — exits BLOCKED and was never retried;
* when a round did retry, the step count, deadline and stall detector were
  not reset, so round two died at its first limit check with zero model
  calls — a theatrical retry;
* a user cancel was "resurrected" three times because ABORTED matched the
  retry set, leaving a ledger whose last phase said RUNNING while the result
  said FAILED.

The classification now rides one explicit ``stop_signal``; only
``step_budget`` / ``time_budget`` / ``stalled`` earn another round.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import xenon.kernel  # noqa: F401 — ledger<->kernel import cycle
from xenon.catalog.catalog import ToolCatalog
from xenon.catalog.gateway import LegacyToolGateway
from xenon.catalog.specs import CORE_SPECS
from xenon.kernel.contracts import (
    Abort,
    AskUser,
    Budget,
    CallTool,
    Capability,
    Finish,
    TaskSpec,
    ToolIntent,
)
from xenon.kernel.controller import VerificationController
from xenon.kernel.policies import PermissionPolicy
from xenon.kernel.runtime import LoopRuntime
from xenon.ledger.events import EventKind
from xenon.ledger.store import Ledger


class Script:
    """A deterministic strategy: hands back scripted actions in order."""

    def __init__(self, actions: list[Any]) -> None:
        self._actions = list(actions)
        self.calls = 0

    def propose(self, view: Any) -> Any:
        self.calls += 1
        if not self._actions:
            return Finish("unexpected extra proposal")
        return self._actions.pop(0)


def _read() -> CallTool:
    return CallTool(
        ToolIntent(
            capability=Capability.READ,
            params={"file_path": "hello.txt"},
        )
    )


def _build_runtime(tmp_path: Path) -> tuple[LoopRuntime, Any]:
    (tmp_path / "hello.txt").write_text("hello world\n", encoding="utf-8")
    ledger = Ledger.in_memory("t1")
    policy = PermissionPolicy(workspace=str(tmp_path))
    runtime = LoopRuntime(
        catalog=ToolCatalog(CORE_SPECS, "core"),
        gateway=LegacyToolGateway(
            specs=CORE_SPECS,
            workspace=tmp_path,
            policy=policy,
        ),
        controller=VerificationController(),
        ledger=ledger,
        policies=(policy,),
    )
    return runtime, ledger


def _spec(tmp_path: Path, **budget: Any) -> TaskSpec:
    return TaskSpec(
        task_id="t1",
        objective="read the file",
        workspace=str(tmp_path),
        budget=Budget(**budget),
    )


def _decisions(ledger: Any) -> list[dict[str, Any]]:
    # Some CONTROL_DECISION payloads (finalize, steering) key their label as
    # "kind" instead of "decision"; those are not what these tests classify on.
    return [
        event.payload
        for event in ledger.read()
        if event.kind is EventKind.CONTROL_DECISION
        and "decision" in event.payload
    ]


# ── retryable exits ──────────────────────────────────────────────────


def test_step_budget_exhaustion_gets_a_fresh_round_and_finishes(tmp_path: Path) -> None:
    runtime, ledger = _build_runtime(tmp_path)
    script = Script([_read(), Finish("done")])

    result = runtime.run(_spec(tmp_path, max_steps=1), script)

    # Round one burned its one step on the read; without the outer loop the
    # run ends BLOCKED here.  Round two starts at steps=0 and reaches finish.
    assert result.phase.value == "finalized"
    retry = [d for d in _decisions(ledger) if d["decision"] == "outer_round_retry"]
    assert len(retry) == 1
    assert retry[0]["last_signal"] == "step_budget"


def test_state_carries_across_rounds(tmp_path: Path) -> None:
    runtime, ledger = _build_runtime(tmp_path)
    script = Script([_read(), Finish("done")])

    watermark_before = ledger.watermark()
    runtime.run(_spec(tmp_path, max_steps=1), script)

    events = ledger.read()
    receipts = [e for e in events if e.kind is EventKind.TOOL_RECEIPT]
    # The one real read happened in round one; round two did not redo it, and
    # the ledger never rewound — the fact was still there to build a view from.
    assert len(receipts) == 1
    retry_seq = next(
        e.seq for e in events if e.payload.get("decision") == "outer_round_retry"
    )
    assert receipts[0].seq < retry_seq
    assert ledger.watermark() > watermark_before


class _ScriptedClock:
    """Advance past the stall window right after the receipt handler runs.

    Call order in round one: run() init (1), begin-round (2), first limit
    check (3) all read t0; the receipt handler's progress refresh (4) reads
    t0+500; every reading afterwards (the second limit check onward) is
    t0+700 — 200s beyond the refreshed timestamp, past the 180s window.
    """

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        if self.calls <= 3:
            return 1000.0
        if self.calls == 4:
            return 1500.0
        return 1700.0


def test_stalled_gets_a_fresh_round(tmp_path: Path) -> None:
    runtime, ledger = _build_runtime(tmp_path)
    runtime._clock = _ScriptedClock()
    script = Script([_read(), Finish("done")])

    result = runtime.run(
        _spec(tmp_path, max_steps=10, max_stall_seconds=180), script
    )

    assert result.phase.value == "finalized"
    retry = [d for d in _decisions(ledger) if d["decision"] == "outer_round_retry"]
    assert len(retry) == 1
    assert retry[0]["last_signal"] == "stalled"


# ── three rounds exhausted ───────────────────────────────────────────


def test_three_rounds_exhausted_blocks_without_delivery(tmp_path: Path) -> None:
    runtime, ledger = _build_runtime(tmp_path)
    # One read per round, then the limit check ends it: three actions total.
    script = Script([_read(), _read(), _read()])

    result = runtime.run(_spec(tmp_path, max_steps=1), script)

    assert result.phase.value == "blocked"
    assert result.accepted is False
    assert result.stop_signal == "step_budget"
    decisions = _decisions(ledger)
    assert [d["decision"] for d in decisions].count("outer_round_retry") == 2
    assert decisions[-1]["decision"] == "outer_rounds_exhausted"
    assert decisions[-1]["outer_rounds"] == 3
    # The result's phase is the ledger's final phase — no RUNNING/FAILED split.
    transitions = [e for e in ledger.read() if e.kind is EventKind.STATE_TRANSITION]
    assert transitions[-1].payload["to"] == "blocked"


# ── non-retryable exits ──────────────────────────────────────────────


def test_model_abort_is_not_retried(tmp_path: Path) -> None:
    runtime, ledger = _build_runtime(tmp_path)
    script = Script([Abort("I judge this impossible"), Finish("unreachable")])

    result = runtime.run(_spec(tmp_path, max_steps=10), script)

    assert result.phase.value == "aborted"
    assert result.stop_signal == "abort"
    assert script.calls == 1
    assert _decisions(ledger) == []


def test_ask_user_is_not_retried(tmp_path: Path) -> None:
    runtime, ledger = _build_runtime(tmp_path)
    script = Script([AskUser("which file?"), Finish("unreachable")])

    result = runtime.run(_spec(tmp_path, max_steps=10), script)

    assert result.phase.value == "blocked"
    assert result.stop_signal == "awaiting_user"
    assert script.calls == 1
    assert _decisions(ledger) == []


def test_user_cancel_is_not_resurrected(tmp_path: Path) -> None:
    runtime, ledger = _build_runtime(tmp_path)
    runtime.request_cancel()
    script = Script([Finish("unreachable")])

    result = runtime.run(_spec(tmp_path, max_steps=10), script)

    assert result.phase.value == "aborted"
    assert result.stop_signal == "cancelled"
    # The cancel was seen before any proposal; no round was even attempted.
    assert script.calls == 0
    assert _decisions(ledger) == []
