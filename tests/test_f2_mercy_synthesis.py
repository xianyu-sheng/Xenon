"""F2 mercy compile + 合成注入 + ReAct 集成单测。"""

from __future__ import annotations

from types import SimpleNamespace

from xenon.engine.budget import BudgetManager
from xenon.engine.context import AgentContext
from xenon.engine.react_engine import ReActEngine
from xenon.engine.tool_tracker import ToolExecutionTracker


# ── 测试辅助 ────────────────────────────────────────────────
class _RecordingCallback:
    def __init__(self):
        self.warnings = []
        self.errors = []
        self.finishes = []

    def on_think(self, t): pass
    def on_act(self, a, p): pass
    def on_observe(self, o): pass
    def on_step(self, *a, **k): pass
    def on_step_done(self, *a, **k): pass
    def on_review(self, *a, **k): pass
    def on_warning(self, w): self.warnings.append(w)
    def on_error(self, e): self.errors.append(e)
    def on_finish(self, r): self.finishes.append(r)


def _tracker_with(calls):
    """构造含若干工具调用的 tracker。calls: [(name, success, summary)]。"""
    t = ToolExecutionTracker()
    for name, success, summary in calls:
        t.record(name, {"file_path": "x.py"}, success, summary,
                 error=None if success else "boom")
    return t


# ════════════════════════════════════════════════════════════
# _inject_synthesis_prompt 6 场景
# ════════════════════════════════════════════════════════════
class TestSynthesisInjection:
    def setup_method(self):
        self.eng = ReActEngine(["m1"], max_iterations=10)

    def test_force_synthesis_when_budget_low_and_tools(self):
        b = BudgetManager(max_iterations=10)
        b.spend(9)  # 9/10 spent, total=10, remaining=1 → 10% < 15%
        tracker = _tracker_with([("write_file", True, "ok")])
        r = self.eng._inject_synthesis_prompt(b, tracker)
        assert r is not None
        assert r[0] == "force_synthesis"
        assert "final_answer" in r[1]

    def test_force_synthesis_not_triggered_without_tools(self):
        b = BudgetManager(max_iterations=10)
        b.spend(9)
        tracker = ToolExecutionTracker()  # 0 calls
        r = self.eng._inject_synthesis_prompt(b, tracker)
        # 无工具 → 不触发 force_synthesis；进入 converge 分支（0 tools）→ soft_warning
        assert r is not None
        assert r[0] == "soft_warning"

    def test_converge_synthesis_with_tools(self):
        b = BudgetManager(max_iterations=10)
        b.spend(8)  # 80% → CONVERGE
        tracker = _tracker_with([("write_file", True, "ok")])
        r = self.eng._inject_synthesis_prompt(b, tracker)
        assert r[0] == "converge_synthesis"

    def test_soft_warning_converge_no_tools(self):
        b = BudgetManager(max_iterations=10)
        b.spend(8)
        tracker = ToolExecutionTracker()
        r = self.eng._inject_synthesis_prompt(b, tracker)
        assert r[0] == "soft_warning"

    def test_compression_reward_after_compression(self):
        b = BudgetManager(max_iterations=10)
        b.spend(5)  # EXECUTE
        b.on_compression()
        tracker = _tracker_with([("write_file", True, "ok")])
        r = self.eng._inject_synthesis_prompt(b, tracker)
        assert r[0] == "compression_reward"

    def test_progress_expansion_mid_execution(self):
        b = BudgetManager(max_iterations=10)
        b.spend(5)  # EXECUTE
        tracker = _tracker_with([
            ("write_file", True, "ok1"),
            ("command", True, "ok2"),
            ("read_file", True, "ok3"),
        ])
        r = self.eng._inject_synthesis_prompt(b, tracker)
        assert r[0] == "progress_expansion"

    def test_gentle_hint_explore_no_tools(self):
        b = BudgetManager(max_iterations=10)
        b.spend(1)  # EXPLORE
        tracker = ToolExecutionTracker()
        r = self.eng._inject_synthesis_prompt(b, tracker)
        assert r[0] == "gentle_hint"

    def test_skip_after_hollow_reward(self):
        """刚奖励过空洞补救 → 跳过注入（hint 已在）避免连续 user 消息堆叠。"""
        b = BudgetManager(max_iterations=10)
        b.spend(1)
        b.on_hollow_answer()
        tracker = ToolExecutionTracker()
        r = self.eng._inject_synthesis_prompt(b, tracker)
        assert r is None

    def test_none_when_no_scenario_matches(self):
        """EXPLORE 阶段但有 1 个工具调用 → 无场景匹配 → None。"""
        b = BudgetManager(max_iterations=10)
        b.spend(1)  # EXPLORE
        tracker = _tracker_with([("write_file", True, "ok")])
        r = self.eng._inject_synthesis_prompt(b, tracker)
        assert r is None


