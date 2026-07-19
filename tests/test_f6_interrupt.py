"""F6 验收：协作式中断 + 引擎内上下文预算检查。

- BaseEngine.interrupt() 置位后，各引擎 run() 在下一轮迭代顶部检测并退出；
- _reset_interrupt() 在每次 run() 起点重置，避免上一轮残留中断污染本轮；
- _context_window() 取 model_configs 中最小的 context_window（瓶颈模型）；
- _near_context_window() 在消息体量接近窗口 80% 时返回 True，触发观察截断。
"""
from types import SimpleNamespace

import xenon.engine.base as base_mod
from xenon.engine.callbacks import EngineCallback
from xenon.engine.context import AgentContext
from xenon.engine.novel_engine import NovelEngine
from xenon.engine.plan_execute_engine import PlanExecuteEngine
from xenon.engine.react_engine import ReActEngine


class _RecordingCallback(EngineCallback):
    def __init__(self):
        self.warnings: list[str] = []
        self.finishes: list[str] = []
        self.acts: list[tuple] = []
        self.steps: list = []

    def on_think(self, thought): pass
    def on_act(self, action, action_input): self.acts.append((action, action_input))
    def on_observe(self, observation): pass
    def on_step(self, step_id, total, task): self.steps.append(step_id)
    def on_step_done(self, *a, **k): pass
    def on_review(self, *a, **k): pass
    def on_error(self, error): pass
    def on_warning(self, warning): self.warnings.append(warning)
    def on_finish(self, result): self.finishes.append(result)


# ── 上下文窗口辅助 ──────────────────────────────────────────
class TestContextWindowHelpers:
    def test_context_window_reads_min_from_model_configs(self):
        configs = {
            "a": SimpleNamespace(context_window=128000),
            "b": SimpleNamespace(context_window=8000),
            "c": SimpleNamespace(context_window=200000),
        }
        eng = ReActEngine(["a", "b", "c"], model_configs=configs)
        assert eng._context_window() == 8000  # 瓶颈模型决定窗口

    def test_context_window_defaults_when_none(self):
        eng = ReActEngine(["a"])  # 无 model_configs
        assert eng._context_window() == 128000

    def test_context_window_ignores_zero(self):
        configs = {"a": SimpleNamespace(context_window=0), "b": SimpleNamespace(context_window=64000)}
        eng = ReActEngine(["a", "b"], model_configs=configs)
        assert eng._context_window() == 64000

    def test_near_context_window_true_for_large(self):
        # 窗口 8000，ratio 0.8 → 阈值 6400（est = chars//2，需 chars > 12800）
        configs = {"a": SimpleNamespace(context_window=8000)}
        eng = ReActEngine(["a"], model_configs=configs)
        big = [{"role": "user", "content": "x" * 20000}]
        assert eng._near_context_window(big) is True

    def test_near_context_window_false_for_small(self):
        configs = {"a": SimpleNamespace(context_window=128000)}
        eng = ReActEngine(["a"], model_configs=configs)
        small = [{"role": "user", "content": "hi"}]
        assert eng._near_context_window(small) is False

    def test_near_context_window_disabled_when_zero(self):
        eng = ReActEngine(["a"])  # 默认窗口 128000 但无配置时仍可测 ratio 路径
        # 窗口 >0 时小消息不触发；这里仅验证不抛异常
        assert eng._near_context_window([{"role": "user", "content": ""}]) is False


# ── ReAct 中断 ──────────────────────────────────────────────
class TestReActInterrupt:
    def test_interrupt_breaks_loop_after_current_iteration(self, monkeypatch):
        """interrupt() 在第 1 次 LLM 调用内置位 → 第 2 轮顶部检测退出。"""
        cb = _RecordingCallback()
        eng = ReActEngine(["m1"], max_iterations=5, callback=cb)
        calls = {"n": 0}

        def fake_llm(messages):
            calls["n"] += 1
            eng.interrupt()  # 模拟外部中断
            return "raw"  # 会被 _parse_response 解析

        eng._call_llm = fake_llm
        eng._parse_response = lambda resp: {"thought": "t", "action": "echo", "action_input": {}}
        eng._execute_tool = lambda action, action_input, ctx, tracker: "obs"
        eng._input_requires_tools = lambda u: True

        result = eng.run("做点什么", AgentContext())
        assert calls["n"] == 1  # 第 2 轮未再调用 LLM
        assert "引擎被用户中断" in result
        assert any("引擎被用户中断，停止迭代" in w for w in cb.warnings)

    def test_reset_interrupt_at_run_start(self, monkeypatch):
        """run() 起点重置中断标志，残留中断不阻塞本轮。"""
        cb = _RecordingCallback()
        eng = ReActEngine(["m1"], max_iterations=3, callback=cb)
        eng._interrupted = True  # 模拟上一轮残留

        seen = {"reset": None}

        def fake_llm(messages):
            seen["reset"] = eng._interrupted  # 应为 False（已被 _reset_interrupt 清除）
            return "raw"

        eng._call_llm = fake_llm
        eng._parse_response = lambda resp: {"thought": "t", "final_answer": "done"}
        eng._input_requires_tools = lambda u: False

        result = eng.run("你好", AgentContext())
        assert seen["reset"] is False
        assert result == "done"


# ── Novel 中断 ──────────────────────────────────────────────
class _FakeProject:
    slug = "test"
    title = "测试小说"

    def get_all_context(self):
        return ""


class _FakeNovelManager:
    def detect_novel(self, user_input): return _FakeProject()
    def list_novels(self): return []
    def update_context(self, slug, operation, detail): pass


class TestNovelInterrupt:
    def test_interrupt_breaks_loop(self):
        cb = _RecordingCallback()
        eng = NovelEngine(
            ["m1"], max_iterations=5,
            novel_manager=_FakeNovelManager(), callback=cb,
        )
        calls = {"n": 0}

        def fake_llm(messages):
            calls["n"] += 1
            eng.interrupt()
            return "raw"

        eng._call_llm = fake_llm
        eng._parse_response = lambda resp: {"thought": "t", "action": "echo", "action_input": {}}
        eng._execute_tool = lambda action, action_input, ctx, tracker: "obs"

        eng.run("写第一章", AgentContext())
        assert calls["n"] == 1
        assert any("引擎被用户中断，停止迭代" in w for w in cb.warnings)


# ── Plan-Execute 中断 ──────────────────────────────────────
class TestPlanInterrupt:
    def test_interrupt_breaks_step_loop(self):
        cb = _RecordingCallback()
        eng = PlanExecuteEngine(["m1"], max_steps=5, callback=cb)

        eng._plan = lambda user_input, context=None: {
            "steps": [
                {"id": 1, "task": "a"},
                {"id": 2, "task": "b"},
                {"id": 3, "task": "c"},
            ],
            "analysis": "",
        }

        def fake_step(step_id, total, task, prev, user_input, tracker, context=None):
            eng.interrupt()  # 第 1 步执行中置位
            return "step1 ok"

        eng._execute_step_with_llm = fake_step
        eng._summarize = lambda *a, **k: "summary"

        eng.run("做三步", AgentContext())
        assert cb.steps == [1]  # 仅第 1 步 on_step 被调用，第 2 步前已退出
        assert any("引擎被用户中断，停止执行" in w for w in cb.warnings)
