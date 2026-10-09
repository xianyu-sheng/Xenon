"""TurnGate：回合级唯一校验层（pass/fail/fuse 状态机）。"""

from __future__ import annotations

from xenon.engine.registry import EngineSpec
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.repl import REPL
from xenon.turn.gate import (
    VERDICT_FAIL,
    VERDICT_FUSE,
    VERDICT_PASS,
    TurnGate,
    tool_events_from_panel,
)


def test_pass_on_clean_answer():
    gate = TurnGate()
    verdict = gate.evaluate(
        "已完成", tool_events=[{"tool": "read_file", "success": True}]
    )
    assert verdict.outcome == VERDICT_PASS
    assert verdict.reasons == []


def test_fail_then_pass_resets_rounds():
    gate = TurnGate()
    bad = [{"tool": "command", "success": False, "error": "pytest failed"}]
    assert gate.evaluate("已完成 ✅ 任务完成", tool_events=bad).outcome == VERDICT_FAIL
    assert gate.rounds_used == 1
    good = [{"tool": "command", "success": True}]
    assert gate.evaluate("修复完成", tool_events=good).outcome == VERDICT_PASS
    assert gate.rounds_used == 0  # pass 后清零


def test_same_cause_failures_fuse():
    gate = TurnGate(max_same_cause_failures=2)
    bad = [{"tool": "command", "success": False, "error": "pytest failed"}]
    assert gate.evaluate("任务完成", tool_events=bad).outcome == VERDICT_FAIL
    verdict = gate.evaluate("任务完成", tool_events=bad)
    assert verdict.outcome == VERDICT_FUSE
    assert any("熔断" in r for r in verdict.reasons)


def test_different_causes_do_not_fuse():
    gate = TurnGate(max_same_cause_failures=2)
    bad = [{"tool": "command", "success": False, "error": "x"}]
    assert (
        gate.evaluate("任务完成", tool_events=bad, criteria=["CI 通过"]).outcome
        == VERDICT_FAIL
    )
    assert (
        gate.evaluate(
            "任务完成", tool_events=bad, criteria=["测试全部通过"]
        ).outcome
        == VERDICT_FAIL
    )
    assert gate.rounds_used == 1  # 原因不同 → 重新计数


def test_tool_events_from_panel():
    class _Step:
        action = "command"
        is_error = True
        observation = "exit 1"

    class _Panel:
        steps = [_Step()]

    events = tool_events_from_panel(_Panel())
    assert events == [
        {"tool": "command", "success": False, "error": "exit 1"},
    ]


def test_verify_turn_stashes_verdict(monkeypatch, tmp_path):
    monkeypatch.setenv("XENON_SESSION_EVENTS_DIR", str(tmp_path))
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    repl = REPL(registry=registry, streaming=False)
    repl._last_user_text = "帮我看看这个项目"

    class _Step:
        action = "command"
        is_error = True
        observation = "pytest failed"

    class _Panel:
        steps = [_Step()]

    ok, reasons = repl._verify_turn(_Panel(), "任务完成")
    assert ok is False
    assert repl._last_gate_verdict is not None
    assert repl._last_gate_verdict.outcome == VERDICT_FAIL


class _Step:
    def __init__(self, is_error: bool):
        self.action = "command"
        self.is_error = is_error
        self.observation = "pytest failed" if is_error else ""
        self.action_input = {"command": "pytest"}


class _Panel:
    def __init__(self, failing: bool):
        self.steps = [_Step(failing)]
        self.errors: list = []
        self.tool_call_count = 1


class _Callback:
    def __init__(self, panel):
        self._panel = panel

    def finish_activity(self):
        pass

    def get_thinking_panel(self):
        return self._panel


class _FakeEngine:
    def __init__(self, answer: str):
        self.answer = answer

    def run(self, prompt, context=None, ctx_mgr=None):
        self.prompt = prompt
        return self.answer


def _setup_retry_harness(monkeypatch, tmp_path, answers):
    monkeypatch.setenv("XENON_SESSION_EVENTS_DIR", str(tmp_path))
    monkeypatch.setenv("XENON_VERIFY_RETRIES", "3")
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    repl = REPL(registry=registry, streaming=False)
    repl._last_user_text = "帮我看看这个项目"

    engines = [_FakeEngine(a) for a in answers]
    callbacks = [_Callback(_Panel(True)) for _ in answers]
    spec = EngineSpec(
        name="fake",
        mode_line="",
        factory=lambda **kw: engines.pop(0),
        result_title="",
    )
    monkeypatch.setattr(repl, "_make_callback", lambda: callbacks.pop(0))
    monkeypatch.setattr(repl, "_start_log_capture", lambda: None)
    monkeypatch.setattr(repl, "_stop_log_capture", lambda: "")
    monkeypatch.setattr(repl, "_persist_engine_trace", lambda e: None)
    monkeypatch.setattr(repl, "_inject_mcp_tools_into_engine", lambda e: None)
    monkeypatch.setattr(repl, "_bind_interactive_tool_runtime", lambda e: None)
    monkeypatch.setattr(repl, "_engine_model_used", lambda e, ids: None)
    monkeypatch.setattr(repl, "_start_steering_listener", lambda e: None)
    monkeypatch.setattr(repl, "_stop_steering_listener", lambda t: None)

    class _Ctx:
        def __init__(self):
            self.user: list = []
            self.assistant: list = []

        def add_user_message(self, content, **kw):
            self.user.append(content)

        def add_assistant_message(self, content, **kw):
            self.assistant.append(content)

    ctx = _Ctx()
    monkeypatch.setattr(repl, "ctx_mgr", ctx)
    monkeypatch.setattr(
        repl, "_render_engine_result", lambda cb, res, title: None
    )
    monkeypatch.setattr(
        repl,
        "auto_router",
        type("_R", (), {"record_model_success": lambda self, m: None})(),
    )
    return repl, spec, engines, ctx


def test_run_engine_fuses_on_same_cause_instead_of_endless_retries(
    monkeypatch, tmp_path
):
    """同因失败 → 第二轮判定即熔断：不再构造第三个引擎。"""

    repl, spec, engines, ctx = _setup_retry_harness(
        monkeypatch, tmp_path, ["任务完成 ✅", "任务完成 ✅", "任务完成 ✅"]
    )
    repl._run_engine(spec, "帮我看看这个项目", ["openai/test"])

    assert len(engines) == 1  # 只消耗 2 个引擎（首轮 + 一次重试后熔断）
    assert repl._turn_tree.tail().status == "fused"
    events = repl._session_events.read()
    assert any(e["type"] == "verification/fused" for e in events)
    assert sum(1 for e in events if e["type"] == "verification/retry") == 1
