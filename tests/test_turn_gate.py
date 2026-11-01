"""TurnGate：回合级唯一校验层（pass/fail/fuse 状态机）。"""

from __future__ import annotations

from xenon.repl.model_registry import ModelRegistry
from xenon.repl.repl import REPL
from xenon.turn.gate import (
    VERDICT_FAIL,
    VERDICT_FUSE,
    VERDICT_PASS,
    VERDICT_REMIND,
    TurnGate,
    tool_events_from_panel,
)


def test_pass_on_clean_answer():
    gate = TurnGate()
    verdict = gate.evaluate(
        "已完成读取并整理结果：共 3 个文件，内容摘要见下。",
        tool_events=[{"tool": "read_file", "success": True}],
    )
    assert verdict.outcome == VERDICT_PASS
    assert verdict.reasons == []


def test_fail_then_pass_resets_rounds():
    gate = TurnGate()
    bad = [{"tool": "command", "success": False, "error": "pytest failed"}]
    assert gate.evaluate("好的", tool_events=bad).outcome == VERDICT_FAIL  # 空洞=结构层
    assert gate.rounds_used == 1
    good = [{"tool": "command", "success": True}]
    assert (
        gate.evaluate(
            "已完成读取并整理结果：共 3 个文件，内容摘要见下。", tool_events=good
        ).outcome
        == VERDICT_PASS
    )
    assert gate.rounds_used == 0  # pass 后清零


def test_semantic_claim_issues_are_reminders_not_failures():
    """L2 语义层：宣称审计命中 → remind（发布 + 核验清单），不阻断。"""

    gate = TurnGate()
    bad = [{"tool": "command", "success": False, "error": "pytest failed"}]
    verdict = gate.evaluate(
        "已完成任务：分析了 12 个模块并给出结论", tool_events=bad
    )
    assert verdict.outcome == VERDICT_REMIND
    assert any("宣称成功" in r for r in verdict.reasons)
    assert gate.rounds_used == 0  # 提醒类不进重试轮次


def test_same_cause_failures_fuse():
    gate = TurnGate(max_same_cause_failures=2)
    bad = [{"tool": "command", "success": False, "error": "pytest failed"}]
    assert gate.evaluate("好的", tool_events=bad).outcome == VERDICT_FAIL
    verdict = gate.evaluate("好的", tool_events=bad)
    assert verdict.outcome == VERDICT_FUSE
    assert any("熔断" in r for r in verdict.reasons)


def test_different_causes_do_not_fuse():
    gate = TurnGate(max_same_cause_failures=2)
    bad = [{"tool": "command", "success": False, "error": "x"}]
    assert gate.evaluate("好的", tool_events=bad).outcome == VERDICT_FAIL
    # 不同空洞签名（quick_fail vs 套话模式）→ 重新计数
    assert (
        gate.evaluate("综上所述，这是分析。", tool_events=bad).outcome
        == VERDICT_FAIL
    )
    assert gate.rounds_used == 1


def test_tool_events_from_panel():
    class _Step:
        action = "command"
        is_error = True
        observation = "exit 1"

    class _Panel:
        steps = [_Step()]

    events = tool_events_from_panel(_Panel())
    assert events == [
        {"tool": "command", "success": False, "error": "exit 1", "params": {}},
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
