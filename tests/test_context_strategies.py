"""v0.5.0: 分层上下文策略测试。"""
import pytest
from xenon.repl.context_strategies import (
    TieredStrategySelector,
    ToolOutputClassifier,
    ToolOutputType,
    ImportanceCalculator,
    SpaceBudget,
    handle_crisis,
    _compress_structural,
    _compress_transient,
    _compress_pointer,
    _compress_mutation,
    _compress_meta,
)


# ── 策略选择 ────────────────────────────────────────────────

class TestTieredStrategySelector:
    def test_select_q1(self):
        s = TieredStrategySelector().select(1)
        assert s.tier == 1
        assert s.trigger_threshold == 0.50
        assert s.tool_output_max_chars == 100
        assert s.summary_segments == 3
        assert s.preserve_reasoning is False
        assert s.decay_rate == 0.75
        assert s.keep_recent_rounds == 1
        assert s.crisis_action == "drop"

    def test_select_q3(self):
        s = TieredStrategySelector().select(3)
        assert s.tier == 3
        assert s.trigger_threshold == 0.60
        assert s.summary_segments == 6
        assert s.crisis_action == "auto_summary"

    def test_select_q5(self):
        s = TieredStrategySelector().select(5)
        assert s.tier == 5
        assert s.trigger_threshold == 0.85
        assert s.tool_output_max_chars == 2000
        assert s.preserve_reasoning is True
        assert s.decay_rate == 0.96
        assert s.keep_recent_rounds == 5
        assert s.crisis_action == "cross_tier_evict"

    def test_select_clamps_out_of_range(self):
        """超出范围的 tier 被 clamp 到 1-5。"""
        s0 = TieredStrategySelector().select(0)
        assert s0.tier == 1
        s6 = TieredStrategySelector().select(6)
        assert s6.tier == 5

    def test_select_with_explore_phase(self):
        """EXPLORE 阶段工具输出保留长度 ×4。"""
        s = TieredStrategySelector().select(3, phase="explore")
        assert s.tool_output_max_chars == 2000  # 500 * 4

    def test_select_with_converge_phase(self):
        """CONVERGE 阶段工具输出保留长度 ×0.3 + 阈值降低。"""
        s = TieredStrategySelector().select(3, phase="converge")
        assert s.tool_output_max_chars == 150  # int(500 * 0.3)
        assert s.trigger_threshold == pytest.approx(0.45)  # 0.60 - 0.15


# ── 工具输出分类 ────────────────────────────────────────────

class TestToolOutputClassifier:
    def test_classify_structural(self):
        assert ToolOutputClassifier.classify("read_file") == ToolOutputType.STRUCTURAL
        assert ToolOutputClassifier.classify("ast_analyze") == ToolOutputType.STRUCTURAL

    def test_classify_transient(self):
        assert ToolOutputClassifier.classify("command") == ToolOutputType.TRANSIENT
        assert ToolOutputClassifier.classify("web_fetch") == ToolOutputType.TRANSIENT

    def test_classify_pointer(self):
        assert ToolOutputClassifier.classify("search_files") == ToolOutputType.POINTER
        assert ToolOutputClassifier.classify("list_files") == ToolOutputType.POINTER

    def test_classify_mutation(self):
        assert ToolOutputClassifier.classify("write_file") == ToolOutputType.MUTATION
        assert ToolOutputClassifier.classify("edit_file") == ToolOutputType.MUTATION

    def test_classify_meta(self):
        assert ToolOutputClassifier.classify("git") == ToolOutputType.META
        assert ToolOutputClassifier.classify("mcp_call") == ToolOutputType.META

    def test_classify_unknown(self):
        """未知工具默认按 TRANSIENT 处理。"""
        assert ToolOutputClassifier.classify("unknown_tool") == ToolOutputType.TRANSIENT

    def test_compress_short_output_unchanged(self):
        """短输出不压缩。"""
        out = "hello"
        result = ToolOutputClassifier.compress("command", out, max_chars=500)
        assert result == out

    def test_compress_long_command(self):
        """长命令输出被截断。"""
        out = "x" * 2000
        result = ToolOutputClassifier.compress("command", out, max_chars=500)
        assert len(result) < len(out)
        assert "省略" in result

    def test_compress_structural_preserves_signatures(self):
        """结构化输出保留函数签名。"""
        out = "\n".join([
            "def foo(a, b):",
            "    " + "x" * 500,
            "def bar(c):",
            "    " + "y" * 500,
        ])
        result = ToolOutputClassifier.compress("read_file", out, max_chars=200)
        assert "foo(a, b)" in result or "def foo" in result
        assert len(result) < len(out)

    def test_compress_many(self):
        """批量压缩。"""
        outputs = [("cmd1", "a" * 100), ("cmd2", "b" * 2000)]
        results = ToolOutputClassifier.compress_many(outputs, max_chars=500)
        assert len(results) == 2
        assert results[0] == "a" * 100  # 短输出不变
        assert len(results[1]) < 2000  # 长输出被压缩


