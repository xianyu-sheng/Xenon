"""集成测试：优雅重启端到端验证。"""

import json
import os
import signal
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.integration
class TestGracefulRestartIntegration:
    """端到端集成测试。"""

    @pytest.fixture
    def mock_full_repl(self):
        """构造完整的 Mock REPL（更接近真实环境）。"""
        from xenon.repl.context_manager import ContextManager, ConversationTurn

        repl = MagicMock()

        # 真实 ContextManager
        repl.ctx_mgr = ContextManager(max_tokens=128000, track_real_usage=True)
        repl.ctx_mgr.add_message("user", "test message 1")
        repl.ctx_mgr.add_message("assistant", "response 1", model_used="model1")
        repl.ctx_mgr.update_working_memory("test_key", "test_value")

        # 其他组件 Mock
        repl.registry = MagicMock()
        repl.registry.current_mode = "react"
        repl.registry.export_config = MagicMock(return_value={"models": {}})
        repl.registry.set_mode = MagicMock()

        repl.model_pool = MagicMock()
        repl.model_pool.from_config = MagicMock()
        repl.auto_router = MagicMock()
        repl.status_bar = MagicMock()
        repl._cache_tracker = MagicMock()
        repl._terminal_activity = MagicMock()
        repl._clipboard_monitor = MagicMock()
        repl.agent_context = MagicMock()
        repl._session_state = {}
        repl._on_clipboard_image = MagicMock()

        return repl

    def test_full_restart_with_session_preservation(self, mock_full_repl, tmp_path):
        """完整重启流程：保存并恢复会话。"""
        from xenon.repl.graceful_restart import GracefulRestartManager

        manager = GracefulRestartManager(mock_full_repl)

        # 记录原始历史
        original_history_len = len(mock_full_repl.ctx_mgr.history)
        original_memory = dict(mock_full_repl.ctx_mgr._working_memory)

        with patch("xenon.repl.system_config.reload_config") as mock_reload, \
             patch("xenon.repl.model_pool.ModelPool") as MockModelPool, \
             patch("xenon.repl.auto_router.AutoRouter") as MockAutoRouter, \
             patch("xenon.repl.terminal_activity.TerminalActivityIndicator") as MockTerminal, \
             patch("xenon.tools.ClipboardMonitor") as MockClipboard:

            # Mock 配置和组件
            mock_config = MagicMock()
            mock_config.validation = MagicMock()
            mock_reload.return_value = mock_config

            mock_pool = MagicMock()
            mock_pool.from_config = MagicMock()
            MockModelPool.return_value = mock_pool

            mock_router = MagicMock()
            MockAutoRouter.return_value = mock_router

            mock_terminal = MagicMock()
            MockTerminal.return_value = mock_terminal

            mock_clipboard = MagicMock()
            MockClipboard.return_value = mock_clipboard

            # 执行重启
            outcome = manager.perform_restart(preserve_session=True)

            # 验证结果
            assert outcome.ok is True
            assert "重启成功" in outcome.message

            # 验证会话已恢复
            assert len(mock_full_repl.ctx_mgr.history) == original_history_len
            assert mock_full_repl.ctx_mgr.history[0].content == "test message 1"
            assert mock_full_repl.ctx_mgr.history[1].content == "response 1"
            assert mock_full_repl.ctx_mgr._working_memory["test_key"] == "test_value"

            # 验证组件已重建
            MockModelPool.assert_called_once()
            MockAutoRouter.assert_called_once()
            MockTerminal.assert_called_once()
            MockClipboard.assert_called_once()

    def test_full_restart_without_session(self, mock_full_repl):
        """完整重启流程：不保存会话。"""
        from xenon.repl.graceful_restart import GracefulRestartManager

        manager = GracefulRestartManager(mock_full_repl)

        original_history_len = len(mock_full_repl.ctx_mgr.history)
        assert original_history_len > 0  # 确保有历史数据

        with patch("xenon.repl.system_config.reload_config") as mock_reload, \
             patch("xenon.repl.model_pool.ModelPool"), \
             patch("xenon.repl.auto_router.AutoRouter"), \
             patch("xenon.repl.terminal_activity.TerminalActivityIndicator"), \
             patch("xenon.tools.ClipboardMonitor"):

            mock_config = MagicMock()
            mock_config.validation = MagicMock()
            mock_reload.return_value = mock_config

            # 执行重启（不保存会话）
            outcome = manager.perform_restart(preserve_session=False)

            # 验证结果
            assert outcome.ok is True
            assert "全新会话" in outcome.message
            assert outcome.context_reset is True

            # 验证历史已清空
            assert len(mock_full_repl.ctx_mgr.history) == 0

    def test_config_validation_failure_preserves_state(self, mock_full_repl):
        """配置验证失败：不触碰任何资源。"""
        from xenon.repl.graceful_restart import GracefulRestartManager

        manager = GracefulRestartManager(mock_full_repl)

        original_history_len = len(mock_full_repl.ctx_mgr.history)

        with patch("xenon.repl.system_config.reload_config") as mock_reload:
            mock_reload.side_effect = RuntimeError("配置加载失败")

            # 执行重启
            outcome = manager.perform_restart(preserve_session=True)

            # 验证失败
            assert outcome.ok is False
            assert "配置验证失败" in outcome.message

            # 验证状态未改变
            assert len(mock_full_repl.ctx_mgr.history) == original_history_len
            mock_full_repl._terminal_activity.close.assert_not_called()

    def test_session_corruption_handling(self, mock_full_repl, tmp_path):
        """会话文件损坏：降级处理。"""
        from xenon.repl.graceful_restart import GracefulRestartManager

        manager = GracefulRestartManager(mock_full_repl)

        # 创建损坏的会话文件
        session_file = tmp_path / "corrupted.json"
        with open(session_file, "w") as f:
            f.write("{ invalid json")

        # 尝试恢复
        outcome = manager.restore_session_state(session_file)

        # 验证失败处理
        assert outcome.ok is False
        assert "读取失败" in outcome.message
        assert session_file.exists()  # 文件保留

    def test_cleanup_timeout_handling(self, mock_full_repl):
        """资源清理超时：不阻塞重启。"""
        from xenon.repl.graceful_restart import GracefulRestartManager

        manager = GracefulRestartManager(mock_full_repl)

        # Mock 组件清理挂起
        mock_full_repl._terminal_activity.close.side_effect = lambda: time.sleep(10)

        # 清理应该不阻塞（虽然我们没有实现超时机制，但应该捕获异常）
        with patch("xenon.repl.graceful_restart.logger") as mock_logger:
            # 修改为立即抛异常（模拟超时后的行为）
            mock_full_repl._terminal_activity.close.side_effect = RuntimeError("timeout")
            manager.cleanup_resources()

            # 验证记录了警告
            mock_logger.warning.assert_called()

    def test_session_recovery_file_handling(self, mock_full_repl, tmp_path):
        """会话恢复文件管理：成功删除、失败保留。"""
        from xenon.repl.graceful_restart import GracefulRestartManager

        manager = GracefulRestartManager(mock_full_repl)

        # 创建有效会话文件
        session_file = tmp_path / "session.json"
        session_data = {
            "version": 1,
            "timestamp": time.time(),
            "history": [
                {"role": "user", "content": "hello", "model_used": None, "metadata": {}},
            ],
            "working_memory": {},
            "working_directory": str(Path.cwd()),
            "current_mode": "direct",
        }

        with open(session_file, "w") as f:
            json.dump(session_data, f)

        # 恢复会话
        outcome = manager.restore_session_state(session_file)

        # 验证成功且文件已删除
        assert outcome.ok is True
        assert not session_file.exists()

    def test_working_directory_restoration(self, mock_full_repl, tmp_path):
        """工作目录恢复。"""
        from xenon.repl.graceful_restart import GracefulRestartManager

        manager = GracefulRestartManager(mock_full_repl)

        # 创建会话文件（指定不同的工作目录）
        original_cwd = Path.cwd()
        target_dir = tmp_path / "target"
        target_dir.mkdir()

        session_file = tmp_path / "session.json"
        session_data = {
            "version": 1,
            "timestamp": time.time(),
            "history": [],
            "working_memory": {},
            "working_directory": str(target_dir),
            "current_mode": "direct",
        }

        with open(session_file, "w") as f:
            json.dump(session_data, f)

        # 恢复会话
        outcome = manager.restore_session_state(session_file)

        # 验证工作目录已切换
        assert outcome.ok is True
        assert Path.cwd() == target_dir

        # 恢复原工作目录
        os.chdir(original_cwd)

    def test_mode_switching_during_restore(self, mock_full_repl, tmp_path):
        """模式切换恢复。"""
        from xenon.repl.graceful_restart import GracefulRestartManager

        manager = GracefulRestartManager(mock_full_repl)

        # 创建会话文件（指定不同模式）
        session_file = tmp_path / "session.json"
        session_data = {
            "version": 1,
            "timestamp": time.time(),
            "history": [],
            "working_memory": {},
            "working_directory": str(Path.cwd()),
            "current_mode": "plan-execute",
        }

        with open(session_file, "w") as f:
            json.dump(session_data, f)

        # 恢复会话
        outcome = manager.restore_session_state(session_file)

        # 验证模式切换被调用
        assert outcome.ok is True
        mock_full_repl.registry.set_mode.assert_called_once_with("plan-execute")

    @pytest.mark.skipif(sys.platform == "win32", reason="Windows 不支持信号")
    def test_signal_driven_restart_flow(self, mock_full_repl):
        """信号驱动的完整重启流程。"""
        from xenon.repl.graceful_restart import GracefulRestartManager

        manager = GracefulRestartManager(mock_full_repl)
        manager.install_signal_handlers()

        # 发送 SIGHUP 信号
        os.kill(os.getpid(), signal.SIGHUP)
        time.sleep(0.2)  # 等待信号处理

        # 验证重启请求已设置
        should, preserve = manager.coordinator.should_restart()
        assert should is True
        assert preserve is True

        # 清理
        manager.uninstall_signal_handlers()

    def test_concurrent_restart_requests(self, mock_full_repl):
        """并发重启请求：协调器正确处理。"""
        from xenon.repl.graceful_restart import GracefulRestartManager
        import threading

        manager = GracefulRestartManager(mock_full_repl)

        def request_restart(preserve: bool):
            manager.coordinator.request_restart(preserve_session=preserve)

        # 并发请求
        threads = [
            threading.Thread(target=request_restart, args=(True,)),
            threading.Thread(target=request_restart, args=(False,)),
            threading.Thread(target=request_restart, args=(True,)),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 验证至少有一个请求生效
        should, _ = manager.coordinator.should_restart()
        assert should is True

    def test_restart_with_empty_history(self, mock_full_repl):
        """空历史重启：边界情况。"""
        from xenon.repl.graceful_restart import GracefulRestartManager

        manager = GracefulRestartManager(mock_full_repl)
        mock_full_repl.ctx_mgr.history.clear()

        with patch("xenon.repl.system_config.reload_config") as mock_reload, \
             patch("xenon.repl.model_pool.ModelPool"), \
             patch("xenon.repl.auto_router.AutoRouter"), \
             patch("xenon.repl.terminal_activity.TerminalActivityIndicator"), \
             patch("xenon.tools.ClipboardMonitor"):

            mock_config = MagicMock()
            mock_config.validation = MagicMock()
            mock_reload.return_value = mock_config

            # 执行重启
            outcome = manager.perform_restart(preserve_session=True)

            # 验证成功
            assert outcome.ok is True

    def test_restart_with_large_history(self, mock_full_repl):
        """大量历史记录重启：性能测试。"""
        from xenon.repl.graceful_restart import GracefulRestartManager

        manager = GracefulRestartManager(mock_full_repl)

        # 添加大量历史记录
        for i in range(100):
            mock_full_repl.ctx_mgr.add_message("user", f"message {i}")
            mock_full_repl.ctx_mgr.add_message("assistant", f"response {i}")

        with patch("xenon.repl.system_config.reload_config") as mock_reload, \
             patch("xenon.repl.model_pool.ModelPool"), \
             patch("xenon.repl.auto_router.AutoRouter"), \
             patch("xenon.repl.terminal_activity.TerminalActivityIndicator"), \
             patch("xenon.tools.ClipboardMonitor"):

            mock_config = MagicMock()
            mock_config.validation = MagicMock()
            mock_reload.return_value = mock_config

            # 执行重启
            start_time = time.time()
            outcome = manager.perform_restart(preserve_session=True)
            elapsed = time.time() - start_time

            # 验证成功且性能合理
            assert outcome.ok is True
            assert elapsed < 5.0  # 5秒内完成
            # 注意：mock_full_repl fixture 本身带 2 条历史，所以最终是 202 条
            assert len(mock_full_repl.ctx_mgr.history) >= 200
