"""Tests for ContextManager — 对话历史与压缩。"""
from omniagent.repl.context_manager import ContextManager


# ── B5: compact 空压缩 short-circuit ──────────────────────
class TestCompact:
    def test_short_history_no_compression(self):
        """B5: 历史不足以压缩时直接返回，不反向增加消息。"""
        cm = ContextManager()
        cm.add_user_message("你好")
        cm.add_assistant_message("你好！")
        cm.add_user_message("再见")
        cm.add_assistant_message("再见！")
        n_before = len(cm.history)
        result = cm.compact()
        assert len(cm.history) == n_before  # 不反向增加
        assert "无需压缩" in result

    def test_long_history_few_user_rounds_no_increase(self):
        """B5: 历史很长但 user 轮数 <3（older 为空）也不反向增加消息。

        旧逻辑在此场景下会凭空多写一条摘要消息（cut_idx==0 且 history>6
        不触发短路径，older 为空却把 old_count 记成 len(history)）。
        """
        cm = ContextManager()
        cm.add_user_message("问题")
        for _ in range(8):
            cm.add_assistant_message("回答" * 50)
        n_before = len(cm.history)
        result = cm.compact()
        assert len(cm.history) == n_before
        assert "无需压缩" in result

    def test_manual_summary_but_nothing_to_compress(self):
        """B5: 给了手动摘要但没有可压缩内容时，返回摘要且不改写历史。"""
        cm = ContextManager()
        cm.add_user_message("仅一轮")
        cm.add_assistant_message("回答")
        n_before = len(cm.history)
        result = cm.compact(summary="手动摘要")
        assert len(cm.history) == n_before
        assert result == "手动摘要"

    def test_normal_compression_still_works(self):
        """B5 回归保护：≥3 轮 user 对话时正常压缩。"""
        cm = ContextManager()
        for i in range(5):
            cm.add_user_message(f"问题{i}")
            cm.add_assistant_message(f"回答{i}")
        # 10 条消息；cut_idx=4 → older=4 条, recent=6 条 → 压缩后 1+6=7
        result = cm.compact(summary="手动摘要")
        assert len(cm.history) == 7
        assert result == "手动摘要"
        assert cm.history[0].role == "system"
        assert "之前 4 条消息" in cm.history[0].content
