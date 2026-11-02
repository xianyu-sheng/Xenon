"""`xenon replay` / `xenon metrics` (#289): the ledger explains itself.

A replay must be model-free and deterministic: same ledger, same report,
after the worktree is gone.
"""

from __future__ import annotations

import json

import pytest

from tests.kernel.test_north_star import (
    FakeCatalog,
    FakeGateway,
    ScriptedStrategy,
    _calc_spec,
)
from xenon.kernel.contracts import (
    CallTool,
    Capability,
    Finish,
    ToolIntent,
)
from xenon.kernel.controller import VerificationController
from xenon.kernel.runtime import LoopRuntime
from xenon.ledger.store import Ledger
from xenon.replay import (
    metrics,
    render_metrics,
    render_timeline,
    resolve_ledger,
    timeline,
)


def _run(tmp_path):
    ledger = Ledger(tmp_path / "task-1.jsonl", "task-1")
    runtime = LoopRuntime(
        catalog=FakeCatalog(),
        gateway=FakeGateway(),
        controller=VerificationController(),
        ledger=ledger,
    )
    runtime.run(
        _calc_spec(),
        ScriptedStrategy(
            [
                CallTool(ToolIntent(capability=Capability.READ)),
                CallTool(ToolIntent(capability=Capability.EDIT)),
                CallTool(ToolIntent(capability=Capability.VERIFY)),
                Finish("done"),
            ]
        ),
    )
    return ledger, list(ledger.read())


def test_metrics_count_the_work(tmp_path):
    _ledger, events = _run(tmp_path)

    data = metrics(events)

    assert data["events"] == len(events)
    assert data["tool_calls"] == 3
    assert data["final_phase"]
    assert data["accepted"] in (True, False)


def test_metrics_account_for_usage_events():
    class _E:
        kind = type("K", (), {"value": "model_usage"})()

        def __init__(self, seq: int, payload: dict) -> None:
            self.seq = seq
            self.payload = payload

        ts = ""

    events = [
        _E(1, {"total_tokens": 100, "estimated": False}),
        _E(2, {"total_tokens": 50, "estimated": True}),
    ]

    data = metrics(events)

    assert data["model_calls"] == 2
    assert data["tokens"] == 150
    assert data["estimated_calls"] == 1


def test_timeline_orders_by_seq_and_hides_noise(tmp_path):
    _ledger, events = _run(tmp_path)

    rows = timeline(events)

    assert [row["seq"] for row in rows] == sorted(row["seq"] for row in rows)
    assert all(row["kind"] for row in rows)
    assert any(row["kind"] == "tool_receipt" for row in rows)

    everything = timeline(events, include_all=True)
    assert len(everything) >= len(rows)


def test_replay_is_deterministic(tmp_path):
    _ledger, events = _run(tmp_path)

    first = render_timeline(timeline(events))
    second = render_timeline(timeline(events))

    assert first == second
    assert render_metrics(metrics(events)) == render_metrics(metrics(events))


def test_resolve_ledger_finds_a_task_id_and_a_path(tmp_path):
    ledger, _events = _run(tmp_path)

    assert resolve_ledger(ledger.path) == ledger.path
    assert resolve_ledger("task-1", root=tmp_path) == ledger.path

    try:
        resolve_ledger("nope", root=tmp_path)
    except FileNotFoundError as exc:
        assert "nope" in str(exc)
    else:  # pragma: no cover - the lookup must fail closed
        raise AssertionError("missing ledger must raise")


def test_cli_writes_json(tmp_path, capsys, monkeypatch):
    from xenon import main as main_mod

    ledger, _events = _run(tmp_path)

    class _Args:
        task = str(ledger.path)
        json = True
        all = False

    code = main_mod._cmd_replay("metrics", str(ledger.path), _Args())
    out = capsys.readouterr().out

    assert code == 0
    payload = json.loads(out)
    assert payload["events"] > 0
    assert "tokens" in payload


def test_cli_reports_a_missing_ledger(tmp_path):
    from xenon import main as main_mod

    class _Args:
        task = str(tmp_path / "nope.jsonl")
        json = False
        all = False

    assert main_mod._cmd_replay("metrics", str(tmp_path / "nope.jsonl"), _Args()) == 2


