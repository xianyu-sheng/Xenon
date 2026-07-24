"""P2-E5 验收：spawn_agent 子 Agent 系统（§Q7）。

覆盖（见审核文档 §Q7 / §8.1.1 / §8.1.6）：

- spawn_agent 在 BUILTIN_TOOLS 中暴露（name/description/params.task）。
- _spawn_subagent：返回格式化结果（✅/task_id/工具调用统计/最终回答），
  空任务失败，记入父 tracker，子引擎持独立 ctx+tracker+budget（隔离）。
- 递归深度限制（max_subagent_depth）防失控。
- 子引擎异常转失败（❌ + 执行异常）。
- 子引擎用独立预算（max_subagent_iterations，非父 max_iterations）。
- 端到端：父 ReAct 循环中 spawn_agent 作为工具被调用并返回 observation。

设计取舍：审核 §Q7 规范为 asyncio.create_task 后台 + _poll_subagents 轮询，
但全仓库零 async 基础设施（§8.1.1）——本实现为同步阻塞委派（子 Agent 能力
交付），后台并发轮询留作后续 perf 优化。
"""

from __future__ import annotations

import json


import xenon.engine.base as base
from xenon.engine.context import AgentContext
from xenon.engine.react_engine import BUILTIN_TOOLS, ReActEngine
from xenon.engine.tool_tracker import ToolExecutionTracker


def _patch_chat(monkeypatch, responder):
    """把 base.chat_completion 替换为 responder(messages)->str。

    父引擎与子引擎（同进程新建实例）均经此，确保子 Agent 也走 mock。
    """
    def fake(model_id, messages, *, max_tokens=None, temperature=0.3,
             credentials=None, base_url=None, **kw):
        return responder(messages)
    monkeypatch.setattr(base, "chat_completion", fake)


def _final_answer_json(text: str) -> str:
    return json.dumps({"thought": "t", "final_answer": text}, ensure_ascii=False)


class TestBuiltinTool:
    def test_spawn_agent_in_builtin_tools(self):
        assert "spawn_agent" in BUILTIN_TOOLS
        spec = BUILTIN_TOOLS["spawn_agent"]
        assert spec["name"] == "spawn_agent"
        assert "task" in spec["params"]
        assert "子 Agent" in spec["description"]


class TestSpawnSubagent:
    def test_returns_formatted_result(self, monkeypatch):
        _patch_chat(monkeypatch, lambda m: _final_answer_json("子任务结果"))
        eng = ReActEngine(["m1"], max_iterations=4)
        tracker = ToolExecutionTracker()
        ctx = AgentContext()

        out = eng._spawn_subagent({"task": "总结这个模块"}, ctx, tracker)

        assert "✅" in out
        assert "sub-react-d1-1" in out  # v0.6.2: task_id 格式为 sub-{engine}-d{depth}-{num}
        assert "工具调用" in out
        assert "子任务结果" in out

    def test_empty_task_fails(self, monkeypatch):
        _patch_chat(monkeypatch, lambda m: _final_answer_json("x"))
        eng = ReActEngine(["m1"])
        out = eng._spawn_subagent({}, AgentContext(), ToolExecutionTracker())
        assert out.startswith("执行失败")
        assert "task" in out

    def test_records_in_parent_tracker(self, monkeypatch):
        _patch_chat(monkeypatch, lambda m: _final_answer_json("ok"))
        eng = ReActEngine(["m1"])
        tracker = ToolExecutionTracker()
        eng._spawn_subagent({"task": "做A"}, AgentContext(), tracker)
        assert len(tracker.calls) == 1
        call = tracker.calls[0]
        assert call.tool_name == "spawn_agent"
        assert call.success is True

    def test_subagent_isolated_ctx_and_tracker(self, monkeypatch):
        """子引擎持独立 ctx 与 tracker，不与父共享。"""
        _patch_chat(monkeypatch, lambda m: _final_answer_json("iso"))
        eng = ReActEngine(["m1"])
        parent_tracker = ToolExecutionTracker()
        parent_ctx = AgentContext()
        parent_ctx.set("secret", "父私有状态")

        eng._spawn_subagent({"task": "总结"}, parent_ctx, parent_tracker)

        sub = eng._last_subagent
        assert sub is not None
        # 子 tracker 与父不同
        assert sub._last_tracker is not parent_tracker
        # 子 ctx 不继承父 store（隔离）
        assert sub._last_tracker is not None

    def test_subagent_uses_independent_budget(self, monkeypatch):
        """子引擎用 max_subagent_iterations，非父 max_iterations。"""
        _patch_chat(monkeypatch, lambda m: _final_answer_json("ok"))
        eng = ReActEngine(["m1"], max_iterations=20, max_subagent_iterations=5)
        eng._spawn_subagent({"task": "总结"}, AgentContext(), ToolExecutionTracker())
        sub = eng._last_subagent
        assert sub.max_iterations == 5
        assert sub.max_iterations != eng.max_iterations

    def test_subagent_exception_becomes_failure(self, monkeypatch):
        def boom(messages):
            raise RuntimeError("子 Agent 炸了")
        _patch_chat(monkeypatch, boom)
        eng = ReActEngine(["m1"])
        tracker = ToolExecutionTracker()
        out = eng._spawn_subagent({"task": "总结"}, AgentContext(), tracker)
        assert "❌" in out
        assert "执行异常" in out
        assert tracker.calls[0].success is False


