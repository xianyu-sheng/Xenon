"""Mid-task steering 机制回归测试。

背景：Codex / Claude Code / Hermes 都支持任务运行中用户补充要求，
Xenon 此前是同步阻塞模型——引擎 run() 期间 stdin 不被读取，用户只能
Ctrl+C 取消，无法「转向」。本特性在 BaseEngine 抽象层实现 steering
通道（7 引擎一处继承），引擎循环检查点消费补充消息并让 LLM 自行
判断如何调整后续步骤。
"""

from __future__ import annotations

from xenon.engine.base import BaseEngine
from xenon.engine.context import AgentContext
from xenon.engine.plan_execute_engine import PlanExecuteEngine
from xenon.engine.react_engine import ReActEngine


class _MinimalEngine(BaseEngine):
    """只测试 BaseEngine steering 原语的最小引擎。"""

    def run(self, user_input: str, context: AgentContext | None = None) -> str:
        self._reset_steering()
        drained = self._drain_steering()
        return ",".join(m["text"] for m in drained)


class TestBaseEngineSteeringPrimitives:
    def test_steer_enqueues_and_drain_returns_fifo(self):
        eng = _MinimalEngine(model_priority=["deepseek/deepseek-v4-flash"])
        assert eng.steer("补充要求 A")
        assert eng.steer("补充要求 B")
        drained = eng._drain_steering()
        assert [m["text"] for m in drained] == ["补充要求 A", "补充要求 B"]
        # 消费后队列为空，且记录到 steering_consumed
        assert eng._drain_steering() == []
        assert [m["text"] for m in eng.steering_consumed] == ["补充要求 A", "补充要求 B"]

    def test_steer_rejects_blank(self):
        eng = _MinimalEngine(model_priority=["deepseek/deepseek-v4-flash"])
        assert eng.steer("") is False
        assert eng.steer("   ") is False
        assert eng._drain_steering() == []

    def test_steering_does_not_cross_runs(self):
        eng = _MinimalEngine(model_priority=["deepseek/deepseek-v4-flash"])
        # 真实引擎在 run() 起点 _reset_steering()，所以 run 前注入的消息
        # 会被本次 run 的 reset 清掉（不跨 run 串扰是设计意图）。
        # 这里验证：reset 后队列为空、steering_consumed 不残留上次的。
        eng.steer("旧要求")
        eng._reset_steering()
        assert eng._drain_steering() == []
        assert eng.steering_consumed == []

    def test_steering_prompt_contains_supplement(self):
        prompt = BaseEngine.steering_prompt(
            [{"text": "把输出改成 JSON 格式"}]
        )
        assert "把输出改成 JSON 格式" in prompt
        assert "原任务继续有效" in prompt


