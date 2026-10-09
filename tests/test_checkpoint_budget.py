"""P2：预算检查点（审批续跑 / 无进展熔断 / 同因中断快速熔断）。"""

from __future__ import annotations

from xenon.engine.context import AgentContext
from xenon.engine.react_engine import ReActEngine, _env_checkpoint_steps
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.repl import REPL


class _Call:
    def __init__(self, success: bool, error: str = ""):
        self.tool_name = "command"
        self.success = success
        self.error = error
        self.result_summary = error


class _Tracker:
    def __init__(self, calls: list[_Call]):
        self.calls = calls


class _Eng(ReActEngine):
    def __init__(self, checkpoint_steps: int):
        super().__init__(model_priority=["m1"], max_iterations=4)
        self.checkpoint_steps = checkpoint_steps


def test_env_checkpoint_steps(monkeypatch):
    monkeypatch.delenv("XENON_CHECKPOINT_STEPS", raising=False)
    assert _env_checkpoint_steps(40) == 40
    monkeypatch.setenv("XENON_CHECKPOINT_STEPS", "12")
    assert _env_checkpoint_steps(40) == 12
    monkeypatch.setenv("XENON_CHECKPOINT_STEPS", "garbage")
    assert _env_checkpoint_steps(40) == 40


def test_checkpoint_no_progress_fuses():
    eng = _Eng(2)
    tracker = _Tracker(
        [_Call(False, "exit 1"), _Call(False, "exit 1")]  # 窗口内失败签名相同
    )
    assert eng._budget_checkpoint(AgentContext(), tracker, 0, "exit 1") == "fuse"


def test_checkpoint_progress_asks_and_continues():
    eng = _Eng(2)
    ctx = AgentContext()
    ctx.set_checkpoint_callback(lambda reason: "approved")
    tracker = _Tracker([_Call(True), _Call(False, "exit 1")])
    assert eng._budget_checkpoint(ctx, tracker, 0, "") == "continue"


def test_checkpoint_declined_stops():
    eng = _Eng(2)
    ctx = AgentContext()
    ctx.set_checkpoint_callback(lambda reason: "declined")
    tracker = _Tracker([_Call(True)])
    assert eng._budget_checkpoint(ctx, tracker, 0, "") == "stop"


def test_checkpoint_no_channel_defaults_approved():
    eng = _Eng(2)
    tracker = _Tracker([_Call(True)])
    assert eng._budget_checkpoint(AgentContext(), tracker, 0, "") == "continue"


def test_request_checkpoint_continuation_outcomes():
    ctx = AgentContext()
    assert ctx.request_checkpoint_continuation("x") == "approved"  # 无通道

    ctx.set_checkpoint_callback(lambda reason: "approved")
    assert ctx.request_checkpoint_continuation("x") == "approved"
    ctx.set_checkpoint_callback(lambda reason: "declined")
    assert ctx.request_checkpoint_continuation("x") == "declined"
    ctx.set_checkpoint_callback(lambda reason: "garbage")
    assert ctx.request_checkpoint_continuation("x") == "declined"  # 封闭词表

    def boom(reason):
        raise RuntimeError("通道故障")

    ctx.set_checkpoint_callback(boom)
    assert ctx.request_checkpoint_continuation("x") == "declined"  # fail-closed


def test_same_cause_interruption_fuses_node(monkeypatch, tmp_path):
    """连续两轮同因中断（如 402）→ 第二轮节点状态 fused + 指引。"""

    monkeypatch.setenv("XENON_SESSION_EVENTS_DIR", str(tmp_path))
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    repl = REPL(registry=registry, streaming=False)
    repl._turn_tree_rebuilt = True

    n1 = repl._turn_tree.append_turn("修 bug")
    repl._finish_turn_node("interrupted", engine="react", reasons=["402 欠费"])
    n2 = repl._turn_tree.append_turn("继续")
    repl._finish_turn_node("interrupted", engine="react", reasons=["402 欠费"])

    assert n1.status == "interrupted"
    assert n2.status == "fused"
    assert any("连续两轮同因" in r for r in n2.verdict_reasons)


def test_different_cause_stays_interrupted(monkeypatch, tmp_path):
    monkeypatch.setenv("XENON_SESSION_EVENTS_DIR", str(tmp_path))
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    repl = REPL(registry=registry, streaming=False)
    repl._turn_tree_rebuilt = True

    repl._turn_tree.append_turn("修 bug")
    repl._finish_turn_node("interrupted", engine="react", reasons=["402 欠费"])
    n2 = repl._turn_tree.append_turn("继续")
    repl._finish_turn_node("interrupted", engine="react", reasons=["500 服务端错误"])

    assert n2.status == "interrupted"


def test_has_checkpoint_channel():
    ctx = AgentContext()
    assert ctx.has_checkpoint_channel() is False
    ctx.set_checkpoint_callback(lambda reason: "approved")
    assert ctx.has_checkpoint_channel() is True


def test_no_channel_keeps_legacy_budget_semantics(monkeypatch, tmp_path):
    """库/直连路径无审批通道 → 检查点永不调用（旧预算语义，不询问不熔断）。"""

    eng = _Eng(4)
    eng.checkpoint_steps = 1  # 若通道门被移除，窗口会在循环内触发检查点
    asked: list = []
    monkeypatch.setattr(
        eng,
        "_budget_checkpoint",
        lambda *a, **k: asked.append(1) or "fuse",
    )
    responses = iter(
        [
            '{"thought": "t", "action": "read_file", '
            '"action_input": {"file_path": "x.py"}}',
            '{"thought": "t", "final_answer": "完成"}',
        ]
    )
    monkeypatch.setattr(
        eng,
        "_call_llm_for_phase",
        lambda phase, messages, **kw: next(responses),
    )
    ctx = AgentContext()
    ctx.set_conversation_messages([])
    result = eng.run("跑任务", context=ctx)
    assert asked == []  # 无通道 → 检查点永不被询问
    assert "完成" in str(result)