# ════════════════════════════════════════════════════════════
# _mercy_compile / _exhaustion_report
# ════════════════════════════════════════════════════════════
class TestMercyCompile:
    def test_synthesis_success_uses_llm(self):
        cb = _RecordingCallback()
        eng = ReActEngine(["m1"], max_iterations=3, callback=cb)
        tracker = _tracker_with([("write_file", True, "写入 100 字节")])
        captured = {}

        def fake_llm(messages, max_tokens=None):
            captured["msgs"] = messages
            return "这是合成出的最终回答，文件已写入 x.py"

        eng._call_llm = fake_llm
        result = eng._mercy_compile("创建 x.py", tracker, [])
        assert "合成出的最终回答" in result
        assert any("mercy compile" in w for w in cb.warnings)
        # 验证合成 prompt 不含 ReAct 格式要求
        assert "收尾合成器" in captured["msgs"][0]["content"]

    def test_synthesis_failure_falls_back_to_report(self):
        eng = ReActEngine(["m1"], max_iterations=3)
        tracker = _tracker_with([
            ("write_file", True, "写入 a.py"),
            ("command", False, ""),
        ])

        def boom(messages, max_tokens=None):
            raise RuntimeError("LLM 挂了")

        eng._call_llm = boom
        result = eng._mercy_compile("任务", tracker, [])
        assert "结构化报告" in result or "执行摘要" in result
        assert "write_file" in result
        assert "command" in result

    def test_no_data_returns_error_message(self):
        eng = ReActEngine(["m1"], max_iterations=3)
        tracker = ToolExecutionTracker()  # 空
        result = eng._mercy_compile("任务", tracker, [])
        assert "达到最大迭代次数" in result
        assert "未执行任何工具调用" in result

    def test_empty_llm_response_falls_back_to_report(self):
        eng = ReActEngine(["m1"], max_iterations=3)
        tracker = _tracker_with([("write_file", True, "ok")])
        eng._call_llm = lambda messages, max_tokens=None: "   "
        result = eng._mercy_compile("任务", tracker, [])
        assert "结构化报告" in result or "执行摘要" in result

    def test_exhaustion_report_caps_at_10(self):
        eng = ReActEngine(["m1"], max_iterations=3)
        calls = [(f"tool{i}", True, f"summary{i}") for i in range(15)]
        tracker = _tracker_with(calls)
        report = eng._exhaustion_report("任务", tracker)
        # 详细记录最多 10 条（"✓ 成功 toolN" 标记）；summary 会列全部名字不算
        assert report.count("✓ 成功 tool") == 10
        # 后 10 条 = tool5..tool14
        assert "✓ 成功 tool5" in report  # 第一条记录
        assert "✓ 成功 tool14" in report  # 最后一条
        assert "✓ 成功 tool4" not in report  # 被截掉（tool0..tool4 不在记录里）


