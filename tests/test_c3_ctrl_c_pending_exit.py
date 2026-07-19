"""
C-3 修复测试：空行 Ctrl+C 应回到 prompt 而非退出（bash 风格）。

v0.3.0 修复前：5/9 终端类型（xterm256color/alacritty/gnome-256color/
screen-256color/vt100）空行 Ctrl+C 直接退出 REPL。

v0.3.0 修复后：
- REPL.__init__ 初始化 _pending_exit=False
- main loop 第一次 KeyboardInterrupt 重画 prompt，第二次才退出
- 成功读到 input 后重置 _pending_exit
"""

from __future__ import annotations

import pytest

from xenon.repl.repl import REPL
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.context_manager import ContextManager


class TestCtrlCPendingExit:
    """C-3 修复：bash 风格 Ctrl+C 二次确认退出。"""

    def test_pending_exit_field_initialized(self):
        """REPL.__init__ 初始化 _pending_exit=False。"""
        reg = ModelRegistry()
        ctx = ContextManager(track_real_usage=False)
        repl = REPL(registry=reg, ctx_mgr=ctx, streaming=False)
        assert repl._pending_exit is False

    def test_pending_exit_independent_across_instances(self):
        """每个 REPL 实例的 _pending_exit 独立。"""
        reg = ModelRegistry()
        ctx = ContextManager(track_real_usage=False)
        r1 = REPL(registry=reg, ctx_mgr=ctx, streaming=False)
        r2 = REPL(registry=reg, ctx_mgr=ctx, streaming=False)
        r1._pending_exit = True
        assert r2._pending_exit is False
