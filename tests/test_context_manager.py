"""Tests for ContextManager — 对话历史与压缩。"""

import os

import pytest

from xenon.repl.context_manager import ContextManager


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


class TestLongSessionStability:
    """长时稳定性（C6）：连续多轮对话无句柄泄漏、compact 真实路径稳定。"""

    def test_60_rounds_no_fd_leak_and_compact_stable(self):
        """60 轮对话：fd 不增长，compact 按需触发并保留摘要。"""
        if not os.path.isdir("/proc/self/fd"):
            pytest.skip("fd accounting requires /proc/self/fd (Linux)")
        def count_fds():
            return len(os.listdir("/proc/self/fd"))

        fd0 = count_fds()
        cm = ContextManager()
        cm.max_tokens = 3000
        cm.compact_threshold = 0.5
        for i in range(60):
            cm.add_user_message(f"第 {i} 轮：{'长文本内容 ' * 20}")
            cm.add_assistant_message(f"回答 {i}：{'结果内容 ' * 10}")
            if cm.needs_compact():
                cm.compact(f"摘要 {i}")
        fd1 = count_fds()
        # 不允许句柄泄漏（允许 ±5 抖动）
        assert fd1 - fd0 <= 5, f"fd 泄漏: {fd0} -> {fd1}"
        # compact 真实触发过：history 被压缩且保留摘要
        assert len(cm.history) < 120
        assert any("摘要" in m.content for m in cm.history)


class TestLightweightFollowupContext:
    """A contextual follow-up must see the turn it refers to.

    Real session: "对上面的第一点……" after a long review answer was answered
    "上一轮内容不存在" — the lightweight history projection dropped every
    assistant turn longer than 1200 chars, leaving a dangling user message.
    """

    def test_long_previous_answer_is_clipped_not_dropped(self):
        cm = ContextManager()
        answer = "1. 把认证抽成中间件\n" + ("细节 " * 600)  # > 1200 chars
        cm.add_user_message("请审阅仓库")
        cm.add_assistant_message(answer)
        cm.add_user_message("对上面的第一点继续")

        messages = cm.get_prompt_messages(
            intent="chat", current_user_input="对上面的第一点继续"
        )

        contents = [m["content"] for m in messages]
        assert any(c.startswith("1. 把认证抽成中间件") for c in contents), contents
        clipped = next(c for c in contents if c.startswith("1. 把认证抽成中间件"))
        assert len(clipped) <= cm._MAX_LIGHTWEIGHT_ASSISTANT_CHARS + 64
        assert "截断" in clipped

    def test_a_followup_without_context_still_gets_no_history(self):
        cm = ContextManager()
        cm.add_user_message("请审阅仓库")
        cm.add_assistant_message("1. 把认证抽成中间件")

        messages = cm.get_prompt_messages(
            intent="chat", current_user_input="写一首诗"
        )

        assert all("请审阅仓库" not in m["content"] for m in messages)
