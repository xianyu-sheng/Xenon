"""The library API must expose the verdict, not just the prose (#202).

A harness that can say "accepted" and then hands the caller a bare string is
forcing them to re-open the ledger to learn the one thing that matters.
"""

from __future__ import annotations

import pytest

import xenon  # noqa: F401  — importing xenon first breaks the ledger<->kernel cycle
import xenon.kernel  # noqa: F401  — same
from xenon import TaskOutcome


class _FakeResult:
    def __init__(self, **kwargs):
        self.task_id = "task-1"
        self.answer = kwargs.get("answer", "done")
        self.accepted = kwargs.get("accepted", True)
        self.phase = type("P", (), {"value": kwargs.get("phase", "finalized")})()
        self.evidence_status = type("E", (), {"value": kwargs.get("evidence", "verified")})()
        self.debt = kwargs.get("debt", ())
        self.finalize_sha = kwargs.get("finalize_sha")


class _FakeRun:
    def __init__(self, ledger_path, **kwargs):
        self.task_id = "task-1"
        self.result = _FakeResult(**kwargs)
        self.ledger_path = ledger_path


def test_the_outcome_carries_the_verdict(tmp_path):
    from xenon import _outcome

    ledger = tmp_path / "t.jsonl"
    ledger.write_text("", encoding="utf-8")
    outcome = _outcome(_FakeRun(ledger, finalize_sha="abc1234"))

    assert outcome.accepted is True
    assert outcome.phase == "finalized"
    assert outcome.evidence == "verified"
    assert outcome.finalize_sha == "abc1234"
    assert outcome.ledger_path == str(ledger)


def test_str_of_the_outcome_is_the_answer():
    from xenon import _outcome

    outcome = _outcome(_FakeRun(None, answer="the answer"))

    assert str(outcome) == "the answer"
    assert outcome.answer == "the answer"


def test_debt_is_exposed_as_reasons(tmp_path):
    from xenon import _outcome

    class _Debt:
        def __init__(self, reason):
            self.reason = reason

    ledger = tmp_path / "t.jsonl"
    ledger.write_text("", encoding="utf-8")
    outcome = _outcome(
        _FakeRun(ledger, accepted=False, phase="blocked", evidence="unverified",
                 debt=(_Debt("no passing test fact"),))
    )

    assert outcome.debt == ("no passing test fact",)
    assert outcome.accepted is False


def test_a_blocked_run_without_debt_still_explains_itself(tmp_path):
    """`blocked` with an empty answer and no debt is not actionable."""

    from xenon import _outcome
    from xenon.ledger.events import EventKind, EventSource
    from xenon.ledger.store import Ledger

    ledger = Ledger(tmp_path / "t.jsonl", "task-1")
    ledger.append(
        EventKind.POLICY_DECISION,
        EventSource.GATE,
        {"kind": "veto", "reason": "verify requires a grant", "scope": ["verify"]},
    )
    outcome = _outcome(
        _FakeRun(ledger.path, accepted=False, phase="blocked", evidence="abstained", answer="")
    )

    assert "verify requires a grant" in outcome.reason


def test_an_accepted_run_has_no_reason(tmp_path):
    from xenon import _outcome

    ledger = tmp_path / "t.jsonl"
    ledger.write_text("", encoding="utf-8")

    assert _outcome(_FakeRun(ledger)).reason == ""


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "auto-edit"),
        ("bypass", "bypass"),
        ("read-only", "read-only"),
        ("scoped", "scoped"),
    ],
)
def test_permission_resolution(value, expected):
    from xenon import _sdk_permission

    assert _sdk_permission(value).value == expected


def test_an_unknown_permission_fails_closed():
    from xenon import _sdk_permission

    with pytest.raises(ValueError):
        _sdk_permission("whatever")


def test_the_public_surface_exports_the_outcome_type():
    assert xenon.TaskOutcome is TaskOutcome
    assert callable(xenon.run_task_result)


# ── 公共入口：run_task_result 必须真的走 kernel 并返回 verdict ──


class _FakeHarnessRun:
    def __init__(self, ledger_path):
        self.task_id = "task-1"
        self.ledger_path = ledger_path
        self.result = _FakeResult()


def test_run_task_result_returns_an_outcome(tmp_path, monkeypatch):
    from unittest.mock import patch

    import xenon

    ledger = tmp_path / "t.jsonl"
    ledger.write_text("", encoding="utf-8")

    with patch("xenon.harness.runner.run_task", lambda *a, **k: _FakeHarnessRun(ledger)):
        outcome = xenon.run_task_result(
            "do it",
            workspace=str(tmp_path),
            model_priority=["x/y"],
        )

    assert isinstance(outcome, TaskOutcome)
    assert outcome.accepted is True
    assert str(outcome) == "done"


def test_run_task_result_forwards_the_permission(tmp_path):
    from unittest.mock import patch

    import xenon

    captured: dict = {}
    ledger = tmp_path / "t.jsonl"
    ledger.write_text("", encoding="utf-8")

    def fake_run(objective, **kwargs):
        captured.update(kwargs)
        return _FakeHarnessRun(ledger)

    with patch("xenon.harness.runner.run_task", fake_run):
        xenon.run_task_result(
            "do it",
            workspace=str(tmp_path),
            model_priority=["x/y"],
            permission="bypass",
            allow_approval=True,
        )

    assert captured["permission"].value == "bypass"
    assert captured["allow_approval"] is True
    assert captured["finalize"] is False


def test_run_task_still_returns_a_string(tmp_path):
    from unittest.mock import patch

    import xenon

    ledger = tmp_path / "t.jsonl"
    ledger.write_text("", encoding="utf-8")

    with patch("xenon.harness.runner.run_task", lambda *a, **k: _FakeHarnessRun(ledger)):
        answer = xenon.run_task("do it", workspace=str(tmp_path), model_priority=["x/y"])

    assert isinstance(answer, str)
    assert answer == "done"


def test_allow_approval_is_forwarded_by_default_false(tmp_path):
    from unittest.mock import patch

    import xenon

    captured: dict = {}
    ledger = tmp_path / "t.jsonl"
    ledger.write_text("", encoding="utf-8")

    def fake_run(objective, **kwargs):
        captured.update(kwargs)
        return _FakeHarnessRun(ledger)

    with patch("xenon.harness.runner.run_task", fake_run):
        xenon.run_task("x", workspace=str(tmp_path), model_priority=["x/y"])

    assert captured["allow_approval"] is False
    assert captured["permission"].value == "auto-edit"