# ── 每个事件种类的渲染分支 ────────────────────────────────────────────


@pytest.mark.parametrize(
    "kind,payload,needle",
    [
        ("tool_receipt", {"receipt": {"tool": "read", "status": "ok"}}, "read -> ok"),
        ("plan_proposed", {"plan": {"steps": [{"id": 1}, {"id": 2}]}}, "2 step(s)"),
        ("plan_accepted", {"plan": {"steps": []}}, "0 step(s)"),
        ("step_closed", {"step_id": "s1"}, "s1"),
        ("debt_opened", {"obligation_id": "o1", "reason": "no test"}, "o1: no test"),
        ("debt_closed", {"obligation_id": "o1"}, "o1"),
        ("model_usage", {"total_tokens": 10, "cumulative_total": 20}, "10 tokens"),
        ("context_compacted", {"count": 3}, "folded 3"),
        ("user_input", {"kind": "constraint", "text": "no e2e"}, "no e2e"),
        ("policy_decision", {"kind": "veto", "reason": "denied"}, "veto: denied"),
        ("control_decision", {"kind": "deliver", "reason": "ok"}, "deliver: ok"),
        ("audit_reported", {"evidence_status": "verified"}, "verified"),
        ("delivered", {"commit": "abc1234"}, "abc1234"),
        ("finalized", {"commit": "deadbee"}, "deadbee"),
        ("state_transition", {"from": "running", "to": "blocked"}, "running -> blocked"),
        ("task_started", {"objective": "fix the bug"}, "fix the bug"),
    ],
)
def test_every_event_kind_renders_a_useful_line(kind, payload, needle):
    from xenon.replay import timeline

    class _E:
        seq = 1

        def __init__(self, kind, payload):
            self.kind = type("K", (), {"value": kind})()
            self.payload = payload

    rows = timeline([_E(kind, payload)], include_all=True)

    assert rows and needle in rows[0]["line"]


def test_an_unknown_event_falls_back_to_its_text():
    from xenon.replay import timeline

    class _E:
        seq = 7
        kind = type("K", (), {"value": "something_new"})()
        payload = {"text": "hello there"}

    rows = timeline([_E()], include_all=True)

    assert "hello there" in rows[0]["line"]


def test_metrics_fold_every_counter():
    """Each counter must move on its own event; a stuck counter is a silent lie."""

    from xenon.replay import metrics

    class _E:
        seq = 0
        ts = ""

        def __init__(self, kind, payload):
            _E.seq += 1
            self.seq = _E.seq
            self.kind = type("K", (), {"value": kind})()
            self.payload = payload

    events = [
        _E("model_usage", {"total_tokens": 100, "estimated": True}),
        _E("tool_receipt", {"receipt": {"tool": "read", "status": "ok"}}),
        _E("step_started", {"step_id": "s1"}),
        _E("context_compacted", {"count": 4}),
        _E("user_input", {"kind": "constraint", "text": "x"}),
        _E("user_approval", {"approved": True}),
        _E("binding_failed", {"reason": "no tool"}),
        _E("audit_reported", {"scope": "final"}),
        _E("debt_opened", {"obligation_id": "o1"}),
        _E("debt_closed", {"obligation_id": "o1"}),
        _E("policy_decision", {"reason": "repeated identical tool call"}),
        _E("state_transition", {"to": "blocked", "reason": "budget"}),
    ]

    data = metrics(events)

    assert data["model_calls"] == 1 and data["tokens"] == 100
    assert data["estimated_calls"] == 1
    assert data["tool_calls"] == 1
    assert data["steps"] == 1
    assert data["compactions"] == 1 and data["folded_events"] == 4
    assert data["steering_inputs"] == 1
    assert data["approvals"] == 1
    assert data["replans"] == 1
    assert data["audit_rounds"] == 1
    assert data["repair_vetoes"] == 1
    assert data["open_debt"] == []
    assert data["closed_debt"] == ["o1"]
    assert data["final_phase"] == "blocked"
    assert data["final_reason"] == "budget"


def test_metrics_keep_unclosed_debt_open():
    from xenon.replay import metrics

    class _E:
        seq = 1
        ts = ""

        def __init__(self, kind, payload):
            self.kind = type("K", (), {"value": kind})()
            self.payload = payload

    data = metrics([_E("debt_opened", {"obligation_id": "o1"})])

    assert data["open_debt"] == ["o1"]


