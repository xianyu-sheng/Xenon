"""P2-E2 迷你 ReAct 验收：无工具步骤跑 N 轮 Thought→Action→Observation（§Q4 / §9 表 E2）。

审核文档 §Q4：规范声称 Plan-Execute 的无工具步骤跑"3 轮迷你 ReAct"，但实际
``_execute_step_with_llm`` 是单次 LLM 调用。本提交将其改为内部跑最多
``max_mini_react_rounds``（默认 3）轮，复用 ``parse_react`` 解析 +
``_execute_step_with_tool`` 执行。

覆盖：
- 纯文本响应：首轮即收敛（向后兼容，单次调用，结果=纯文本）。
- final_answer JSON：提取 final_answer（结果=提取值，非整段 JSON）。
- action → final_answer：2 轮，工具被执行，observation 回填，结果=final_answer。
- 3 轮耗尽（持续 action）：调用次数封顶 max_mini_react_rounds。
- max_mini_react_rounds 可配。
- 工具异常转 observation（⚠️ 工具执行失败），循环继续。
- _verify_llm_file_claims 仍生效（文件声明未验证→警告）。
- 端到端 run()：无工具步骤走迷你 ReAct。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from xenon.engine.context import AgentContext
from xenon.engine.plan_execute_engine import PlanExecuteEngine
from xenon.engine.tool_tracker import ToolExecutionTracker


def _eng(max_mini_react_rounds: int = 3) -> PlanExecuteEngine:
    return PlanExecuteEngine(["m1"], max_mini_react_rounds=max_mini_react_rounds)


class _FakeLLM:
    """按预设序列返回 _call_llm 结果（execute 调用逐个弹出）。"""

    def __init__(self, execute_responses: list[str]):
        self._responses = list(execute_responses)
        self.execute_calls = 0
        self.execute_messages: list[list[dict]] = []

    def __call__(self, messages, max_tokens=None, *, model_priority=None):
        self.execute_calls += 1
        self.execute_messages.append(messages)
        if self._responses:
            return self._responses.pop(0)
        return '{"thought":"done","final_answer":"fallback"}'


def _action_json(tool: str, params: dict) -> str:
    return json.dumps({"thought": "need tool", "action": tool, "action_input": params},
                      ensure_ascii=False)


def _final_json(text: str) -> str:
    return json.dumps({"thought": "summarize", "final_answer": text}, ensure_ascii=False)


# ── 单元：直接调用 _execute_step_with_llm ────────────────────


class TestBackwardCompat:
    def test_plain_text_single_call(self):
        eng = _eng()
        eng._call_llm = _FakeLLM(["完成: 分析"])
        out = eng._execute_step_with_llm(1, 1, "分析需求", "", "原始任务", None)
        assert eng._call_llm.execute_calls == 1  # 首轮即收敛
        assert out == "完成: 分析"  # 纯文本原样返回

    def test_final_answer_json_extracted(self):
        eng = _eng()
        eng._call_llm = _FakeLLM([_final_json("真实结论")])
        out = eng._execute_step_with_llm(1, 1, "总结", "", "原始任务", None)
        assert eng._call_llm.execute_calls == 1
        assert out == "真实结论"  # 提取 final_answer，非整段 JSON


class TestActionLoop:
    def test_action_then_final_answer(self):
        eng = _eng()
        eng._call_llm = _FakeLLM([
            _action_json("read_file", {"file_path": "x.py"}),
            _final_json("基于 x.py 的结论"),
        ])
        observed: list[tuple[str, dict]] = []
        eng._execute_step_with_tool = lambda tool, params, ctx, tracker=None: (
            observed.append((tool, params)) or "OBS: x.py 内容"
        )

        out = eng._execute_step_with_llm(1, 1, "分析 x.py", "", "原始任务", None)

        assert eng._call_llm.execute_calls == 2  # action + final_answer
        assert observed == [("read_file", {"file_path": "x.py"})]
        assert out == "基于 x.py 的结论"
        # 第二轮的 user 消息含 observation 回填
        second_msgs = eng._call_llm.execute_messages[1]
        assert any("OBS: x.py 内容" in m.get("content", "") for m in second_msgs)

    def test_exhausts_rounds_bounded(self):
        """持续 action 不给 final_answer → 调用次数封顶 max_mini_react_rounds。"""
        eng = _eng(max_mini_react_rounds=3)
        eng._call_llm = _FakeLLM([_action_json("read_file", {"file_path": f"f{i}.py"}) for i in range(10)])
        eng._execute_step_with_tool = lambda tool, params, ctx, tracker=None: "OBS"

        eng._execute_step_with_llm(1, 1, "分析", "", "原始任务", None)
        assert eng._call_llm.execute_calls == 3  # 封顶 3 轮

    def test_custom_rounds(self):
        eng = _eng(max_mini_react_rounds=2)
        eng._call_llm = _FakeLLM([_action_json("read_file", {"file_path": "f.py"}) for _ in range(10)])
        eng._execute_step_with_tool = lambda tool, params, ctx, tracker=None: "OBS"

        eng._execute_step_with_llm(1, 1, "分析", "", "原始任务", None)
        assert eng._call_llm.execute_calls == 2  # 封顶 2 轮

    def test_tool_exception_becomes_observation(self):
        """工具抛异常 → observation 含 ⚠️，循环继续到 final_answer。"""
        eng = _eng()
        eng._call_llm = _FakeLLM([
            _action_json("read_file", {"file_path": "bad.py"}),
            _final_json("容错结论"),
        ])

        def boom(tool, params, ctx, tracker=None):
            raise RuntimeError("disk error")
        eng._execute_step_with_tool = boom

        out = eng._execute_step_with_llm(1, 1, "分析", "", "原始任务", None)
        assert out == "容错结论"  # 循环未中断
        # 第二轮 user 消息含异常 observation
        second_msgs = eng._call_llm.execute_messages[1]
        assert any("⚠️ 工具执行失败" in m.get("content", "") for m in second_msgs)


class TestFileClaimsStillVerified:
    def test_unverified_file_claim_warns(self):
        """迷你 ReAct 结果仍走 _verify_llm_file_claims（声明写文件但无工具→警告）。"""
        eng = _eng()
        eng._call_llm = _FakeLLM([_final_json("已保存 x_missing.py 的内容")])
        tracker = ToolExecutionTracker()  # 无任何工具调用

        out = eng._execute_step_with_llm(1, 1, "保存", "", "原始任务", tracker)
        assert "未经工具验证" in out
        assert "x_missing.py" in out

    def test_verified_claim_no_warning(self):
        eng = _eng()
        eng._call_llm = _FakeLLM([_final_json("已保存 ok.py 的内容")])
        tracker = ToolExecutionTracker()
        # 模拟 write_file 已成功执行
        from types import SimpleNamespace
        tracker.calls.append(SimpleNamespace(
            tool_name="write_file", params={"file_path": "ok.py"}, success=True))

        out = eng._execute_step_with_llm(1, 1, "保存", "", "原始任务", tracker)
        assert "未经工具验证" not in out


# ── 端到端 run() ─────────────────────────────────────────────


class TestRunIntegration:
    def test_no_tool_step_uses_mini_react(self):
        """run() 中无工具步骤走迷你 ReAct，步骤结果为 final_answer。"""
        from xenon.engine.callbacks import EngineCallback

        plan = {"analysis": "单步分析", "steps": [
            {"id": 1, "task": "分析需求并总结", "tool": None, "params": {}, "depends_on": []},
        ]}

        class _BranchLLM:
            def __init__(self):
                self.execute_calls = 0

            def __call__(self, messages, max_tokens=None, *, model_priority=None):
                sys = messages[0]["content"] if messages else ""
                if "任务规划专家" in sys:
                    return json.dumps(plan, ensure_ascii=False)
                if "请根据以下执行结果" in sys:
                    return "SUMMARY"
                # 迷你 ReAct 执行
                self.execute_calls += 1
                return _final_json("端到端结论")

        class _Cap(EngineCallback):
            def __init__(self):
                self.step_results: list[str] = []

            def on_step_done(self, step_id, success, summary):
                self.step_results.append(summary)

        eng = PlanExecuteEngine(["m1"], max_mini_react_rounds=3, callback=_Cap())
        fake = _BranchLLM()
        eng._call_llm = fake

        out = eng.run("分析一下", AgentContext())
        assert out == "SUMMARY"  # run 返回总结
        assert fake.execute_calls == 1  # 无工具步骤首轮即 final_answer
        assert eng.callback.step_results == ["端到端结论"]  # 步骤结果=final_answer