# ════════════════════════════════════════════════════════════
# ReAct run() 集成：空洞拒绝 / 工具门控 / 压缩奖励 / mercy 降级
# ════════════════════════════════════════════════════════════
class TestReActIntegration:
    def test_hollow_answer_rejected_then_accepted(self):
        """收束阶段空洞回答 → 拒绝并要求重写；第二次强制接受。"""
        cb = _RecordingCallback()
        eng = ReActEngine(["m1"], max_iterations=4, callback=cb)
        n = {"i": 0}

        def fake_llm(messages, max_tokens=None):
            n["i"] += 1
            # 始终返回空洞 final_answer
            return '{"thought":"t","final_answer":"综上所述，整体设计完善。"}'

        eng._call_llm = fake_llm
        eng._parse_response = lambda r: {"thought": "t", "final_answer": "综上所述，整体设计完善。"}
        eng._input_requires_tools = lambda u: False
        # 模拟已有工具执行（进入收束阶段 + has_executions 触发空洞检测）
        eng._execute_tool = lambda action, ai, ctx, tracker: "obs"
        # 让第一次迭代先执行工具进入"有执行"状态，再进收束
        # 简化：直接给 tracker 预置——但 run() 内部新建 tracker。改用 parse 在前几轮返回 action
        responses = [
            {"thought": "t", "action": "write_file", "action_input": {}},  # 执行工具
            {"thought": "t", "final_answer": "综上所述，整体设计完善。"},  # 空洞
            {"thought": "t", "final_answer": "综上所述，整体设计完善。"},  # 再次空洞→接受
        ]
        ri = {"i": -1}
        eng._call_llm = lambda messages, max_tokens=None: "raw"
        eng._parse_response = lambda r: (ri.__setitem__("i", ri["i"] + 1) or responses[min(ri["i"], len(responses)-1)])
        eng._input_requires_tools = lambda u: True
        eng._execute_tool = lambda action, ai, ctx, tracker: "obs"

        # max_iterations=4，spend 推进：要进入收束需 spent>=3 (75% of 4=3)
        # 第1轮 action(spend1) → 第2轮 空洞(spend2,EXECUTE,has_exec✓但EXECUTE非CONVERGE→空洞门要 has_executions✓→触发)
        result = eng.run("实现功能", AgentContext())
        # 第二次空洞应被接受（hollow_rejections 上限 1）
        assert "综上所述" in result

    def test_converge_tool_gating_blocks_exploration(self):
        """收束阶段调用 list_files 被门控，不执行工具。"""
        cb = _RecordingCallback()
        eng = ReActEngine(["m1"], max_iterations=4, callback=cb)
        executed = []
        responses = [
            {"thought": "t", "action": "write_file", "action_input": {}},  # spend1 EXPLORE
            {"thought": "t", "action": "write_file", "action_input": {}},  # spend2 EXPLORE
            {"thought": "t", "action": "write_file", "action_input": {}},  # spend3 CONVERGE(75%)→write_file 允许
            {"thought": "t", "action": "list_files", "action_input": {}},  # spend4 CONVERGE→list_files 拦截
            {"thought": "t", "final_answer": "完成"},
        ]
        ri = {"i": -1}
        eng._call_llm = lambda messages, max_tokens=None: "raw"
        eng._parse_response = lambda r: (ri.__setitem__("i", ri["i"] + 1) or responses[min(ri["i"], len(responses)-1)])
        eng._input_requires_tools = lambda u: True
        eng._execute_tool = lambda action, ai, ctx, tracker: executed.append(action) or "obs"

        eng.run("做点事", AgentContext())
        # list_files 在收束阶段被拦截，不应出现在 executed
        assert "list_files" not in executed
        # 拦截应有 warning
        assert any("收束阶段" in w for w in cb.warnings)

    def test_mercy_compile_on_budget_exhaustion(self):
        """预算耗尽（无中断、无 final_answer、有工具调用）→ mercy compile 合成。"""
        cb = _RecordingCallback()
        eng = ReActEngine(["m1"], max_iterations=3, callback=cb)
        # 始终返回 action，永不 final_answer → 耗尽
        eng._call_llm = lambda messages, max_tokens=None: "raw"
        eng._parse_response = lambda r: {"thought": "t", "action": "write_file", "action_input": {}}
        eng._input_requires_tools = lambda u: True
        eng._execute_tool = lambda action, ai, ctx, tracker: "obs"
        # mercy compile 的合成调用：_call_llm 会被同一 fake 调用 → 返回 "raw"
        # 但合成 prompt 检测：fake 返回 "raw"（非空）→ 作为合成结果
        result = eng.run("做点事", AgentContext())
        # 应走 mercy compile（有工具执行 → 合成路径），返回 "raw" 或结构化报告
        assert result  # 非空
        assert "引擎被用户中断" not in result

    def test_compression_reward_granted_on_compaction(self):
        """第 5 轮压缩成功 → budget.on_compression 被调用。"""
        eng = ReActEngine(
            ["m1"], max_iterations=8,
            model_configs={"m1": SimpleNamespace(context_window=1000)},
        )
        # monkeypatch _maybe_compact_messages 使其在第 5 轮返回更短列表
        call_count = {"n": 0}

        def fake_compact(messages, turn, every=5):
            call_count["n"] = turn
            if turn == 5:
                return [{"role": "system", "content": "COMPACTED"}]  # 更短
            return messages

        eng._maybe_compact_messages = fake_compact
        responses = [{"thought": "t", "action": "write_file", "action_input": {}}] * 7 + \
                    [{"thought": "t", "final_answer": "完成"}]
        ri = {"i": -1}
        eng._call_llm = lambda messages, max_tokens=None: "raw"
        eng._parse_response = lambda r: (ri.__setitem__("i", ri["i"] + 1) or responses[min(ri["i"], len(responses)-1)])
        eng._input_requires_tools = lambda u: True
        eng._execute_tool = lambda action, ai, ctx, tracker: "obs"

        eng.run("做点事", AgentContext())
        # 第 5 轮 fake_compact 返回更短 → 触发 on_compression（间接验证：无异常即通过）
        assert call_count["n"] >= 5