# ── blocking_reason：blocked 必须能解释自己 ───────────────────────────


def _ledger_with(tmp_path, events):
    import xenon.kernel  # noqa: F401 — breaks the ledger<->kernel import cycle
    from xenon.ledger.events import EventKind, EventSource
    from xenon.ledger.store import Ledger

    ledger = Ledger(tmp_path / "t.jsonl", "t")
    for kind, payload in events:
        ledger.append(getattr(EventKind, kind.upper()), EventSource.GATE, payload)
    return ledger


def test_blocking_reason_is_empty_when_the_ledger_is_missing(tmp_path):
    from xenon.replay import blocking_reason

    assert blocking_reason(tmp_path / "nope.jsonl", task_id="t") == ""


def test_blocking_reason_reads_a_veto(tmp_path):
    from xenon.replay import blocking_reason

    ledger = _ledger_with(
        tmp_path,
        [("policy_decision", {"kind": "veto", "reason": "verify requires a grant"})],
    )

    assert blocking_reason(ledger.path, task_id="t") == "verify requires a grant"


def test_blocking_reason_includes_the_scope(tmp_path):
    from xenon.replay import blocking_reason

    ledger = _ledger_with(
        tmp_path,
        [
            (
                "policy_decision",
                {"kind": "veto", "reason": "network requires a grant", "scope": ["remote", "github"]},
            )
        ],
    )

    reason = blocking_reason(ledger.path, task_id="t")

    assert "network requires a grant" in reason
    assert "github" in reason


def test_blocking_reason_names_a_rejected_approval(tmp_path):
    from xenon.replay import blocking_reason

    ledger = _ledger_with(
        tmp_path,
        [("user_approval", {"approved": False, "reason": "用户拒绝"})],
    )

    assert blocking_reason(ledger.path, task_id="t") == "审批未通过：用户拒绝"


def test_blocking_reason_skips_an_approved_request(tmp_path):
    from xenon.replay import blocking_reason

    ledger = _ledger_with(
        tmp_path,
        [
            ("user_approval", {"approved": True, "reason": "fine"}),
            ("control_decision", {"kind": "block", "reason": "open debt"}),
        ],
    )

    assert blocking_reason(ledger.path, task_id="t") == "open debt"


def test_blocking_reason_uses_the_last_blocking_event(tmp_path):
    from xenon.replay import blocking_reason

    ledger = _ledger_with(
        tmp_path,
        [
            ("policy_decision", {"kind": "veto", "reason": "first"}),
            ("policy_decision", {"kind": "veto", "reason": "second"}),
        ],
    )

    assert blocking_reason(ledger.path, task_id="t") == "second"


def test_blocking_reason_is_empty_without_a_blocking_event(tmp_path):
    from xenon.replay import blocking_reason

    ledger = _ledger_with(tmp_path, [("task_started", {"objective": "x"})])

    assert blocking_reason(ledger.path, task_id="t") == ""


def test_blocking_reason_survives_an_unreadable_ledger(tmp_path):
    from xenon.replay import blocking_reason

    broken = tmp_path / "broken.jsonl"
    broken.write_text("{not json", encoding="utf-8")

    assert blocking_reason(broken, task_id="t") == ""


def test_blocking_reason_skips_a_rejection_without_a_reason(tmp_path):
    """`approved: false` with no reason must not produce a half-sentence."""

    from xenon.replay import blocking_reason

    ledger = _ledger_with(
        tmp_path,
        [("user_approval", {"approved": False, "reason": ""})],
    )

    assert blocking_reason(ledger.path, task_id="t") == ""


def test_blocking_reason_returns_empty_when_the_ledger_cannot_be_opened(monkeypatch, tmp_path):
    """A reporting helper must never raise — it runs on the failure path."""

    import xenon.kernel  # noqa: F401
    import xenon.ledger.store as store
    from xenon.replay import blocking_reason

    class _Boom:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("ledger corrupt")

    monkeypatch.setattr(store, "Ledger", _Boom)

    assert blocking_reason(tmp_path / "t.jsonl", task_id="t") == ""
