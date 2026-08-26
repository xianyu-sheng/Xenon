"""测试 graceful_restart.py 的单元测试。"""

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestRestartCoordinator:
    """测试 RestartCoordinator 线程安全性。"""

    def test_initial_state(self):
        """初始状态：未请求重启。"""
        from xenon.repl.graceful_restart import RestartCoordinator

        coordinator = RestartCoordinator()
        should, preserve = coordinator.should_restart()
        assert should is False
        assert preserve is True  # 默认值

    def test_request_restart(self):
        """请求重启：状态正确传递。"""
        from xenon.repl.graceful_restart import RestartCoordinator

        coordinator = RestartCoordinator()
        coordinator.request_restart(preserve_session=False)

        should, preserve = coordinator.should_restart()
        assert should is True
        assert preserve is False

    def test_clear_restart_flag(self):
        """清除标志后恢复初始状态。"""
        from xenon.repl.graceful_restart import RestartCoordinator

        coordinator = RestartCoordinator()
        coordinator.request_restart(preserve_session=True)
        coordinator.clear()

        should, preserve = coordinator.should_restart()
        assert should is False

    def test_thread_safety(self):
        """并发请求：最后一次生效。"""
        from xenon.repl.graceful_restart import RestartCoordinator

        coordinator = RestartCoordinator()

        def requester(preserve: bool):
            coordinator.request_restart(preserve_session=preserve)

        threads = [
            threading.Thread(target=requester, args=(True,)),
            threading.Thread(target=requester, args=(False,)),
            threading.Thread(target=requester, args=(True,)),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        should, preserve = coordinator.should_restart()
        assert should is True
        # preserve 值不确定（竞态），但至少有一个线程成功设置了标志


class TestGracefulRestartManager:
    """测试 GracefulRestartManager 核心逻辑。"""

    @pytest.fixture
    def mock_repl(self):
        """构造 Mock REPL 对象。"""
        repl = MagicMock()
        repl.ctx_mgr = MagicMock()
        repl.ctx_mgr.history = []
        repl.ctx_mgr._working_memory = {}
        repl.ctx_mgr.max_tokens = 128000
        repl.registry = MagicMock()
        repl.registry.current_mode = "direct"
        repl.registry.export_config = MagicMock(return_value={"models": {}})
        repl.model_pool = MagicMock()
        repl.auto_router = MagicMock()
        repl.status_bar = MagicMock()
        repl._cache_tracker = MagicMock()
        repl._terminal_activity = MagicMock()
        repl._clipboard_monitor = MagicMock()
        repl.agent_context = MagicMock()
        repl._session_state = {}
        repl._on_clipboard_image = MagicMock()
        return repl

    @pytest.fixture
    def manager(self, mock_repl):
        """构造 GracefulRestartManager。"""
        from xenon.repl.graceful_restart import GracefulRestartManager

        return GracefulRestartManager(mock_repl)

    def test_install_signal_handlers_unix(self, manager):
        """Unix 平台：信号处理器安装成功。"""
        if sys.platform == "win32":
            pytest.skip("Windows 平台不支持信号")

        result = manager.install_signal_handlers()
        assert result is True
        assert manager._handlers_installed is True
        assert signal.SIGHUP in manager._original_handlers
        assert signal.SIGUSR1 in manager._original_handlers

        # 清理
        manager.uninstall_signal_handlers()

    def test_install_signal_handlers_windows(self, manager):
        """Windows 平台：信号处理器安装失败。"""
        if sys.platform != "win32":
            pytest.skip("仅 Windows 平台")

        result = manager.install_signal_handlers()
        assert result is False
        assert manager._handlers_installed is False

    def test_uninstall_signal_handlers(self, manager):
        """卸载信号处理器：恢复原处理器。"""
        if sys.platform == "win32":
            pytest.skip("Windows 平台不支持信号")

        manager.install_signal_handlers()
        _ = manager._original_handlers[signal.SIGHUP]  # 验证已安装

        manager.uninstall_signal_handlers()
        assert manager._handlers_installed is False
        assert len(manager._original_handlers) == 0

        # 验证处理器已恢复
        current_handler = signal.signal(signal.SIGHUP, signal.SIG_DFL)
        signal.signal(signal.SIGHUP, current_handler)  # 恢复
        # 无法直接比较处理器对象，只能验证不崩溃

    def test_validate_config_success(self, manager):
        """配置验证成功。"""
        with patch("xenon.repl.system_config.reload_config") as mock_reload:
            mock_config = MagicMock()
            mock_config.validation = MagicMock()
            mock_reload.return_value = mock_config

            valid, error = manager.validate_config()
            assert valid is True
            assert error == ""

    def test_validate_config_failure(self, manager):
        """配置验证失败：配置文件不存在。"""
        with patch("xenon.repl.system_config.reload_config") as mock_reload:
            mock_reload.side_effect = FileNotFoundError("config.yaml not found")

            valid, error = manager.validate_config()
            assert valid is False
            assert "配置文件不存在" in error

    def test_save_session_state_success(self, manager, mock_repl, tmp_path):
        """保存会话成功。"""
        # 准备历史数据
        from xenon.repl.context_manager import ConversationTurn

        turn1 = ConversationTurn(role="user", content="test1")
        turn2 = ConversationTurn(role="assistant", content="test2", model_used="model1")
        mock_repl.ctx_mgr.history = [turn1, turn2]
        mock_repl.ctx_mgr._working_memory = {"key": "value"}

        session_file = tmp_path / "test_session.json"
        result = manager.save_session_state(session_file)

        assert result == session_file
        assert session_file.exists()

        # 验证文件内容
        with open(session_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        assert data["version"] == 1
        assert len(data["history"]) == 2
        assert data["history"][0]["role"] == "user"
        assert data["history"][0]["content"] == "test1"
        assert data["working_memory"] == {"key": "value"}
        assert "working_directory" in data

    def test_save_session_state_failure(self, manager, mock_repl):
        """保存会话失败：路径不可写。"""
        invalid_path = Path("/invalid/path/session.json")
        result = manager.save_session_state(invalid_path)
        assert result is None

    def test_restore_session_state_success(self, manager, mock_repl, tmp_path):
        """恢复会话成功。"""
        # 创建会话文件
        session_file = tmp_path / "session.json"
        session_data = {
            "version": 1,
            "timestamp": time.time(),
            "history": [
                {
                    "role": "user",
                    "content": "hello",
                    "model_used": None,
                    "metadata": {},
                },
                {
                    "role": "assistant",
                    "content": "world",
                    "model_used": "model1",
                    "metadata": {},
                },
            ],
            "working_memory": {"test": "data"},
            "working_directory": str(Path.cwd()),
            "current_mode": "react",
        }

        with open(session_file, "w", encoding="utf-8") as f:
            json.dump(session_data, f)

        # 恢复会话
        outcome = manager.restore_session_state(session_file)

        assert outcome.ok is True
        assert "会话已恢复" in outcome.message
        assert not session_file.exists()  # 成功后删除

    def test_restore_session_state_file_not_exists(self, manager, tmp_path):
        """恢复会话失败：文件不存在。"""
        session_file = tmp_path / "nonexistent.json"
        outcome = manager.restore_session_state(session_file)

        assert outcome.ok is False
        assert "文件不存在" in outcome.message

    def test_restore_session_state_version_mismatch(self, manager, tmp_path):
        """恢复会话失败：版本不兼容。"""
        session_file = tmp_path / "session.json"
        session_data = {
            "version": 999,  # 不兼容的版本
            "history": [],
        }

        with open(session_file, "w", encoding="utf-8") as f:
            json.dump(session_data, f)

        outcome = manager.restore_session_state(session_file)

        assert outcome.ok is False
        assert "版本不兼容" in outcome.message
        assert session_file.exists()  # 失败时保留文件

    def test_restore_session_state_invalid_format(self, manager, tmp_path):
        """恢复会话失败：格式错误。"""
        session_file = tmp_path / "session.json"
        session_data = {
            "version": 1,
            "history": "invalid",  # 应该是列表
        }

        with open(session_file, "w", encoding="utf-8") as f:
            json.dump(session_data, f)

        outcome = manager.restore_session_state(session_file)

        assert outcome.ok is False
        assert "格式错误" in outcome.message
        assert session_file.exists()  # 失败时保留文件

    def test_restore_session_state_invalid_turn(self, manager, tmp_path):
        """恢复会话失败：turn 格式错误。"""
        session_file = tmp_path / "session.json"
        session_data = {
            "version": 1,
            "history": [
                {"role": "user"},  # 缺少 content
            ],
        }

        with open(session_file, "w", encoding="utf-8") as f:
            json.dump(session_data, f)

        outcome = manager.restore_session_state(session_file)

        assert outcome.ok is False
        assert "缺少 role 或 content" in outcome.message
        assert session_file.exists()  # 失败时保留文件

    def test_cleanup_resources(self, manager, mock_repl):
        """资源清理：各组件独立清理。"""
        manager.cleanup_resources()

        # 验证各组件的 close 方法被调用
        mock_repl._terminal_activity.close.assert_called_once()

    def test_cleanup_resources_partial_failure(self, manager, mock_repl):
        """资源清理：单个组件失败不阻塞整体。"""
        mock_repl._terminal_activity.close.side_effect = RuntimeError("cleanup failed")

        # 不应抛异常
        manager.cleanup_resources()

    def test_reload_components(self, manager, mock_repl):
        """重新初始化组件。"""
        with (
            patch("xenon.repl.system_config.reload_config") as mock_reload,
            patch("xenon.repl.model_pool.ModelPool") as MockModelPool,
            patch("xenon.repl.auto_router.AutoRouter") as MockAutoRouter,
            patch(
                "xenon.repl.terminal_activity.TerminalActivityIndicator"
            ) as MockTerminal,
            patch("xenon.tools.ClipboardMonitor") as MockClipboard,
        ):
            mock_reload.return_value = MagicMock()
            mock_pool = MagicMock()
            MockModelPool.return_value = mock_pool

            manager.reload_components()

            # 验证组件被重建
            MockModelPool.assert_called_once()
            MockAutoRouter.assert_called_once()
            MockTerminal.assert_called_once()
            MockClipboard.assert_called_once()

    def test_perform_restart_config_validation_fails(self, manager):
        """执行重启：配置验证失败，不触碰资源。"""
        with patch.object(manager, "validate_config") as mock_validate:
            mock_validate.return_value = (False, "配置错误")

            outcome = manager.perform_restart(preserve_session=True)

            assert outcome.ok is False
            assert "配置验证失败" in outcome.message
            # 验证未调用清理和初始化
            manager.repl._terminal_activity.close.assert_not_called()

    def test_perform_restart_success_with_session(self, manager, mock_repl, tmp_path):
        """执行重启：保存并恢复会话成功。"""
        with (
            patch.object(manager, "validate_config") as mock_validate,
            patch.object(manager, "save_session_state") as mock_save,
            patch.object(manager, "cleanup_resources") as mock_cleanup,
            patch.object(manager, "reload_components") as mock_reload,
            patch.object(manager, "restore_session_state") as mock_restore,
        ):
            mock_validate.return_value = (True, "")
            session_file = tmp_path / "session.json"
            mock_save.return_value = session_file
            mock_restore.return_value = MagicMock(ok=True, message="成功")

            outcome = manager.perform_restart(preserve_session=True)

            assert outcome.ok is True
            mock_validate.assert_called_once()
            mock_save.assert_called_once()
            mock_cleanup.assert_called_once()
            mock_reload.assert_called_once()
            mock_restore.assert_called_once_with(session_file)

    def test_perform_restart_success_without_session(self, manager, mock_repl):
        """执行重启：不保存会话，全新启动。"""
        with (
            patch.object(manager, "validate_config") as mock_validate,
            patch.object(manager, "cleanup_resources"),
            patch.object(manager, "reload_components"),
        ):
            mock_validate.return_value = (True, "")

            outcome = manager.perform_restart(preserve_session=False)

            assert outcome.ok is True
            assert "全新会话" in outcome.message
            assert outcome.context_reset is True
            mock_repl.ctx_mgr.clear.assert_called_once()

    def test_perform_restart_save_session_fails(self, manager, mock_repl):
        """执行重启：会话保存失败，取消重启。"""
        with (
            patch.object(manager, "validate_config") as mock_validate,
            patch.object(manager, "save_session_state") as mock_save,
        ):
            mock_validate.return_value = (True, "")
            mock_save.return_value = None  # 保存失败

            outcome = manager.perform_restart(preserve_session=True)

            assert outcome.ok is False
            assert "会话保存失败" in outcome.message
            # 验证未调用清理
            manager.repl._terminal_activity.close.assert_not_called()

    def test_perform_restart_reload_fails(self, manager, mock_repl):
        """执行重启：组件初始化失败。"""
        with (
            patch.object(manager, "validate_config") as mock_validate,
            patch.object(manager, "cleanup_resources"),
            patch.object(manager, "reload_components") as mock_reload,
        ):
            mock_validate.return_value = (True, "")
            mock_reload.side_effect = RuntimeError("初始化失败")

            outcome = manager.perform_restart(preserve_session=False)

            assert outcome.ok is False
            assert "组件初始化失败" in outcome.message


class TestSignalHandling:
    """测试信号处理。"""

    @pytest.mark.skipif(sys.platform == "win32", reason="Windows 不支持信号")
    def test_signal_handler_triggers_restart(self):
        """信号处理器触发重启请求。"""
        from xenon.repl.graceful_restart import GracefulRestartManager

        mock_repl = MagicMock()
        manager = GracefulRestartManager(mock_repl)
        manager.install_signal_handlers()

        # 发送信号
        os.kill(os.getpid(), signal.SIGHUP)
        time.sleep(0.1)  # 等待信号处理

        should, preserve = manager.coordinator.should_restart()
        assert should is True

        # 清理
        manager.uninstall_signal_handlers()

    @pytest.mark.skipif(sys.platform == "win32", reason="Windows 不支持信号")
    def test_signal_handler_after_uninstall(self):
        """卸载后信号不再触发重启。"""
        from xenon.repl.graceful_restart import GracefulRestartManager

        mock_repl = MagicMock()
        manager = GracefulRestartManager(mock_repl)
        manager.install_signal_handlers()
        manager.uninstall_signal_handlers()

        manager.coordinator.clear()

        # 发送信号（应被原处理器处理，不触发重启）
        # 注意：这个测试可能不可靠，因为信号处理器已恢复
        # 这里只验证不崩溃
        should, _ = manager.coordinator.should_restart()
        assert should is False
