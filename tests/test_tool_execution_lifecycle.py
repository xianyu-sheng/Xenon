"""Tool execution lifecycle, durable checkpoints, and safe recovery."""

from __future__ import annotations

from xenon.engine.context import AgentContext
from xenon.engine.tool_tracker import ToolExecutionTracker
from xenon.nodes import tool_executor as executor_module
from xenon.nodes.tool_executor import (
    ToolExecutionState,
    ToolExecutor,
    classify_tool,
    recover_tool_execution_checkpoint,
)
from xenon.repl.permissions import PermissionGate, PermissionMode


class _ScriptedNode:
    script: list[object] = []

    def __init__(self, _name, action_type=None, **_params):
        self.action_type = action_type

    @staticmethod
    def normalize_params(params):
        return params

    def execute(self, _context):
        result = self.script.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _executor(monkeypatch, script, *, retries=2):
    _ScriptedNode.script = list(script)
    monkeypatch.setattr(executor_module, "ToolNode", _ScriptedNode)
    return ToolExecutor(retry_attempts=retries)


def test_read_only_retry_has_observable_state_transitions(monkeypatch):
    executor = _executor(
        monkeypatch,
        [
            {"success": False, "error": "connection timeout"},
            {"success": True, "content": "ok"},
        ],
    )
    context = AgentContext()
    tracker = ToolExecutionTracker()

    result = executor.execute(
        "read_file",
        {"file_path": "/private/input.txt", "content": "do-not-persist"},
        context,
        tracker=tracker,
        tools={"read_file": {}},
    )

    assert result.success is True
    assert result.state is ToolExecutionState.SUCCEEDED
    assert result.attempts == 2
    assert [event["state"] for event in result.lifecycle] == [
        "pending",
        "running",
        "retrying",
        "running",
        "succeeded",
    ]
    assert tracker.calls[0].state == "succeeded"
    assert tracker.calls[0].attempts == 2
    assert context.get("_tool_execution_checkpoint")["state"] == "succeeded"

    persisted = str(context.get("_tool_execution_history"))
    assert "/private/input.txt" not in persisted
    assert "do-not-persist" not in persisted
    assert "parameter_names" in persisted


def test_mcp_lifecycle_uses_remote_operation_semantics():
    assert classify_tool("mcp_call", {"tool_name": "train_query"}) == "INFO"
    assert classify_tool("mcp_call", {"tool_name": "record_create"}) == "WRITE"
    assert classify_tool("mcp_call", {"tool_name": "shell_execute"}) == "SENSITIVE"
    assert classify_tool("mcp_call", {"tool_name": "opaque_remote"}) == "SENSITIVE"


def test_read_only_mcp_call_does_not_request_write_confirmation(monkeypatch):
    confirmations: list[str] = []
    gate = PermissionGate(PermissionMode.DEFAULT)
    gate.set_confirm_callback(
        lambda tool_name, _params, _risk: confirmations.append(tool_name) or True
    )
    executor = _executor(
        monkeypatch,
        [{"success": True, "content": "trains"}],
    )

    result = executor.execute(
        "mcp_call",
        {"tool_name": "train_query", "arguments": {"from": "昆山"}},
        AgentContext(),
        tools={"mcp_call": {}},
    )

    assert result.success is True
    assert result.tool_class == "INFO"
    assert confirmations == []


def test_stateful_timeout_is_not_replayed_or_marked_recoverable(monkeypatch):
    executor = _executor(
        monkeypatch,
        [
            {"success": False, "error": "命令执行超时 (1s): destructive-command"},
            {"success": True, "content": "must not run"},
        ],
        retries=3,
    )
    context = AgentContext()

    result = executor.execute(
        "command",
        {"action": "destructive-command"},
        context,
        tools={"command": {}},
    )

    assert result.state is ToolExecutionState.TIMED_OUT
    assert result.timed_out is True
    assert result.attempts == 1
    assert result.retryable is False
    assert result.recoverable is False
    assert len(_ScriptedNode.script) == 1
    checkpoint = context.get("_tool_execution_checkpoint")
    assert checkpoint["resume_action"] == "manual_verification"
    assert checkpoint["status_unknown"] is True
    assert "不得自动重试" in result.observation
    assert "先核验副作用" in result.next_hint()
    assert "destructive-command" not in str(checkpoint)


