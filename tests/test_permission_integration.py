"""Authorization tests for the real REPL execution path.

The legacy ``PermissionGate`` these used to exercise was removed: the kernel
never injected it into the executor, so it decided nothing while still owning
``/permissions``.  What remains here are the checks that are about the *live*
path -- cancellation ending a turn, runtime-registered tools being classified
as sensitive, and escalation running on the kernel with the turn's real level.
``tests/kernel/test_permissions.py`` owns the policy semantics themselves.
"""

from __future__ import annotations

from xenon.nodes.tool_executor import classify_tool


def test_cancel_stops_without_a_second_model_turn():
    """A cancel must end the turn, not buy the model another call."""

    from tests.kernel.test_north_star import (
        ScriptedStrategy,
        _calc_spec,
        _runtime,
    )
    from xenon.kernel.contracts import CallTool, Capability, ToolIntent
    from xenon.ledger.store import Ledger

    ledger = Ledger.in_memory("task-1")
    runtime = _runtime(ledger)
    calls = {"proposed": 0}

    class CancelAfterFirst(ScriptedStrategy):
        def propose(self, view):
            calls["proposed"] += 1
            action = super().propose(view)
            runtime.request_cancel()
            return action

    strategy = CancelAfterFirst([CallTool(ToolIntent(capability=Capability.READ))])
    result = runtime.run(_calc_spec(), strategy)

    assert result.phase.value == "aborted"
    assert calls["proposed"] == 1, "a cancelled turn must not ask the model again"


def test_dynamic_tools_are_classified_as_sensitive(monkeypatch):
    """A tool registered at runtime cannot hide behind a static name list.

    ``classify_tool`` is what the executor reports upward, and it is what makes
    a dynamically-registered tool subject to approval.  The legacy gate's own
    risk table could not see such tools at all -- which is why the executor
    overrides the risk instead of trusting a name list.
    """
    from xenon.nodes import tool_node

    monkeypatch.setitem(
        tool_node._DYNAMIC_TOOLS,
        "custom_side_effect",
        {
            "handler": lambda context: {"success": True},
            "description": "test",
            "params": {},
        },
    )

    assert classify_tool("custom_side_effect") == "SENSITIVE"


def test_escalation_runs_on_the_kernel_with_the_live_level(monkeypatch):
    """Escalation is a tool turn: it runs on the loop with the live level."""

    from xenon.repl.execution_policy import ExecutionLevel
    from xenon.repl.model_registry import ModelRegistry
    from xenon.repl.repl import REPL

    registry = ModelRegistry()
    registry.add_model("openai/gpt-4o", "gpt4")
    repl = REPL(registry=registry, streaming=False)
    captured: dict = {}

    def fake_kernel(owner, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(
        "xenon.repl.runtime_adapter.run_kernel_turn", fake_kernel
    )
    repl.agent_context.update({"_execution_level": int(ExecutionLevel.WRITE)})

    repl._run_react_engine("帮我创建 hello.py", ["openai/gpt-4o"])

    policy = captured["execution_policy"]
    assert policy.level is ExecutionLevel.WRITE
    assert captured["objective"].startswith("帮我创建 hello.py") or (
        "帮我创建 hello.py" in captured["objective"]
    )