# ── 重要性衰减 ──────────────────────────────────────────────

class TestImportanceCalculator:
    def test_tier_score_q5(self):
        assert ImportanceCalculator.tier_score(5) == 1.0

    def test_tier_score_q1(self):
        assert ImportanceCalculator.tier_score(1) == 0.2

    def test_no_decay_at_zero_distance(self):
        """距离为 0 时无衰减。"""
        score = ImportanceCalculator.effective_importance(5, 10, 10, 0.90)
        assert score == 1.0

    def test_decay_over_distance(self):
        """距离增加时重要性衰减。"""
        s0 = ImportanceCalculator.effective_importance(3, 10, 10, 0.90)
        s5 = ImportanceCalculator.effective_importance(3, 5, 10, 0.90)
        assert s0 == 0.6  # tier_score(3) = 3/5 = 0.6
        assert s5 < s0  # distance=5 → 0.6 * 0.9^5

    def test_q1_decays_faster_than_q5(self):
        """Q1 比 Q5 衰减更快。"""
        q5 = ImportanceCalculator.effective_importance(5, 0, 10, 0.96)
        q1 = ImportanceCalculator.effective_importance(1, 0, 10, 0.75)
        assert q1 < q5  # Q1 衰减后重要性远低于 Q5

    def test_filter_by_importance(self):
        """过滤低重要性轮次。"""
        from xenon.repl.context_manager import ConversationTurn
        turns = [
            ConversationTurn(role="user", content="重要问题", task_tier=5, turn_index=5, turn_type="user_input"),
            ConversationTurn(role="assistant", content="知道了", task_tier=1, turn_index=6, turn_type="assistant_output"),
        ]
        filtered = ImportanceCalculator.filter_by_importance(turns, current_index=10, decay_rate=0.85, min_score=0.1)
        # user_input 始终保留
        assert len(filtered) >= 1
        assert filtered[0].content == "重要问题"


# ── 空间预算 ────────────────────────────────────────────────

class TestSpaceBudget:
    def test_ample(self):
        assert SpaceBudget.evaluate(0.50) == "ample"
        assert SpaceBudget.evaluate(0.80) == "ample"
        assert SpaceBudget.can_call_llm(0.80) is True

    def test_tight(self):
        assert SpaceBudget.evaluate(0.90) == "tight"
        assert SpaceBudget.can_call_llm(0.90) is True

    def test_critical(self):
        assert SpaceBudget.evaluate(0.96) == "critical"
        assert SpaceBudget.can_call_llm(0.96) is False


# ── 危急处理 ────────────────────────────────────────────────