class _MockCaller:
    """可注入的假 LLM 调用器：按调用顺序返回预设响应，并记录 messages。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []

    def __call__(self, phase: str, messages: list[dict], **kwargs):
        self.calls.append(messages)
        if self.responses:
            return self.responses.pop(0)
        return '{"thought": "t", "final_answer": "默认收尾"}'


class TestReActConsumesSteering:
    def test_steering_injected_between_iterations(self, monkeypatch):
        engine = ReActEngine(
            model_priority=["deepseek/deepseek-v4-flash"],
            max_iterations=3,
            native_fc=False,  # 强制走文本 _call_llm_for_phase，避免真实 API
        )

        def fake_call(phase, messages, **kwargs):
            # 第一次调用：模拟任务运行中用户补充要求（REPL 监听线程 steer）
            if len(engine.steering_consumed) == 0:
                engine.steer("补充：请输出 JSON 格式")
                return '{"thought": "第一步", "action": "search_files", "action_input": {"pattern": "x"}}'
            return '{"thought": "收到补充，调整方向", "final_answer": "完成，按补充要求输出"}'

        monkeypatch.setattr(engine, "_call_llm_for_phase", fake_call)
        # 工具执行用假 executor：search_files 返回空结果
        monkeypatch.setattr(
            engine, "_execute_tool",
            lambda *a, **k: "未找到匹配文件",
        )

        result = engine.run("分析这个项目", context=AgentContext())
        # steering 在第二次迭代检查点被消费
        assert [m["text"] for m in engine.steering_consumed] == ["补充：请输出 JSON 格式"]
        assert "完成" in result


class TestCombinedEnginesConsumeSteering:
    """组合引擎（非 BaseEngine 子类）通过 SteeringMixin 消费 steering。

    组合引擎在 run() 阶段边界消费并拼进传给子引擎的 prompt——子引擎
    run() 起点会 _reset_steering()，所以 steering 必须在组合层持有。
    """

    def test_plan_react_steer_into_step_input(self, monkeypatch):
        from xenon.engine.combined_engines import PlanReactEngine
        engine = PlanReactEngine(
            model_priority=["deepseek/deepseek-v4-flash"],
            max_steps=3,
        )
        seen = {"text": ""}
        injected = {"done": False}

        def fake_plan(user_input, context=None):
            if not injected["done"]:
                engine.steer("补充：用中文写注释")
                injected["done"] = True
            return {"analysis": "计划", "steps": [{"id": 1, "task": "实现功能"}, {"id": 2, "task": "写测试"}]}

        monkeypatch.setattr(engine.planner, "_plan", fake_plan)

        def fake_react_run(input_text, context=None, ctx_mgr=None):
            if "用中文写注释" in input_text:
                seen["text"] = input_text
            return "步骤完成"

        monkeypatch.setattr(engine.reactor, "run", fake_react_run)
        engine._summarize = lambda *a, **k: "汇总"

        engine.run("开发一个功能", context=AgentContext())
        assert engine.steering_consumed
        assert "用中文写注释" in seen["text"]

    def test_reflection_combination_steer_into_repair_prompt(self, monkeypatch):
        from xenon.engine.combined_engines import ReactReflectionEngine
        engine = ReactReflectionEngine(
            model_priority=["deepseek/deepseek-v4-flash"],
            review_rounds=1,
        )
        seen = {"text": ""}
        injected = {"done": False}

        def fake_react_run(input_text, context=None, ctx_mgr=None):
            if not injected["done"]:
                engine.steer("补充：输出 Markdown 表格")
                injected["done"] = True
            return "初步结果"

        monkeypatch.setattr(engine.reactor, "run", fake_react_run)

        def fake_review(user_input, output, **kwargs):
            return {"pass": False, "score": 3, "feedback": "需要改进", "issues": []}

        monkeypatch.setattr(engine.reflector, "review_existing", fake_review)

        def fake_repair(input_text, context=None, ctx_mgr=None):
            if "输出 Markdown 表格" in input_text:
                seen["text"] = input_text
            return "修复后结果"

        monkeypatch.setattr(engine.repairer, "run", fake_repair)

        engine.run("写一个工具", context=AgentContext())
        assert engine.steering_consumed
        assert "输出 Markdown 表格" in seen["text"]

    def test_reflection_engine_steer_into_feedback(self, monkeypatch):
        from xenon.engine.reflection_engine import ReflectionEngine
        engine = ReflectionEngine(
            model_priority=["deepseek/deepseek-v4-flash"],
            max_rounds=2,
        )
        seen = {"text": ""}
        injected = {"done": False}

        def fake_execute(user_input, feedback, ctx):
            if "补充：加单元测试" in str(feedback):
                seen["text"] = str(feedback)
            if not injected["done"]:
                engine.steer("补充：加单元测试")
                injected["done"] = True
            return "输出 v1"

        monkeypatch.setattr(engine, "_execute", fake_execute)

        def fake_review(user_input, output):
            return {"score": 2, "feedback": "不够好", "issues": []}

        monkeypatch.setattr(engine, "_review", fake_review)

        engine.run("写代码", context=AgentContext())
        assert engine.steering_consumed
        assert "加单元测试" in seen["text"]

    def test_review_pass_with_steering_still_repairs(self, monkeypatch):
        """review pass 但存在 steering 时不得直接收尾——必须进入修复。

        回归：此前 steering 在 repair 阶段才消费，review pass 会提前
        return，补充要求被静默丢弃（真实 LLM 多引擎测试发现）。
        """
        from xenon.engine.combined_engines import ReactReflectionEngine
        engine = ReactReflectionEngine(
            model_priority=["deepseek/deepseek-v4-flash"],
            review_rounds=1,
        )
        repaired = {"called": False}
        injected = {"done": False}

        def fake_react_run(input_text, context=None, ctx_mgr=None):
            if not injected["done"]:
                engine.steer("补充：输出 Markdown 表格")
                injected["done"] = True
            return "初步结果"

        monkeypatch.setattr(engine.reactor, "run", fake_react_run)

        def fake_review(user_input, output, **kwargs):
            # review 通过——此前会直接收尾，steering 永不消费
            return {"pass": True, "score": 9, "feedback": "很好", "issues": []}

        monkeypatch.setattr(engine.reflector, "review_existing", fake_review)

        def fake_repair(input_text, context=None, ctx_mgr=None):
            repaired["called"] = True
            assert "输出 Markdown 表格" in input_text
            return "修复后结果"

        monkeypatch.setattr(engine.repairer, "run", fake_repair)

        engine.run("写一个工具", context=AgentContext())
        # steering 被消费（即使 review pass）
        assert engine.steering_consumed
        # 修复阶段被强制触发，且携带补充要求
        assert repaired["called"]


class TestPlanExecuteConsumesSteering:
    def _engine(self):
        return PlanExecuteEngine(
            model_priority=["deepseek/deepseek-v4-flash"],
            max_steps=5,
        )

    def _plan_with_write(self):
        # 计划必须含写工具步骤，否则被 PlanCompletenessGate 拦截（既有设计）
        return (
            '{"analysis": "计划", "steps": ['
            '{"id": 1, "task": "读取文件", "tool": null}, '
            '{"id": 2, "task": "修改代码", "tool": "write_file", '
            '"params": {"file_path": "/tmp/x.txt", "content": "x"}}]}'
        )

    def test_serial_path_injects_steering_into_step_prompt(self, monkeypatch):
        engine = self._engine()
        step_responses = [
            '{"thought": "读文件", "final_answer": "已读取"}',
            '{"thought": "改代码", "final_answer": "已修改"}',
            '{"thought": "补救写文件", "final_answer": "已写入"}',  # Phase 2.5 补救执行
        ]
        seen = {"text": ""}
        injected = {"done": False}

        def fake_call(phase, messages, **kwargs):
            if phase == "plan":
                # 计划生成后、步骤执行前：模拟运行中用户补充
                if not injected["done"]:
                    engine.steer("补充：改成异步实现")
                    injected["done"] = True
                return self._plan_with_write()
            if phase == "execute_step":
                if "用户中途补充" in str(messages):
                    seen["text"] = str(messages)
                return step_responses.pop(0) if step_responses else '{"thought": "t", "final_answer": "步骤完成"}'
            return "汇总完成"

        monkeypatch.setattr(engine, "_call_llm_for_phase", fake_call)

        engine.run("重构模块", context=AgentContext())

        assert engine.steering_consumed
        assert "改成异步实现" in seen["text"]

    def test_dag_path_consumes_steering_at_wave_checkpoint(self, monkeypatch):
        """DAG 路径：波次检查点消费 steering 并注入串行波次步骤。"""
        engine = self._engine()
        plan = (
            '{"analysis": "计划", "steps": ['
            '{"id": 1, "task": "A", "tool": null}, '
            '{"id": 2, "task": "B", "tool": "write_file", '
            '"params": {"file_path": "/tmp/x.txt", "content": "x"}, "depends_on": [1]}]}'
        )
        step_seen = {"text": ""}
        injected = {"done": False}

        def fake_call(phase, messages, **kwargs):
            if phase == "plan":
                if not injected["done"]:
                    engine.steer("补充：注意边界情况")
                    injected["done"] = True
                return plan
            if phase == "execute_step":
                if "用户中途补充" in str(messages):
                    step_seen["text"] = str(messages)
                return '{"thought": "t", "final_answer": "步骤完成"}'
            return "汇总完成"

        monkeypatch.setattr(engine, "_call_llm_for_phase", fake_call)

        engine.run("执行计划", context=AgentContext())
        assert engine.steering_consumed
        assert "注意边界情况" in step_seen["text"]
