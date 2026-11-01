"""`xenon replay` / `xenon metrics` (#289): the ledger explains itself.

A replay must be model-free and deterministic: same ledger, same report,
after the worktree is gone.
"""

from __future__ import annotations

import json

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