class TestCrisisHandling:
    def _make_turn(self, content, tier=1, role="user", tt="user_input", idx=0):
        from xenon.repl.context_manager import ConversationTurn
        return ConversationTurn(role=role, content=content, task_tier=tier, turn_type=tt, turn_index=idx)

    def test_q1_crisis_drop(self):
        """Q1 危急 → 直接丢弃 older。"""
        strategy = TieredStrategySelector().get_preset(1)
        older = [self._make_turn("你好", tier=1, idx=i) for i in range(3)]
        recent = [self._make_turn("最近消息", tier=1, idx=10)]
        new_hist, summary = handle_crisis(older, recent, strategy, 10)
        assert "丢弃" in summary
        assert len(new_hist) == 1  # 只剩 recent

    def test_q2_crisis_label(self):
        """Q2 危急 → 单行标注。"""
        strategy = TieredStrategySelector().get_preset(2)
        older = [self._make_turn("解释一下", tier=2, idx=i) for i in range(3)]
        recent = [self._make_turn("最近消息", tier=2, idx=10)]
        new_hist, summary = handle_crisis(older, recent, strategy, 10)
        assert "丢弃" in summary
        assert len(new_hist) == 2  # 1 label + 1 recent

    def test_q3_crisis_auto_summary(self):
        """Q3 危急 → _auto_summary() 正则兜底。"""
        strategy = TieredStrategySelector().get_preset(3)
        older = [
            self._make_turn("帮我写一个排序函数", tier=3, idx=0),
            self._make_turn("已创建 sort.py 文件", tier=3, role="assistant", tt="assistant_output", idx=1),
        ]
        recent = [self._make_turn("最近消息", tier=3, idx=10)]
        new_hist, summary = handle_crisis(older, recent, strategy, 10)
        assert len(summary) > 0
        assert len(new_hist) >= 1  # summary turn + recent

    def test_q4_crisis_structured_truncate(self):
        """Q4 危急 → 结构化截断。"""
        strategy = TieredStrategySelector().get_preset(4)
        older = [
            self._make_turn("长" * 500, tier=4, idx=i) for i in range(10)
        ]
        recent = [self._make_turn("最近", tier=4, idx=100)]
        new_hist, summary = handle_crisis(older, recent, strategy, 100)
        assert "截断" in summary
        # 保留 top 5 + recent
        assert len(new_hist) <= 6

    def test_q5_crisis_cross_tier_evict(self):
        """Q5 危急 → 跨 tier 驱逐。"""
        strategy = TieredStrategySelector().get_preset(5)
        older = [
            self._make_turn("Q1 消息", tier=1, idx=i) for i in range(3)
        ] + [
            self._make_turn("Q3 消息", tier=3, idx=i) for i in range(3, 6)
        ] + [
            self._make_turn("Q5 消息", tier=5, idx=i) for i in range(6, 9)
        ]
        recent = [self._make_turn("最近", tier=5, idx=100)]
        new_hist, summary = handle_crisis(older, recent, strategy, 100)
        # Q1 被丢弃，Q3 被摘要，Q5 被保留 → 总消息数减少
        assert len(new_hist) < len(older) + len(recent)
        # 至少保留了一些高 tier 消息和 recent
        assert len(new_hist) >= 2  # 至少 Q4-Q5 保留 + recent


# ── 压缩函数 ────────────────────────────────────────────────

class TestCompressFunctions:
    def test_structural_preserves_def(self):
        out = "def foo():\n    pass\n\ndef bar():\n    return 1\n"
        result = _compress_structural(out * 100, 200)
        assert "def foo" in result
        assert "省略" in result

    def test_transient_truncates(self):
        out = "line1\nline2\n" * 500
        result = _compress_transient(out, 200)
        assert "省略" in result
        assert len(result) < len(out)

    def test_pointer_extracts_paths(self):
        out = "/home/user/app.py\n/home/user/test.py\n/home/user/README.md\n" * 100
        result = _compress_pointer(out, 200)
        assert "app.py" in result
        assert "test.py" in result
        assert len(result) < len(out)

    def test_mutation_shows_diff(self):
        out = "+added line\n-removed line\n" * 100
        result = _compress_mutation(out, 200)
        assert "变更" in result

    def test_meta_summarizes(self):
        out = "commit abc123\nAuthor: test\n" * 100
        result = _compress_meta(out, 200)
        assert "元操作" in result