def test_keyboard_interrupt_becomes_task_cancellation(monkeypatch):
    executor = _executor(monkeypatch, [KeyboardInterrupt()])
    context = AgentContext()

    result = executor.execute(
        "read_file",
        {"file_path": "README.md"},
        context,
        tools={"read_file": {}},
    )

    assert result.state is ToolExecutionState.CANCELLED
    assert result.cancelled is True
    assert context.get("_task_cancelled") is True


def test_checkpoint_callback_runs_during_execution_and_history_is_bounded(monkeypatch):
    observed: list[str] = []
    context = AgentContext()
    context.set_tool_checkpoint_callback(
        lambda checkpoint: observed.append(checkpoint["state"])
    )
    executor = _executor(
        monkeypatch,
        [{"success": True, "content": "ok"}],
    )

    executor.execute(
        "read_file",
        {"file_path": "README.md"},
        context,
        tools={"read_file": {}},
    )

    assert observed == ["pending", "running", "succeeded"]
    for index in range(40):
        context.record_tool_checkpoint({"state": "test", "index": index})
    history = context.get("_tool_execution_history")
    assert len(history) == 32
    assert history[0]["index"] == 8
    assert history[-1]["index"] == 39


def test_lifecycle_callback_persists_each_transition_to_session(
    monkeypatch,
    tmp_path,
):
    import xenon.repl.session as session_module

    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)
    context = AgentContext()
    persisted_states: list[str] = []

    def persist(checkpoint):
        persisted_states.append(checkpoint["state"])
        session_module.auto_save([], context.to_dict(), {})

    context.set_tool_checkpoint_callback(persist)
    executor = _executor(
        monkeypatch,
        [{"success": True, "content": "ok"}],
    )
    executor.execute(
        "read_file",
        {"file_path": "README.md"},
        context,
        tools={"read_file": {}},
    )

    saved = session_module.load_session("_auto")
    assert persisted_states == ["pending", "running", "succeeded"]
    assert saved["context"]["_tool_execution_checkpoint"]["state"] == "succeeded"


def test_recovery_never_replays_and_distinguishes_read_from_write():
    read_context = AgentContext(initial={
        "_tool_execution_checkpoint": {
            "tool_name": "web_fetch",
            "tool_class": "INFO",
            "state": "running",
        }
    })
    read_notice = recover_tool_execution_checkpoint(read_context)
    read_checkpoint = read_context.get("_tool_execution_checkpoint")

    assert read_checkpoint["state"] == "interrupted"
    assert read_checkpoint["retryable"] is True
    assert read_checkpoint["resume_action"] == "retry"
    assert "未自动重放" in read_notice

    write_context = AgentContext(initial={
        "_tool_execution_checkpoint": {
            "tool_name": "write_file",
            "tool_class": "WRITE",
            "state": "retrying",
        }
    })
    write_notice = recover_tool_execution_checkpoint(write_context)
    write_checkpoint = write_context.get("_tool_execution_checkpoint")

    assert write_checkpoint["state"] == "interrupted"
    assert write_checkpoint["retryable"] is False
    assert write_checkpoint["resume_action"] == "manual_verification"
    assert "人工核验" in write_notice


def test_resume_command_surfaces_interrupted_stateful_tool(
    monkeypatch,
    tmp_path,
):
    import xenon.repl.session as session_module
    from xenon.repl.commands import dispatch_command
    from xenon.repl.context_manager import ContextManager
    from xenon.repl.model_pool import ModelPool
    from xenon.repl.model_registry import ModelRegistry

    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)
    session_module.save_session(
        "interrupted",
        history=[],
        context_store={
            "_tool_execution_checkpoint": {
                "tool_name": "git",
                "tool_class": "WRITE",
                "state": "running",
            }
        },
        model_config={},
    )

    class _ReplStub:
        def __init__(self):
            self.ctx_mgr = ContextManager()
            self.agent_context = AgentContext()
            self._session_state = {"agent_context": self.agent_context}
            self.registry = ModelRegistry()
            self.model_pool = ModelPool()

        @staticmethod
        def _persist_tool_checkpoint(_checkpoint):
            return None

    repl = _ReplStub()
    result = dispatch_command(
        "/resume",
        "interrupted",
        registry=repl.registry,
        ctx_mgr=repl.ctx_mgr,
        session_state={"_repl": repl},
    )

    assert "已恢复会话" in result
    assert "未自动重放" in result
    assert "人工核验" in result
    assert repl.agent_context.get("_tool_execution_checkpoint")["state"] == (
        "interrupted"
    )
