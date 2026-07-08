"""F4 验收：ContextManager 注入引擎 + 引擎内每 5 轮自动压缩。

- run(user_input, context, ctx_mgr)：ctx_mgr 注入四引擎 + 三个组合引擎；
- ctx_mgr 提供时消费其（已压缩）消息，不再自行 [-10:] 截断；
- 引擎内每 5 轮调 _maybe_compact_messages 触发 in-run 压缩（抑制 O(n²) 增长）；
- 组合引擎把 ctx_mgr 透传给子引擎。
"""
from types import SimpleNamespace

from omniagent.engine.combined_engines import ReactReflectionEngine
from omniagent.engine.context import AgentContext
from omniagent.engine.plan_execute_engine import PlanExecuteEngine
from omniagent.engine.react_engine import ReActEngine
from omniagent.repl.context_manager import ContextManager, ConversationTurn


def _ctx_mgr_with(n):
    cm = ContextManager()
    for i in range(n):
        cm.add_user_message(f"历史user{i}")
        cm.add_assistant_message(f"历史asst{i}")
    return cm


# ── ctx_mgr 注入：消费压缩后消息，不再 [-10:] ──────────────
class TestCtxMgrInjection:
    def test_react_consumes_all_ctx_mgr_history(self):
        """ctx_mgr 有 15 条历史 → ReAct 全量注入（不再 [-10:] 截到 10）。"""
        cm = _ctx_mgr_with(15)  # 30 条非 system 消息
        eng = ReActEngine(["m1"], max_iterations=3)
        captured = {}

        def fake_llm(messages, max_tokens=None):
            captured["history_count"] = sum(
                1 for m in messages if "历史" in m.get("content", "")
            )
            return '{"thought":"t","final_answer":"done"}'

        eng._call_llm = fake_llm
        eng._parse_response = lambda resp: {"thought": "t", "final_answer": "done"}
        eng._input_requires_tools = lambda u: False

        eng.run("继续", AgentContext(), ctx_mgr=cm)
        assert captured["history_count"] == 30  # 全量，非 10

    def test_react_without_ctx_mgr_caps_at_10(self):
        """无 ctx_mgr → 回退 AgentContext 历史 [-10:]（向后兼容）。"""
        ctx = AgentContext()
        ctx.set_conversation_messages([
            {"role": "user", "content": f"历史user{i}"} for i in range(20)
        ] + [{"role": "assistant", "content": f"历史asst{i}"} for i in range(20)])
        eng = ReActEngine(["m1"], max_iterations=3)
        captured = {}

        def fake_llm(messages, max_tokens=None):
            captured["history_count"] = sum(
                1 for m in messages if "历史" in m.get("content", "")
            )
            return "raw"

        eng._call_llm = fake_llm
        eng._parse_response = lambda resp: {"thought": "t", "final_answer": "done"}
        eng._input_requires_tools = lambda u: False

        eng.run("继续", ctx)
        assert captured["history_count"] == 10  # [-10:] 截断

    def test_plan_consumes_ctx_mgr_history(self):
        cm = _ctx_mgr_with(5)  # 10 条历史
        eng = PlanExecuteEngine(["m1"])
        eng._ctx_mgr = cm
        captured = {}

        def fake_plan(self, user_input, context=None):
            history = self._history_messages(context)
            captured["n"] = len(history)
            return {"steps": [], "analysis": "ok"}

        # 保存原方法引用（class body 定义的 def _plan）→ finally 恢复，
        # 而不是 ``del PlanExecuteEngine._plan``——后者会真删方法，
        # 让后续测试 ``eng.run`` 调 ``self._plan`` 报 AttributeError。
        original_plan = PlanExecuteEngine._plan
        PlanExecuteEngine._plan = fake_plan
        try:
            eng.run("做点事", AgentContext(), ctx_mgr=cm)
        finally:
            PlanExecuteEngine._plan = original_plan
        assert captured["n"] == 10  # 全量 ctx_mgr 历史


# ── in-run 压缩：每 5 轮触发 ───────────────────────────────
class TestMaybeCompactMessages:
    def test_noop_before_turn_5(self):
        eng = ReActEngine(["m1"])
        msgs = [{"role": "user", "content": "hi"}]
        out = eng._maybe_compact_messages(msgs, 3)
        assert out is msgs  # 未到 5 轮，原样返回同一对象

    def test_noop_at_turn_0(self):
        eng = ReActEngine(["m1"])
        msgs = [{"role": "user", "content": "hi"}]
        assert eng._maybe_compact_messages(msgs, 0) is msgs

    def test_invokes_compact_at_turn_5(self, monkeypatch):
        eng = ReActEngine(["m1"], model_configs={"m1": SimpleNamespace(context_window=1000)})
        called = {"n": 0}

        def fake_compact(self_cm, *a, **k):
            called["n"] += 1
            self_cm.history = [ConversationTurn(role="system", content="COMPACTED")]
            return "COMPACTED"

        monkeypatch.setattr(
            "omniagent.repl.context_manager.ContextManager.compact", fake_compact
        )
        msgs = [{"role": "user", "content": f"msg{i}"} for i in range(8)]
        out = eng._maybe_compact_messages(msgs, 5)
        assert called["n"] == 1
        assert len(out) < len(msgs)  # 已被压缩替换
        assert out[0]["content"] == "COMPACTED"

    def test_compact_failure_does_not_break_loop(self, monkeypatch):
        eng = ReActEngine(["m1"])

        def boom(self_cm, *a, **k):
            raise RuntimeError("compact exploded")

        monkeypatch.setattr(
            "omniagent.repl.context_manager.ContextManager.compact", boom
        )
        msgs = [{"role": "user", "content": "msg"}]
        out = eng._maybe_compact_messages(msgs, 5)
        assert out is msgs  # 压缩失败时沿用原 messages


# ── 组合引擎透传 ctx_mgr ───────────────────────────────────
class TestCompositeForwarding:
    def test_react_reflection_forwards_ctx_mgr(self):
        eng = ReactReflectionEngine(["m1"], react_iterations=2, review_rounds=1)
        received = []

        def fake_reactor_run(user_input, context=None, ctx_mgr=None):
            received.append(("reactor", ctx_mgr))
            return "reactor output"

        def fake_reflector_run(user_input, context=None, ctx_mgr=None):
            received.append(("reflector", ctx_mgr))
            return "final output"

        eng.reactor.run = fake_reactor_run
        eng.reflector.run = fake_reflector_run

        cm = _ctx_mgr_with(2)
        eng.run("任务", ctx_mgr=cm)
        assert ("reactor", cm) in received
        assert ("reflector", cm) in received
