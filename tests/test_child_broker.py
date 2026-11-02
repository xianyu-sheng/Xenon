"""Brokered approval for children (#356).

The rule under test: a child can never obtain authority on its own.  Every
request goes to the parent's provider, and "no answer" means no.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from xenon.harness.child_broker import (
    BrokerApprovalProvider,
    serve_requests,
)
from xenon.kernel.policies import PermissionMode


class _Intent:
    def __init__(self, capability: PermissionMode = PermissionMode.AUTO_EDIT) -> None:
        self.capability = capability
        self.params = {"file_path": "calc.py"}


class _Action:
    def __init__(self) -> None:
        self.intent = _Intent()


class _Yes:
    def __init__(self) -> None:
        self.seen: list[dict] = []

    def approve_request(self, request):
        self.seen.append(request)
        return True, "approved by human"


class _No:
    def approve_request(self, request):
        return False, "user said no"


class _Boom:
    def approve_request(self, request):
        raise RuntimeError("ui exploded")


def _run_broker_in_thread(directory: Path, provider, result: list, wait=5.0) -> threading.Thread:
    child = BrokerApprovalProvider(directory, wait_seconds=wait)

    def call() -> None:
        result.append(child.request(None, _Action(), None))

    thread = threading.Thread(target=call)
    thread.start()
    return thread


def test_a_request_is_answered_by_the_parent(tmp_path: Path) -> None:
    provider = _Yes()
    result: list[bool] = []
    thread = _run_broker_in_thread(tmp_path / "broker", provider, result)

    # Parent side: service requests while the child waits.
    served: list = []
    while thread.is_alive() and not served:
        served = serve_requests(tmp_path / "broker", provider)
    thread.join(timeout=10)

    assert result == [True]
    assert served and served[0].approved is True
    assert provider.seen[0]["params"] == {"file_path": "calc.py"}


def test_the_parent_can_deny(tmp_path: Path) -> None:
    result: list[bool] = []
    thread = _run_broker_in_thread(tmp_path / "broker", _No(), result)

    while thread.is_alive() and not serve_requests(tmp_path / "broker", _No()):
        pass
    thread.join(timeout=10)

    assert result == [False]


def test_no_provider_means_no_authority(tmp_path: Path) -> None:
    """Fail closed: the request is denied, with a reason."""

    served = serve_requests(tmp_path / "broker", None)  # nothing to answer yet
    assert served == []

    (tmp_path / "broker").mkdir(parents=True, exist_ok=True)
    (tmp_path / "broker" / "req-1-1.json").write_text(
        json.dumps({"id": "1-1", "capability": "verify", "tool": "verify"}),
        encoding="utf-8",
    )
    served = serve_requests(tmp_path / "broker", None)

    assert len(served) == 1
    assert served[0].approved is False
    assert "no approval provider" in served[0].reason
    answer = json.loads((tmp_path / "broker" / "resp-1-1.json").read_text())
    assert answer["approved"] is False


def test_a_broken_provider_denies_instead_of_allowing(tmp_path: Path) -> None:
    (tmp_path / "broker").mkdir(parents=True)
    (tmp_path / "broker" / "req-2-1.json").write_text(
        json.dumps({"id": "2-1", "capability": "shell"}), encoding="utf-8"
    )

    served = serve_requests(tmp_path / "broker", _Boom())

    assert served[0].approved is False
    assert "ui exploded" in served[0].reason


def test_a_provider_without_the_hook_cannot_answer(tmp_path: Path) -> None:
    class Wrong:
        pass

    (tmp_path / "broker").mkdir(parents=True)
    (tmp_path / "broker" / "req-3-1.json").write_text(
        json.dumps({"id": "3-1", "capability": "verify"}), encoding="utf-8"
    )

    served = serve_requests(tmp_path / "broker", Wrong())

    assert served[0].approved is False
    assert "cannot answer file requests" in served[0].reason


def test_each_request_is_asked_once(tmp_path: Path) -> None:
    (tmp_path / "broker").mkdir(parents=True)
    (tmp_path / "broker" / "req-4-1.json").write_text(
        json.dumps({"id": "4-1", "capability": "verify"}), encoding="utf-8"
    )
    provider = _Yes()
    seen: set[str] = set()

    first = serve_requests(tmp_path / "broker", provider, seen=seen)
    second = serve_requests(tmp_path / "broker", provider, seen=seen)

    assert len(first) == 1 and second == []
    assert len(provider.seen) == 1


def test_a_child_without_an_answer_denies_after_its_wait(tmp_path: Path) -> None:
    child = BrokerApprovalProvider(tmp_path / "broker", wait_seconds=0.3)

    assert child.request(None, _Action(), None) is False
    assert "no answer from the parent" in child.last_reason


def test_a_non_tool_action_is_never_approved(tmp_path: Path) -> None:
    child = BrokerApprovalProvider(tmp_path / "broker", wait_seconds=0.1)

    assert child.request(None, object(), None) is False
    assert "only covers tool calls" in child.last_reason


def test_serving_a_missing_directory_is_a_no_op(tmp_path: Path) -> None:
    assert serve_requests(tmp_path / "nope", _Yes()) == []


# ── permission subset ────────────────────────────────────────────────


def test_a_child_cannot_escalate_beyond_its_parent(tmp_path: Path) -> None:
    from xenon.harness.child import run_child_task
    from xenon.harness.llm import ModelConfig

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "calc.py").write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="cannot escalate"):
        run_child_task(
            objective="x",
            workspace=ws,
            model=ModelConfig(model_id="test/fake"),
            profile="react",
            permission=PermissionMode.BYPASS,
            parent_permission=PermissionMode.AUTO_EDIT,
        )


def test_a_child_may_be_less_privileged_than_its_parent(tmp_path: Path) -> None:
    from xenon.harness.child import _check_permission_subset

    _check_permission_subset(PermissionMode.READ_ONLY, PermissionMode.BYPASS)
    _check_permission_subset(PermissionMode.AUTO_EDIT, PermissionMode.AUTO_EDIT)


def test_the_repl_agent_delegates_with_the_parent_provider() -> None:
    """`/agent` must not hand a child a session it could not authorize itself."""

    from xenon.harness import child as child_mod
    from xenon.repl.command_groups.agent import _cmd_sub_agent

    captured: dict = {}

    def fake_run(**kwargs):
        captured.update(kwargs)

        class _Outcome:
            child_id = "p-sub1-abc"
            objective = kwargs["objective"]
            profile = kwargs["profile"]
            accepted = True
            phase = "finalized"
            answer = "ok"
            ledger_path = "l.jsonl"
            debt = ()
            tool_calls = 1
            elapsed_seconds = 0.1
            error = None
            pid = 111
            killed = False

            @property
            def delivered(self):
                return True

            def summary(self, **kw):
                return "ok"

        return _Outcome()

    class _Model:
        def get_healthy(self):
            return []

        def list_all(self):
            return []

    class _Entry:
        model_id = "test/fake"

    class _Pool:
        pass

    class _Repl:
        _execution_level = 3
        project_ctx = None
        _last_kernel_task_id = "parent-x"
        _session_events = None
        model_pool = _Model()
        registry = object()

    _Repl.model_pool.get_healthy = lambda: [_Entry()]
    _Repl.model_pool.list_all = lambda: [_Entry()]

    original = child_mod.run_child_task
    child_mod.run_child_task = fake_run
    try:
        import xenon.harness.llm as llm

        original_model = llm.ModelConfig

        def fake_model_config(registry, ids):
            return original_model(model_id="test/fake")

        from xenon.repl import runtime_adapter as ra

        original_cfg = ra.model_config_for
        ra.model_config_for = fake_model_config
        try:
            _cmd_sub_agent(args="do the thing", session_state={}, repl=_Repl())
        finally:
            ra.model_config_for = original_cfg
            llm.ModelConfig = original_model
    finally:
        child_mod.run_child_task = original

    assert captured["approval"] is not None, "child must be able to ask"
    assert captured["parent_permission"] is not None
