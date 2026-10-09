"""发布门 v3：校验失败 → 同回合反馈重试（验证-执行闭环）。

两件事：
1. 收紧成功宣称规则：裸 ✅ / 表格里的"✅ 优秀"不再是任务成功宣称；
2. 校验未通过时，`_run_engine` 在同一回合内把失败原因反馈给新引擎
   重跑（上限 XENON_VERIFY_RETRIES，默认 2），仍失败才按草稿发布。
"""

from __future__ import annotations

from xenon.engine.registry import EngineSpec
from xenon.engine.task_verifier import verify_final_answer
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.repl import REPL, _verify_retries_limit


def test_verify_retries_limit_default_and_env(monkeypatch):
    monkeypatch.delenv("XENON_VERIFY_RETRIES", raising=False)
    assert _verify_retries_limit() == 2  # 默认 2 次

    monkeypatch.setenv("XENON_VERIFY_RETRIES", "5")
    assert _verify_retries_limit() == 5

    monkeypatch.setenv("XENON_VERIFY_RETRIES", "garbage")
    assert _verify_retries_limit() == 2  # 无效值回退默认

    monkeypatch.setenv("XENON_VERIFY_RETRIES", "-3")
    assert _verify_retries_limit() == 0  # 下限 0（关闭重试）


def test_bare_checkmark_in_table_is_not_a_success_claim():
    ok, reasons = verify_final_answer(
        "| 架构设计 | ✅ 优秀 |\n| 测试 | 🟡 数量可观，覆盖待定 |",
        tool_events=[{"tool": "command", "success": False, "error": "x"}],
    )
    assert ok is True
    assert reasons == []


def test_task_level_success_claim_still_blocked():
    ok, reasons = verify_final_answer(
        "任务已完成",
        tool_events=[{"tool": "command", "success": False, "error": "x"}],
    )
    assert ok is False
    assert any("宣称成功" in r for r in reasons)


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


def test_run_engine_retries_when_verification_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("XENON_SESSION_EVENTS_DIR", str(tmp_path))
    monkeypatch.setenv("XENON_VERIFY_RETRIES", "2")
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    repl = REPL(registry=registry, streaming=False)
    repl._last_user_text = "帮我看看这个项目"  # 无完成准则：只由宣称审计规则触发

    engines = [_FakeEngine("任务完成 ✅"), _FakeEngine("修复完成，CI 通过")]
    callbacks = [_Callback(_Panel(True)), _Callback(_Panel(False))]
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
    rendered: list = []
    monkeypatch.setattr(
        repl, "_render_engine_result", lambda cb, res, title: rendered.append(res)
    )
    monkeypatch.setattr(
        repl,
        "auto_router",
        type("_R", (), {"record_model_success": lambda self, m: None})(),
    )

    repl._run_engine(spec, "修到 CI 通过", ["openai/test"])

    assert engines == []  # 第二轮引擎被构造并运行
    assert rendered == ["修复完成，CI 通过"]
    assert any("校验未通过" in m for m in ctx.user)
    events = repl._session_events.read()
    assert any(e["type"] == "verification/retry" for e in events)


def test_run_engine_exhausts_retries_and_still_renders(monkeypatch, tmp_path):
    """两轮都失败时：重试耗尽，仍渲染最后一轮结果（由渲染层按草稿处理）。"""

    monkeypatch.setenv("XENON_SESSION_EVENTS_DIR", str(tmp_path))
    monkeypatch.setenv("XENON_VERIFY_RETRIES", "1")
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    repl = REPL(registry=registry, streaming=False)
    repl._last_user_text = "帮我看看这个项目"  # 无完成准则：只由宣称审计规则触发

    engines = [_FakeEngine("任务完成 ✅"), _FakeEngine("任务完成 ✅")]
    callbacks = [_Callback(_Panel(True)), _Callback(_Panel(True))]
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
    rendered: list = []
    monkeypatch.setattr(
        repl, "_render_engine_result", lambda cb, res, title: rendered.append(res)
    )
    monkeypatch.setattr(
        repl,
        "auto_router",
        type("_R", (), {"record_model_success": lambda self, m: None})(),
    )

    repl._run_engine(spec, "修到 CI 通过", ["openai/test"])

    assert engines == []  # 重试轮仍会运行（第一次失败触发一轮重试）
    assert rendered == ["任务完成 ✅"]
    events = repl._session_events.read()
    assert sum(1 for e in events if e["type"] == "verification/retry") == 1


def test_retry_budget_half_of_first_round_clamped(monkeypatch, tmp_path):
    """决策 2：重试轮引擎预算 = 首轮实际步数的一半，clamp [10, 40]。"""

    monkeypatch.setenv("XENON_SESSION_EVENTS_DIR", str(tmp_path))
    monkeypatch.setenv("XENON_VERIFY_RETRIES", "1")
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    repl = REPL(registry=registry, streaming=False)
    repl._last_user_text = "帮我看看这个项目"

    engine_budgets: list[int] = []

    class _BudgetEngine(_FakeEngine):
        def __init__(self, answer: str):
            super().__init__(answer)
            self.max_iterations = 40

    def factory(**kw):
        engine = _BudgetEngine("任务完成 ✅")
        engine_budgets.append(engine)
        return engine

    class _BigPanel:
        steps = [_Step(True) for _ in range(30)]  # 首轮实际 30 步（含失败回执）
        errors: list = []
        tool_call_count = 30

    callbacks = [_Callback(_BigPanel()), _Callback(_BigPanel())]
    spec = EngineSpec(name="fake", mode_line="", factory=factory, result_title="")

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

    monkeypatch.setattr(repl, "ctx_mgr", _Ctx())
    monkeypatch.setattr(
        repl, "_render_engine_result", lambda cb, res, title: None
    )
    monkeypatch.setattr(
        repl,
        "auto_router",
        type("_R", (), {"record_model_success": lambda self, m: None})(),
    )

    repl._run_engine(spec, "帮我看看这个项目", ["openai/test"])

    assert len(engine_budgets) == 2  # 首轮 + 一次重试
    assert engine_budgets[0].max_iterations == 40  # 首轮不缩
    assert engine_budgets[1].max_iterations == 15  # 30/2


def test_retry_budget_helper_clamps(monkeypatch):
    from xenon.repl.repl import _retry_budget_for

    assert _retry_budget_for(0) == 10
    assert _retry_budget_for(5) == 10
    assert _retry_budget_for(30) == 15
    assert _retry_budget_for(100) == 40
    assert _retry_budget_for(19) == 10  # 19//2=9 → clamp 到 10
