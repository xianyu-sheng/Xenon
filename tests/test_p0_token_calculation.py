"""
P0-Critical: Token 计算修复专项测试。

验证内容：
1. _estimate_tokens() 真正集成 tiktoken
2. _subscribe_usage() 成功后有 debug 日志
3. _on_usage() 有详细 debug 日志输出所有字段
4. cumulative_tokens 正确累加
5. compact 后 _last_usage_source 更新为 "estimated"
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from xenon.repl.context_manager import ContextManager, _estimate_tokens


class TestTiktokenIntegration:
    """测试 tiktoken 集成。"""

    def test_estimate_tokens_uses_tiktoken_when_available(self, caplog):
        """验证 _estimate_tokens 优先使用 tiktoken。"""
        caplog.set_level(logging.DEBUG)

        text = "Hello, world! This is a test."

        # Mock tiktoken at the import level inside _estimate_tokens
        mock_enc = MagicMock()
        mock_enc.encode.return_value = [1, 2, 3, 4, 5, 6, 7]  # 7 tokens

        mock_tiktoken = MagicMock()
        mock_tiktoken.get_encoding.return_value = mock_enc

        with patch.dict("sys.modules", {"tiktoken": mock_tiktoken}):
            result = _estimate_tokens(text)

            # 验证调用了 tiktoken
            mock_tiktoken.get_encoding.assert_called_once_with("cl100k_base")
            mock_enc.encode.assert_called_once_with(text)
            assert result == 7

            # 不应该有 fallback 日志
            assert "tiktoken 计算失败" not in caplog.text

    def test_estimate_tokens_fallback_on_tiktoken_failure(self, caplog):
        """验证 tiktoken 失败时回退到启发式估算。"""
        caplog.set_level(logging.DEBUG)

        text = "Hello world"

        # Mock tiktoken to raise an error
        mock_tiktoken = MagicMock()
        mock_tiktoken.get_encoding.side_effect = ImportError("tiktoken not found")

        with patch.dict("sys.modules", {"tiktoken": mock_tiktoken}):
            result = _estimate_tokens(text)

            # 应该有 fallback 日志
            assert "tiktoken 计算失败，回退启发式估算" in caplog.text
            # 启发式估算应该返回合理值（不为 0）
            assert result > 0


class TestUsageCallbackLogs:
    """测试 usage 回调日志。"""

    def test_subscribe_usage_logs_success(self, caplog):
        """验证 _subscribe_usage 成功时记录 debug 日志。"""
        caplog.set_level(logging.DEBUG)

        with patch("xenon.utils.llm_client.register_usage_callback") as mock_register:
            mock_register.return_value = lambda: None  # mock unsub function

            ctx = ContextManager(track_real_usage=True)

            # 验证成功日志
            assert "Usage callback successfully registered" in caplog.text
            mock_register.assert_called_once()

    def test_on_usage_logs_details(self, caplog):
        """验证 _on_usage 记录详细的 usage 信息。"""
        caplog.set_level(logging.DEBUG)

        ctx = ContextManager()

        # 模拟 usage 对象
        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 100
        mock_usage.completion_tokens = 50
        mock_usage.total_tokens = 150

        ctx._on_usage("gpt-4", mock_usage, 1.23)

        # 验证详细日志
        assert "Usage callback triggered" in caplog.text
        assert "model=gpt-4" in caplog.text
        assert "prompt=100" in caplog.text
        assert "completion=50" in caplog.text
        assert "total=150" in caplog.text
        assert "latency=1.23s" in caplog.text


class TestCumulativeTokens:
    """测试累计 token 计数。"""

    def test_cumulative_tokens_accumulate_correctly(self):
        """验证 cumulative_tokens 跨多次调用正确累加。"""
        ctx = ContextManager()

        assert ctx._cumulative_tokens == 0
        assert ctx._last_usage_source == "none"

        # 第一次调用
        ctx.record_real_usage(100, 50, 150)
        assert ctx._cumulative_tokens == 150
        assert ctx._last_usage_source == "real"

        # 第二次调用（累加）
        ctx.record_real_usage(200, 100, 300)
        assert ctx._cumulative_tokens == 450  # 150 + 300
        assert ctx._last_usage_source == "real"

        # 第三次调用
        ctx.record_real_usage(50, 25, 75)
        assert ctx._cumulative_tokens == 525  # 450 + 75
        assert ctx._last_usage_source == "real"

    def test_cumulative_tokens_in_stats(self):
        """验证 stats() 返回累计 token 数。"""
        ctx = ContextManager()

        ctx.record_real_usage(100, 50, 150)
        ctx.record_real_usage(200, 100, 300)

        stats = ctx.stats()
        assert stats["cumulative_tokens"] == 450
        assert stats["token_source"] == "real"

    def test_cumulative_tokens_reset_on_clear(self):
        """验证 clear() 后累计 token 归零。"""
        ctx = ContextManager()

        ctx.record_real_usage(100, 50, 150)
        assert ctx._cumulative_tokens == 150

        ctx.clear()
        assert ctx._cumulative_tokens == 0
        assert ctx._last_usage_source == "none"


class TestCompactUpdatesSource:
    """测试 compact 后状态更新。"""

    def test_compact_updates_source_to_estimated(self):
        """验证 compact 后 _last_usage_source 更新为 'estimated'。"""
        ctx = ContextManager(max_tokens=100)  # 低阈值，触发压缩

        # 添加足够多的消息以触发压缩
        for i in range(10):
            ctx.add_user_message(f"User message {i}" * 10)
            ctx.add_assistant_message(f"Assistant response {i}" * 10)

        # 记录真实 usage
        ctx.record_real_usage(100, 50, 150)
        assert ctx._last_usage_source == "real"
        initial_cumulative = ctx._cumulative_tokens
        assert initial_cumulative == 150

        # 触发 compact（提供 summary，避免调用 LLM）
        summary = ctx.compact(summary="Test summary")

        # 验证 source 已更新为 estimated
        assert ctx._last_usage_source == "estimated"
        # 验证 cumulative_tokens 已重新估算
        assert ctx._cumulative_tokens > 0

    def test_compact_recalculates_cumulative_tokens(self):
        """验证 compact 后重新估算 cumulative_tokens。"""
        ctx = ContextManager(max_tokens=100)  # 低阈值

        # 添加足够多的消息
        for i in range(10):
            ctx.add_user_message(f"Message {i}" * 10)
            ctx.add_assistant_message(f"Response {i}" * 10)

        # 记录真实 usage（模拟一个很大的值）
        ctx.record_real_usage(1000, 500, 1500)
        assert ctx._cumulative_tokens == 1500

        # compact（提供 summary）
        ctx.compact(summary="Compacted summary")

        # compact 后应该重新估算（基于压缩后的 history）
        # 压缩后 history 变少，cumulative_tokens 应该显著减小
        assert ctx._last_usage_source == "estimated"
        # 压缩后的 history 应该比原始的小很多
        assert ctx._cumulative_tokens < 1500

    def test_undo_clears_real_usage(self):
        """验证 undo 后真实 usage 失效。"""
        ctx = ContextManager()

        ctx.save_snapshot()
        ctx.add_user_message("Message 1")
        ctx.record_real_usage(100, 50, 150)

        assert ctx._real_usage is not None
        assert ctx._last_usage_source == "real"

        # undo 回退
        success = ctx.undo()
        assert success

        # 真实 usage 应该失效
        assert ctx._real_usage is None


class TestDebugTokens:
    """测试 debug_tokens() 方法。"""

    def test_debug_tokens_returns_detailed_info(self):
        """验证 debug_tokens 返回详细的 token 信息。"""
        ctx = ContextManager()

        ctx.add_user_message("Hello")
        ctx.add_assistant_message("Hi there!")
        ctx.record_real_usage(100, 50, 150)

        debug_info = ctx.debug_tokens()

        assert "history_length" in debug_info
        assert "estimated_total" in debug_info
        assert "current_usage" in debug_info
        assert "cumulative_tokens" in debug_info
        assert "last_usage_source" in debug_info
        assert "real_usage" in debug_info
        assert "per_turn" in debug_info

        assert debug_info["history_length"] == 2
        assert debug_info["cumulative_tokens"] == 150
        assert debug_info["last_usage_source"] == "real"
        assert debug_info["real_usage"] == {"prompt": 100, "completion": 50, "total": 150}

    def test_debug_tokens_per_turn_details(self):
        """验证 debug_tokens 返回每轮 token 详情。"""
        ctx = ContextManager()

        ctx.add_user_message("Short message")
        ctx.add_assistant_message("Another short response")

        debug_info = ctx.debug_tokens()
        per_turn = debug_info["per_turn"]

        assert len(per_turn) == 2
        assert per_turn[0]["role"] == "user"
        assert per_turn[0]["tokens"] > 0
        assert per_turn[1]["role"] == "assistant"
        assert per_turn[1]["tokens"] > 0
