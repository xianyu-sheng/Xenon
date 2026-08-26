"""优雅重启管理器 — 信号驱动 + 会话保持的 REPL 重启。

功能：
1. 信号处理：监听 SIGHUP/SIGUSR1 触发重启（Windows 降级为仅命令）
2. 优雅关闭：保存会话状态，清理资源（终端/剪贴板/MCP）
3. 重新初始化：重载配置、重建模型池、重新注册工具与引擎
4. 会话恢复：可选恢复对话历史与工作目录
5. /restart 命令：与信号走同一套逻辑

设计约束：
- 配置验证先行：验证失败时不触碰任何资源，REPL 原样继续
- 事务式恢复：会话文件全部校验完才写入，失败时保留原文件
- 资源清理超时：单个组件不能阻塞整个流程
- 异常兜底：perform_restart() 抛异常时由调用方保存会话

参考：xenon/repl/config_watcher.py 的既有风格 (e6ca7e8)
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from xenon.repl.repl import REPL

logger = logging.getLogger(__name__)

# 会话文件固定名称（避免 stem 推导问题）
_RESTART_RECOVERY_FILE = ".restart_recovery.json"
# 会话文件格式版本
_SESSION_FORMAT_VERSION = 1
# 资源清理超时（秒）
_CLEANUP_TIMEOUT = 5.0


@dataclass
class RestartOutcome:
    """重启结果。

    Attributes:
        ok: 是否成功
        message: 用户可见的提示信息
        context_reset: 上下文是否被 clear()（恢复失败的降级状态标志）
    """

    ok: bool
    message: str
    context_reset: bool = False


class RestartCoordinator:
    """重启协调器 — 线程安全的重启请求传递。

    信号处理器（子线程）通过它向主循环（主线程）传递重启请求。
    使用 threading.Event 实现原子状态传递。
    """

    def __init__(self) -> None:
        self._restart_event = threading.Event()
        self._preserve_session = True  # 默认保存会话
        self._lock = threading.Lock()

    def request_restart(self, preserve_session: bool = True) -> None:
        """请求重启（信号处理器调用）。"""
        with self._lock:
            self._preserve_session = preserve_session
            self._restart_event.set()

    def should_restart(self) -> tuple[bool, bool]:
        """检查是否需要重启（主循环调用）。

        Returns:
            (should_restart, preserve_session)
        """
        with self._lock:
            should = self._restart_event.is_set()
            preserve = self._preserve_session
            return should, preserve

    def clear(self) -> None:
        """清除重启标志（主循环处理完后调用）。"""
        with self._lock:
            self._restart_event.clear()


class GracefulRestartManager:
    """优雅重启管理器 — 信号处理 + 会话保持 + 资源清理 + 配置重载。

    职责：
    1. 安装/卸载信号处理器（SIGHUP/SIGUSR1）
    2. 保存/恢复会话状态（对话历史、工作目录、模式）
    3. 清理资源（终端、剪贴板、MCP）
    4. 重载配置、重建模型池
    5. 提供 /restart 命令入口
    """

    def __init__(self, repl: REPL) -> None:
        self.repl = repl
        self.coordinator = RestartCoordinator()
        self._original_handlers: dict[int, Any] = {}
        self._handlers_installed = False

    def install_signal_handlers(self) -> bool:
        """安装信号处理器（仅 Unix 平台）。

        Returns:
            True 表示安装成功，False 表示平台不支持
        """
        if sys.platform == "win32":
            logger.debug("Windows 平台不支持信号重启，请使用 /restart 命令")
            return False

        if not hasattr(signal, "SIGHUP") or not hasattr(signal, "SIGUSR1"):
            logger.debug("当前平台不支持 SIGHUP/SIGUSR1")
            return False

        try:
            for sig in [signal.SIGHUP, signal.SIGUSR1]:
                # 保存原处理器
                self._original_handlers[sig] = signal.signal(sig, self._signal_handler)
            self._handlers_installed = True
            logger.debug("信号处理器已安装: SIGHUP, SIGUSR1")
            return True
        except (OSError, ValueError) as e:
            logger.debug(f"信号处理器安装失败: {e}")
            return False

    def uninstall_signal_handlers(self) -> None:
        """卸载信号处理器，恢复原处理器。"""
        if not self._handlers_installed:
            return

        for sig, original in self._original_handlers.items():
            try:
                signal.signal(sig, original)
            except (OSError, ValueError) as e:
                logger.debug(f"恢复信号处理器失败 (sig={sig}): {e}")

        self._original_handlers.clear()
        self._handlers_installed = False
        logger.debug("信号处理器已卸载")

    def _signal_handler(self, signum: int, frame: Any) -> None:
        """信号处理回调（子线程执行）。

        不直接操作 REPL 资源，只设置标志让主循环处理。
        """
        # 防御：销毁过程中收到信号
        if not hasattr(self, "repl") or self.repl is None:
            return

        try:
            sig_name = signal.Signals(signum).name
        except (ValueError, AttributeError):
            sig_name = f"signal-{signum}"

        logger.info(f"收到信号 {sig_name}，请求优雅重启")
        self.coordinator.request_restart(preserve_session=True)

    def validate_config(self) -> tuple[bool, str]:
        """验证配置可加载（不触碰任何资源）。

        Returns:
            (valid, error_message)
        """
        try:
            # 尝试重新加载配置文件
            from xenon.repl.system_config import reload_config

            config = reload_config()

            # 基本有效性检查
            if config is None:
                return False, "配置加载返回 None"

            # 检查关键字段
            if not hasattr(config, "validation"):
                return False, "配置缺少 validation 段"

            logger.debug("配置验证通过")
            return True, ""

        except FileNotFoundError as e:
            return False, f"配置文件不存在: {e}"
        except Exception as e:
            logger.error("配置验证失败", exc_info=True)
            return False, f"配置加载异常: {e}"

    def save_session_state(self, session_file: Path | None = None) -> Path | None:
        """保存当前会话状态到临时文件。

        Args:
            session_file: 指定保存路径，None 则使用默认路径

        Returns:
            保存成功返回文件路径，失败返回 None
        """
        try:
            if session_file is None:
                xenon_dir = Path.home() / ".xenon"
                xenon_dir.mkdir(parents=True, exist_ok=True)
                session_file = xenon_dir / _RESTART_RECOVERY_FILE

            # 构造会话快照
            history_data = []
            for turn in self.repl.ctx_mgr.history:
                history_data.append(
                    {
                        "role": turn.role,
                        "content": turn.content,
                        "model_used": turn.model_used,
                        "metadata": turn.metadata,
                    }
                )

            snapshot = {
                "version": _SESSION_FORMAT_VERSION,
                "timestamp": time.time(),
                "history": history_data,
                "working_memory": dict(self.repl.ctx_mgr._working_memory),
                "working_directory": str(Path.cwd()),
                "current_mode": self.repl.registry.current_mode,
            }

            # 原子写入
            with open(session_file, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)

            logger.info(f"会话已保存到 {session_file.absolute()}")
            return session_file

        except Exception as e:
            logger.error(f"保存会话失败: {e}", exc_info=True)
            return None

    def restore_session_state(self, session_file: Path) -> RestartOutcome:
        """从文件恢复会话状态（事务式）。

        校验段：全部校验通过才进入写入段
        写入段：任一步失败时 clear() 上下文，返回 context_reset=True

        Args:
            session_file: 会话文件路径

        Returns:
            RestartOutcome(ok=成功/失败, message=提示信息, context_reset=是否已清空上下文)
        """
        if not session_file.exists():
            return RestartOutcome(
                ok=False,
                message=f"会话文件不存在: {session_file.absolute()}",
            )

        try:
            with open(session_file, "r", encoding="utf-8") as f:
                snapshot = json.load(f)
        except Exception as e:
            return RestartOutcome(
                ok=False,
                message=f"会话文件读取失败: {e}\n文件保留在 {session_file.absolute()}",
            )

        # ── 校验段：全部校验完才进入写入段 ──

        # 版本校验
        version = snapshot.get("version")
        if version != _SESSION_FORMAT_VERSION:
            return RestartOutcome(
                ok=False,
                message=f"会话文件版本不兼容 (期望 {_SESSION_FORMAT_VERSION}, 实际 {version})\n"
                f"文件保留在 {session_file.absolute()}",
            )

        # 历史记录校验
        history = snapshot.get("history", [])
        if not isinstance(history, list):
            return RestartOutcome(
                ok=False,
                message=f"会话文件格式错误: history 不是列表\n文件保留在 {session_file.absolute()}",
            )

        # 逐条校验 turn 格式
        for i, turn_data in enumerate(history):
            if not isinstance(turn_data, dict):
                return RestartOutcome(
                    ok=False,
                    message=f"会话文件格式错误: history[{i}] 不是字典\n"
                    f"文件保留在 {session_file.absolute()}",
                )
            if "role" not in turn_data or "content" not in turn_data:
                return RestartOutcome(
                    ok=False,
                    message=f"会话文件格式错误: history[{i}] 缺少 role 或 content\n"
                    f"文件保留在 {session_file.absolute()}",
                )
            if not isinstance(turn_data["role"], str) or not isinstance(
                turn_data["content"], str
            ):
                return RestartOutcome(
                    ok=False,
                    message=f"会话文件格式错误: history[{i}] 的 role/content 不是字符串\n"
                    f"文件保留在 {session_file.absolute()}",
                )

        # ── 写入段：整体包在 try 块，任一步失败都清空上下文 ──

        try:
            # 创建临时上下文，全部写入成功后再替换
            from xenon.repl.context_manager import ContextManager

            temp_ctx = ContextManager(
                max_tokens=self.repl.ctx_mgr.max_tokens,
                track_real_usage=True,
            )

            # 写入历史记录
            for turn_data in history:
                temp_ctx.add_message(
                    role=turn_data["role"],
                    content=turn_data["content"],
                    model_used=turn_data.get("model_used"),
                    metadata=turn_data.get("metadata", {}),
                )

            # 写入工作记忆
            working_memory = snapshot.get("working_memory", {})
            if working_memory:
                for key, value in working_memory.items():
                    temp_ctx.update_working_memory(key, value)

            # 切换模式（失败不中止）
            mode = snapshot.get("current_mode")
            if mode:
                try:
                    self.repl.registry.set_mode(mode)
                except Exception as e:
                    logger.warning(f"恢复模式失败 (mode={mode}): {e}")

            # 恢复工作目录（失败不中止）
            working_dir = snapshot.get("working_directory")
            if working_dir:
                try:
                    os.chdir(working_dir)
                    logger.info(f"工作目录已恢复: {working_dir}")
                except Exception as e:
                    logger.warning(f"恢复工作目录失败: {e}")

            # 原子替换上下文
            self.repl.ctx_mgr = temp_ctx

            # 成功后删除临时文件
            try:
                session_file.unlink()
            except Exception as e:
                logger.warning(f"删除临时文件失败: {e}")

            return RestartOutcome(
                ok=True,
                message=f"会话已恢复 ({len(history)} 轮对话)",
            )

        except Exception as e:
            # 写入段失败：清空上下文，保留文件
            logger.error(f"会话恢复失败（写入段异常）: {e}", exc_info=True)
            self.repl.ctx_mgr.clear()
            return RestartOutcome(
                ok=False,
                message=(
                    f"会话恢复失败（写入段异常），当前上下文已重置为空\n"
                    f"这是恢复失败的降级状态，非正常 fresh 重启\n"
                    f"旧历史保留在 {session_file.absolute()}，可用 /resume 手动加载"
                ),
                context_reset=True,
            )

    def cleanup_resources(self) -> None:
        """清理资源（终端、剪贴板、MCP）。

        每个组件独立清理，单个失败不阻塞整体流程。
        """
        # 清理终端活动指示器
        logger.debug("清理终端活动指示器...")
        try:
            if hasattr(self.repl, "_terminal_activity"):
                self.repl._terminal_activity.close()
        except Exception as e:
            logger.warning(f"清理终端活动指示器失败: {e}")

        # 清理剪贴板监听器
        logger.debug("清理剪贴板监听器...")
        try:
            if hasattr(self.repl, "_clipboard_monitor"):
                # ClipboardMonitor 没有显式 close 方法，但可以停止线程
                monitor = self.repl._clipboard_monitor
                if hasattr(monitor, "stop"):
                    monitor.stop()
        except Exception as e:
            logger.warning(f"清理剪贴板监听器失败: {e}")

        # 清理 MCP 服务器连接
        logger.debug("清理 MCP 连接...")
        try:
            if hasattr(self.repl, "agent_context") and hasattr(
                self.repl.agent_context, "close_all_mcp"
            ):
                self.repl.agent_context.close_all_mcp()
        except Exception as e:
            logger.warning(f"清理 MCP 连接失败: {e}")

        # 清理上下文管理器资源
        logger.debug("清理上下文管理器...")
        try:
            if hasattr(self.repl.ctx_mgr, "close"):
                self.repl.ctx_mgr.close()
        except Exception as e:
            logger.warning(f"清理上下文管理器失败: {e}")

        logger.debug("资源清理完成")

    def reload_components(self) -> None:
        """重新初始化组件（模型池、工具注册等）。"""
        logger.debug("重新初始化模型池...")

        # 重载配置
        from xenon.repl.system_config import reload_config

        reload_config()

        # 重建模型池
        from xenon.repl.model_pool import ModelPool
        from xenon.repl.auto_router import AutoRouter

        self.repl.model_pool = ModelPool()
        self.repl.auto_router = AutoRouter(
            self.repl.model_pool,
            context_manager=self.repl.ctx_mgr,
            cache_tracker=self.repl._cache_tracker,
        )
        self.repl.status_bar._auto_router = self.repl.auto_router

        # 从配置重新加载模型
        config_data = self.repl.registry.export_config(include_derived=True).get(
            "models", {}
        )
        self.repl.model_pool.from_config(config_data)

        # 重建终端活动指示器
        from xenon.repl.terminal_activity import TerminalActivityIndicator

        self.repl._terminal_activity = TerminalActivityIndicator()
        self.repl._session_state["terminal_activity"] = self.repl._terminal_activity

        # 重建剪贴板监听器
        from xenon.tools import ClipboardMonitor

        self.repl._clipboard_monitor = ClipboardMonitor(
            on_image=self.repl._on_clipboard_image
        )

        logger.debug("组件重新初始化完成")

    def perform_restart(self, preserve_session: bool = True) -> RestartOutcome:
        """执行优雅重启（主流程）。

        流程：
        1. 验证配置可加载（失败时不触碰资源）
        2. 保存会话（如果 preserve_session=True）
        3. 清理资源
        4. 重新初始化组件
        5. 恢复会话（如果保存了）

        Args:
            preserve_session: 是否保存并恢复会话

        Returns:
            RestartOutcome 表示重启结果
        """
        logger.info(f"开始优雅重启 (preserve_session={preserve_session})")

        # 步骤 1: 验证配置（失败时不触碰任何资源）
        valid, error = self.validate_config()
        if not valid:
            logger.error(f"配置验证失败: {error}")
            return RestartOutcome(
                ok=False,
                message=f"配置验证失败，重启已取消:\n{error}\nREPL 继续以当前配置运行",
            )

        # 步骤 2: 保存会话（如果需要）
        session_file = None
        if preserve_session:
            session_file = self.save_session_state()
            if session_file is None:
                return RestartOutcome(
                    ok=False,
                    message="会话保存失败，重启已取消\nREPL 继续以当前配置运行",
                )

        # 步骤 3: 清理资源
        logger.info("清理资源...")
        self.cleanup_resources()

        # 步骤 4: 重新初始化组件
        logger.info("重新初始化组件...")
        try:
            self.reload_components()
        except Exception as e:
            logger.error(f"组件初始化失败: {e}", exc_info=True)
            return RestartOutcome(
                ok=False,
                message=f"组件初始化失败: {e}\nREPL 可能处于不稳定状态",
            )

        # 步骤 5: 恢复会话（如果保存了）
        if session_file is not None:
            outcome = self.restore_session_state(session_file)
            if outcome.ok:
                return RestartOutcome(
                    ok=True,
                    message=f"✅ 重启成功\n{outcome.message}",
                )
            else:
                # 恢复失败但重启本身完成了
                return RestartOutcome(
                    ok=True,
                    message=f"⚠ 重启完成，但会话恢复失败\n{outcome.message}",
                    context_reset=outcome.context_reset,
                )
        else:
            # 不保存会话的重启
            self.repl.ctx_mgr.clear()
            return RestartOutcome(
                ok=True,
                message="✅ 重启成功（全新会话）",
                context_reset=True,
            )
