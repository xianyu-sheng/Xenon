"""Crash, corruption, and sustained-checkpoint recovery tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import textwrap

from xenon.engine.context import AgentContext
from xenon.nodes.tool_executor import recover_tool_execution_checkpoint
from xenon.repl.commands import dispatch_command
from xenon.repl.context_manager import ContextManager
from xenon.repl.model_pool import ModelPool
from xenon.repl.model_registry import ModelRegistry


def test_sigkill_recovers_all_parallel_tools_without_replaying(tmp_path, monkeypatch):
    """A hard-killed worker leaves two durable, privacy-safe active entries."""
    sessions = tmp_path / "sessions"
    script = textwrap.dedent(
        """
        import os
        from pathlib import Path
        import threading
        import time

        from xenon.engine.context import AgentContext
        from xenon.nodes import tool_executor as executor_module
        from xenon.nodes.tool_executor import ToolExecutor
        import xenon.repl.session as session_module

        session_module.SESSIONS_DIR = Path(os.environ["XENON_CHAOS_SESSIONS"])

        class HangingNode:
            def __init__(self, _name, action_type=None, **_params):
                self.action_type = action_type

            @staticmethod
            def normalize_params(params, **_kwargs):
                return params

            def execute(self, _context):
                time.sleep(30)
                return {"success": True, "content": "too late"}

        executor_module.ToolNode = HangingNode
        context = AgentContext()
        announced = threading.Event()

        def persist(_checkpoint):
            session_module.auto_save([], context.to_dict(), {})
            active = context.get("_tool_execution_active", {})
            if len(active) == 2 and not announced.is_set():
                announced.set()
                print("BOTH-CHECKPOINTED", flush=True)

        context.set_tool_checkpoint_callback(persist)
        executor = ToolExecutor()
        jobs = [
            ("read_file", {"file_path": "/private/input.txt"}),
            ("write_file", {
                "file_path": "/private/output.txt",
                "content": "must-never-be-persisted",
            }),
        ]
        for tool_name, params in jobs:
            threading.Thread(
                target=executor.execute,
                args=(tool_name, params, context),
                kwargs={"tools": {tool_name: {}}},
                daemon=True,
            ).start()

        if not announced.wait(5):
            raise RuntimeError("parallel checkpoints were not persisted")
        time.sleep(30)
        """
    )
    env = os.environ.copy()
    env["XENON_CHAOS_SESSIONS"] = str(sessions)
    env["HOME"] = str(tmp_path / "home")
    python = shutil.which("python3") or "python3"
    process = subprocess.Popen(
        [python, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "BOTH-CHECKPOINTED"
        process.kill()
        process.wait(timeout=3)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)

    import xenon.repl.session as session_module

    monkeypatch.setattr(session_module, "SESSIONS_DIR", sessions)
    saved = session_module.load_session("_auto")
    raw = (sessions / "_auto.json").read_text(encoding="utf-8")
    assert "/private/input.txt" not in raw
    assert "/private/output.txt" not in raw
    assert "must-never-be-persisted" not in raw

    context = AgentContext(initial=saved["context"])
    notice = recover_tool_execution_checkpoint(context)

    assert "2 个" in notice
    assert "read_file" in notice
    assert "write_file" in notice
    assert context.get("_tool_execution_active") == {}
    assert process.returncode is not None and process.returncode < 0


def test_corrupt_resume_fails_closed_without_replacing_current_state(
    tmp_path,
    monkeypatch,
):
    import xenon.repl.session as session_module

    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)
    (tmp_path / "broken.json").write_text('{"history": [', encoding="utf-8")
    ctx_mgr = ContextManager()
    ctx_mgr.add_user_message("current conversation must survive")
    original_context = AgentContext({"sentinel": "preserved"})

    class _ReplStub:
        def __init__(self):
            self.ctx_mgr = ctx_mgr
            self.agent_context = original_context
            self._session_state = {"agent_context": original_context}
            self.registry = ModelRegistry()
            self.model_pool = ModelPool()

    repl = _ReplStub()
    result = dispatch_command(
        "/resume",
        "broken",
        registry=repl.registry,
        ctx_mgr=ctx_mgr,
        session_state={"_repl": repl},
    )

    assert result is not None and "命令执行失败" in result
    assert "Traceback" not in result
    assert repl.agent_context is original_context
    assert repl.agent_context.get("sentinel") == "preserved"
    assert ctx_mgr.history[-1].content == "current conversation must survive"


def test_malformed_active_key_is_migrated_and_removed():
    context = AgentContext(initial={
        "_tool_execution_active": {
            "legacy-execution-key": {
                "tool_name": "read_file",
                "tool_class": "INFO",
                "state": "running",
            }
        },
        "_tool_execution_checkpoint": {
            "tool_name": "read_file",
            "tool_class": "INFO",
            "state": "running",
        },
    })

    notice = recover_tool_execution_checkpoint(context)

    assert "1 个" in notice
    assert context.get("_tool_execution_active") == {}
    assert context.get("_tool_execution_checkpoint")["execution_id"] == (
        "legacy-execution-key"
    )


def test_session_listing_skips_wrong_field_types_without_hiding_good_sessions(
    tmp_path,
    monkeypatch,
):
    import xenon.repl.session as session_module

    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)
    session_module.save_session(
        "good",
        history=[{"role": "user", "content": "keep"}],
        context_store={},
        model_config={},
    )
    (tmp_path / "bad.json").write_text(
        json.dumps({
            "name": "bad",
            "history": [{"role": "user", "content": "bad"}],
            "extra": "wrong-type",
            "saved_at_ts": "not-a-number",
        }),
        encoding="utf-8",
    )

    sessions = session_module.list_sessions()

    assert {session["name"] for session in sessions} == {"good", "bad"}
    bad = next(session for session in sessions if session["name"] == "bad")
    assert bad["paradigm"] == ""
    assert bad["saved_at_ts"] == 0


def test_invalid_message_shape_is_rejected_before_current_history_is_cleared(
    tmp_path,
    monkeypatch,
):
    import xenon.repl.session as session_module

    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)
    (tmp_path / "invalid-message.json").write_text(
        json.dumps({
            "name": "invalid-message",
            "history": ["not-a-message"],
            "context": {},
            "model_config": {},
            "extra": {},
        }),
        encoding="utf-8",
    )
    ctx_mgr = ContextManager()
    ctx_mgr.add_user_message("current conversation must survive")

    class _ReplStub:
        def __init__(self):
            self.ctx_mgr = ctx_mgr
            self.agent_context = AgentContext({"sentinel": "preserved"})
            self._session_state = {"agent_context": self.agent_context}
            self.registry = ModelRegistry()
            self.model_pool = ModelPool()

    repl = _ReplStub()
    result = dispatch_command(
        "/resume",
        "invalid-message",
        registry=repl.registry,
        ctx_mgr=ctx_mgr,
        session_state={"_repl": repl},
    )

    assert result == "❌ 恢复失败: 会话消息格式无效。"
    assert ctx_mgr.history[-1].content == "current conversation must survive"
    assert repl.agent_context.get("sentinel") == "preserved"


def test_six_thousand_transitions_keep_recovery_state_bounded():
    context = AgentContext()

    for index in range(2_000):
        base = {
            "execution_id": f"execution-{index}",
            "tool_name": "read_file",
            "tool_class": "INFO",
        }
        context.record_tool_checkpoint({**base, "state": "pending"})
        context.record_tool_checkpoint({**base, "state": "running"})
        context.record_tool_checkpoint({**base, "state": "succeeded"})

    stored = context.to_dict()
    encoded = json.dumps(stored)
    assert stored["_tool_execution_active"] == {}
    assert len(stored["_tool_execution_history"]) == 32
    assert len(encoded) < 20_000