class TestDepthLimit:
    def test_depth_zero_refuses_when_max_zero(self, monkeypatch):
        _patch_chat(monkeypatch, lambda m: _final_answer_json("x"))
        eng = ReActEngine(["m1"], max_subagent_depth=0)
        out = eng._spawn_subagent({"task": "总结"}, AgentContext(), ToolExecutionTracker())
        assert "嵌套深度超限" in out
        assert eng._last_subagent is None  # 未创建子引擎

    def test_depth_one_sub_cannot_spawn_further(self, monkeypatch):
        """max_subagent_depth=1：父(深度0)可 spawn → 子(深度1) 再 spawn 被拒。"""
        _patch_chat(monkeypatch, lambda m: _final_answer_json("x"))
        eng = ReActEngine(["m1"], max_subagent_depth=1)
        # 父 spawn 一次（应成功）
        out1 = eng._spawn_subagent({"task": "总结"}, AgentContext(), ToolExecutionTracker())
        assert "✅" in out1
        sub = eng._last_subagent
        assert sub._subagent_depth == 1
        # 子再 spawn → 拒绝
        out2 = sub._spawn_subagent({"task": "再委派"}, AgentContext(), ToolExecutionTracker())
        assert "嵌套深度超限" in out2


class TestEndToEnd:
    def test_react_loop_invokes_spawn_agent(self, monkeypatch):
        """父 ReAct 循环：首轮 spawn_agent，次轮 final_answer。"""
        state = {"spawned": False}

        def responder(messages):
            last = messages[-1]["content"] if messages else ""
            # 含 spawn_agent 观察结果 → 进入汇总
            if "子任务" in last and "✅" in last:
                return _final_answer_json("已汇总子 Agent 结果")
            # 首轮：发起 spawn_agent
            if not state["spawned"]:
                state["spawned"] = True
                return json.dumps(
                    {"thought": "委派", "action": "spawn_agent",
                     "action_input": {"task": "分析并总结"}, "final_answer": ""},
                    ensure_ascii=False)
            return _final_answer_json("已完成")

        _patch_chat(monkeypatch, responder)
        eng = ReActEngine(["m1"], max_iterations=4)
        eng._input_requires_tools = lambda u: True
        out = eng.run("帮我分析", AgentContext())

        # 父 tracker 记录了 spawn_agent
        assert any(c.tool_name == "spawn_agent" for c in eng._last_tracker.calls)
        # 最终返回了汇总答案
        assert "已汇总" in out
