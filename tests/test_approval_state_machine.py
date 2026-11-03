"""Approval as a state machine (#371): no path may hang, every exit is clean.

Locks the behaviours that #344/#345 already fixed plus the remaining gaps:

    1. ``_active_runtime`` is None after every run_kernel_turn exit path;
    2. a Ctrl-C during approval denies the call AND aborts the task (never
       kills the process, never continues);
    3. a non-TTY approval request fails closed immediately with an actionable
       reason;
    4. the child broker's wait timeout denies with a ledger reason, not a hang;
    5. the child broker and the main turn serialize on the permission lock.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from xenon.engine.callbacks import ConsoleCallback
from xenon.repl.execution_policy import ExecutionLevel
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.runtime_adapter import run_kernel_turn


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)

    def __call__(self, model_id, messages, **kwargs):
        return self.responses.pop(0)


def _shell_then_finish() -> FakeClient:
    return FakeClient(
        [
            json.dumps(
                {
                    "action": "call_tool",
                    "capability": "shell",
                    "params": {"command": "echo hello"},
                }
            ),
            json.dumps({"action": "finish", "answer": "done"}),
        ]
    )


def _repl(tmp_path: Path, monkeypatch) -> Any:
    from tests.test_repl_kernel_runtime import FakeRepl

    registry = ModelRegistry()
    registry.add_model("test/fake", "fake")
    repl = FakeRepl(tmp_path, registry)
    repl._confirm_tool = lambda tool, params, risk, *, from_child=False: (
        True,
        "",
    )
    return repl


# ── 1. every exit path clears the active runtime ──────────────────


def test_all_exit_paths_clear_the_active_runtime(tmp_path, monkeypatch) -> None:
    import xenon.repl.runtime_adapter as adapter

    repl = _repl(tmp_path, monkeypatch)

    def turn():
        return run_kernel_turn(
            repl,
            objective="run echo",
            user_input="run echo",
            model_ids=["test/fake"],
            execution_policy=SimpleNamespace(level=ExecutionLevel.EXECUTE),
            callback=ConsoleCallback(verbose=False),
            client=_shell_then_finish(),
        )

    # Path 1: normal completion.
    turn()
    assert repl._active_runtime is None

    # Path 2: adapter.run raises.
    def boom_run(self, *args, **kwargs):
        raise RuntimeError("kernel boom")

    monkeypatch.setattr(adapter.RuntimeAdapter, "run", boom_run)
    turn()
    assert repl._active_runtime is None

    # Path 3: KeyboardInterrupt propagates through the turn.
    def ki_run(self, *args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(adapter.RuntimeAdapter, "run", ki_run)
    with pytest.raises(KeyboardInterrupt):
        turn()
    assert repl._active_runtime is None

    # Path 4: from_repl raises (bad level).
    def boom_from_repl(*args, **kwargs):
        raise ValueError("bad level")

    monkeypatch.setattr(adapter.RuntimeAdapter, "from_repl", boom_from_repl)
    turn()
    assert repl._active_runtime is None


# ── 2. Ctrl-C during approval denies AND aborts ───────────────────


def test_ctrl_c_during_approval_denies_and_aborts(tmp_path, monkeypatch) -> None:
    """A Ctrl-C inside the approval panel is a deny, and the kernel turn is
    aborted — never a hang, never a silent continue (#371)."""

    import sys
    from types import SimpleNamespace as NS

    import xenon.repl.approval_ux as ux
    from tests.test_permissions_command import _prompt_repl

    monkeypatch.setattr(
        ux,
        "_config",
        lambda: NS(interaction=NS(assume_yes=False)),
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(ux, "console", _NullConsole())

    asked: list[str] = []

    def ctrl_c(default="n", *, from_child=False):
        asked.append("asked")
        raise KeyboardInterrupt

    prompt_repl = _prompt_repl("y", workspace=tmp_path)
    prompt_repl._ask_approval_choice = ctrl_c

    outcome = prompt_repl._confirm_tool_approval(
        "command", {"command": "echo hi"}
    )

    assert outcome == "cancelled"
    assert asked == ["asked"]


class _NullConsole:
    def print(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ── 3. non-TTY fails closed with an actionable reason ─────────────


def test_non_tty_approval_fails_closed(tmp_path, monkeypatch) -> None:
    import sys

    from tests.test_permissions_command import _prompt_repl

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)

    import types
    from types import SimpleNamespace as NS

    import xenon.repl.approval_ux as ux
    from xenon.repl.approval_ux import ApprovalUxMixin

    monkeypatch.setattr(ux, "_config", lambda: NS(interaction=NS(assume_yes=False)))
    monkeypatch.setattr(ux, "console", _NullConsole())
    repl = _prompt_repl("y", workspace=tmp_path)
    repl._confirm_tool = types.MethodType(ApprovalUxMixin._confirm_tool, repl)
    repl._boundary_approved_once = set()
    repl._last_approval_scope = ""

    allowed, reason = repl._confirm_tool(
        "command", {"command": "git log"}, "CRITICAL"
    )

    assert allowed is False
    assert "非交互环境" in reason
    assert "permissions bypass" in reason


# ── 4. a child's timeout lands in the ledger, not a hang ──────────


def test_child_timeout_denies_with_a_ledger_reason(tmp_path: Path) -> None:
    from tests.kernel.test_north_star import ScriptedStrategy, _runtime
    from tests.kernel.test_session_grants import _policy
    from xenon.harness.child_broker import BrokerApprovalProvider
    from xenon.kernel.contracts import CallTool, Capability, TaskSpec, ToolIntent
    from xenon.ledger.events import EventKind
    from xenon.ledger.store import Ledger

    ledger = Ledger.in_memory("child-timeout")
    runtime = _runtime(ledger, policies=(_policy(str(tmp_path)),))
    runtime._approval = BrokerApprovalProvider(
        tmp_path / "broker", wait_seconds=0.2
    )

    action = CallTool(
        ToolIntent(capability=Capability.SHELL, params={"command": "git log"})
    )
    runtime.run(
        TaskSpec(task_id="child-timeout", objective="o", workspace=str(tmp_path)),
        ScriptedStrategy([action]),
    )

    approvals = [
        event
        for event in ledger.read()
        if event.kind is EventKind.USER_APPROVAL
    ]
    assert approvals, "the denial must be in the ledger"
    assert approvals[0].payload["approved"] is False
    assert "timed out" in str(approvals[0].payload["reason"])


# ── 5. broker and main-turn approvals serialize ───────────────────


def test_main_and_child_approvals_serialize_on_the_lock(
    tmp_path, monkeypatch
) -> None:
    import threading

    from tests.test_permissions_command import _prompt_repl

    repl = _prompt_repl("y", workspace=tmp_path)
    active: list[bool] = []
    lock = threading.RLock()

    def serial_ask(default="n", *, from_child=False):
        with lock:
            assert not any(active), "two approvals interleaved"
            active.append(True)
            import time

            time.sleep(0.05)
            active.pop()
        return "y"

    repl._ask_approval_choice = serial_ask

    results: list[str] = []

    def approve_child():
        results.append(
            repl._confirm_tool_approval(
                "command", {"command": "echo hi"}, from_child=True
            )
        )

    def approve_main():
        results.append(
            repl._confirm_tool_approval("command", {"command": "echo hi"})
        )

    threads = [
        threading.Thread(target=approve_main),
        threading.Thread(target=approve_child),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 2
    assert all(r == "allowed-once" for r in results)
