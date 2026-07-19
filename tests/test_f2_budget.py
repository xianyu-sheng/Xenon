"""F2 BudgetManager 单测。"""

from __future__ import annotations

from xenon.engine.budget import (
    CONVERGE_BLOCKED_TOOLS,
    BudgetManager,
    BudgetPhase,
)


class TestBudgetBasics:
    def test_initial_state(self):
        b = BudgetManager(max_iterations=10)
        assert b.spent == 0
        assert b.bonus == 0
        assert b.total == 10
        assert b.remaining == 10
        assert b.can_continue() is True
        assert b.phase is BudgetPhase.EXPLORE

    def test_spend_advances_spent(self):
        b = BudgetManager(max_iterations=4)
        b.spend()
        b.spend()
        assert b.spent == 2
        assert b.remaining == 2

    def test_spend_zero_or_negative_noop(self):
        b = BudgetManager(max_iterations=4)
        b.spend(0)
        b.spend(-5)
        assert b.spent == 0

    def test_can_continue_false_when_exhausted(self):
        b = BudgetManager(max_iterations=2)
        b.spend(2)
        assert b.can_continue() is False
        assert b.remaining == 0


class TestPhases:
    def test_explore_phase_first_25pct(self):
        b = BudgetManager(max_iterations=10)
        b.spend(2)  # 2/10 = 20% < 25%
        assert b.is_explore_phase() is True
        assert b.phase is BudgetPhase.EXPLORE

    def test_execute_phase_middle(self):
        b = BudgetManager(max_iterations=10)
        b.spend(5)  # 50%
        assert b.is_execute_phase() is True
        assert b.phase is BudgetPhase.EXECUTE

    def test_converge_phase_last_25pct(self):
        b = BudgetManager(max_iterations=10)
        b.spend(8)  # 80% >= 75%
        assert b.is_converge_phase() is True
        assert b.phase is BudgetPhase.CONVERGE

    def test_phase_boundary_uses_base_not_bonus(self):
        """阶段边界基于 base 比例，bonus 不改变 CONVERGE 触发时机。"""
        b = BudgetManager(max_iterations=10, hollow_reward=5)
        b.spend(2)
        b.on_hollow_answer()  # bonus +5 → total 15
        assert b.total == 15
        b.spend(6)  # spent=8, base ratio 80% → CONVERGE
        assert b.is_converge_phase() is True
        assert b.can_continue() is True  # 8 < 15

    def test_zero_max_iterations(self):
        b = BudgetManager(max_iterations=0)
        assert b.ratio == 1.0
        assert b.is_converge_phase() is True
        assert b.can_continue() is False


class TestRewards:
    def test_on_compression_adds_default(self):
        b = BudgetManager(max_iterations=10)
        b.on_compression()
        assert b.bonus == 2
        assert b.total == 12
        assert b.rewards == [("compression", 2)]

    def test_on_hollow_answer_adds_default(self):
        b = BudgetManager(max_iterations=10)
        b.on_hollow_answer()
        assert b.bonus == 3
        assert b.rewards == [("hollow", 3)]

    def test_reward_custom_n(self):
        b = BudgetManager(max_iterations=10)
        b.on_compression(5)
        b.on_hollow_answer(1)
        assert b.bonus == 6
        assert b.rewards == [("compression", 5), ("hollow", 1)]

    def test_reward_zero_noop(self):
        b = BudgetManager(max_iterations=10)
        b.on_compression(0)
        assert b.bonus == 0

    def test_reward_capped_by_multiplier(self):
        """bonus 不超过 max_total_multiplier × base。"""
        b = BudgetManager(max_iterations=10, max_total_multiplier=2.0)
        b.on_compression(100)  # 请求 100，但 cap=20
        assert b.bonus == 10
        assert b.total == 20  # capped at 2×10

    def test_reward_refused_when_at_cap(self):
        b = BudgetManager(max_iterations=10, max_total_multiplier=2.0)
        b.on_compression(10)  # 满到 cap=20
        assert b.bonus == 10
        assert b.total == 20
        b.on_hollow_answer(5)  # 已达封顶，拒绝
        assert b.bonus == 10  # 不变
        assert b.rewards[-1] == ("hollow", 0)  # 记录尝试但发放 0

    def test_reward_extends_can_continue(self):
        b = BudgetManager(max_iterations=3)
        b.spend(3)
        assert b.can_continue() is False
        b.on_hollow_answer(2)
        assert b.total == 5
        assert b.can_continue() is True


class TestToolGating:
    def test_explore_allows_exploration_tools(self):
        b = BudgetManager(max_iterations=10)
        b.spend(1)  # EXPLORE
        allow, _ = b.allow_tool("list_files")
        assert allow is True

    def test_converge_blocks_exploration_tools(self):
        b = BudgetManager(max_iterations=10)
        b.spend(8)  # CONVERGE
        for tool in ("list_files", "search_files", "code_index", "ast_analyze",
                     "diff_preview", "web_fetch", "github_fetch"):
            allow, reason = b.allow_tool(tool)
            assert allow is False, f"{tool} 应被禁用"
            assert "收束阶段" in reason
            assert tool in reason

    def test_converge_allows_read_file(self):
        """read_file 在收束阶段仍允许（最终验证是合法的）。"""
        b = BudgetManager(max_iterations=10)
        b.spend(8)
        allow, reason = b.allow_tool("read_file")
        assert allow is True
        assert reason == ""

    def test_converge_allows_write_and_command(self):
        b = BudgetManager(max_iterations=10)
        b.spend(8)
        assert b.allow_tool("write_file")[0] is True
        assert b.allow_tool("command")[0] is True
        assert b.allow_tool("edit_file")[0] is True

    def test_blocked_set_contents(self):
        """CONVERGE_BLOCKED_TOOLS 恰为 7 个探索型工具。"""
        assert CONVERGE_BLOCKED_TOOLS == frozenset({
            "list_files", "search_files", "code_index", "ast_analyze",
            "diff_preview", "web_fetch", "github_fetch",
        })


class TestSummaryReset:
    def test_summary_contains_key_fields(self):
        b = BudgetManager(max_iterations=10)
        b.spend(8)
        b.on_compression()
        s = b.summary()
        assert "8" in s  # spent
        assert "12" in s  # total
        assert "converge" in s
        assert "剩余 4" in s

    def test_reset_clears_state(self):
        b = BudgetManager(max_iterations=10)
        b.spend(5)
        b.on_compression()
        b.reset()
        assert b.spent == 0
        assert b.bonus == 0
        assert b.rewards == []
        assert b.total == 10
