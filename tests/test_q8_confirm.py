"""P3-Q8 破坏性操作确认 + dispatch_command 兜底测试。"""

from __future__ import annotations

import pytest

from xenon.repl.commands import _confirm, dispatch_command, ExitSignal, _HANDLERS
from xenon.repl.context_manager import ContextManager
from xenon.repl.model_registry import ModelRegistry


# --------------------------- _confirm() ---------------------------

def test_confirm_auto_yes_when_env_set(monkeypatch):
    monkeypatch.setenv("XENON_ASSUME_YES", "1")
    assert _confirm("危险操作？", default=False) is True


def test_confirm_eof_returns_default(monkeypatch):
    """非交互环境 Confirm.ask 抛 EOFError → 保守取 default。"""
    monkeypatch.delenv("XENON_ASSUME_YES", raising=False)
    import rich.prompt as rp

    def boom(*a, **kw):
        raise EOFError
    monkeypatch.setattr(rp.Confirm, "ask", boom)
    assert _confirm("x", default=False) is False
    assert _confirm("x", default=True) is True


def test_confirm_calls_ask_when_no_env(monkeypatch):
    monkeypatch.delenv("XENON_ASSUME_YES", raising=False)
    import rich.prompt as rp

    calls = []

    def fake_ask(prompt, default=False):
        calls.append((prompt, default))
        return False
    monkeypatch.setattr(rp.Confirm, "ask", fake_ask)
    assert _confirm("真的吗？", default=False) is False
    assert len(calls) == 1
    assert calls[0] == ("真的吗？", False)


# --------------------------- dispatch_command 兜底 ---------------------------

def test_dispatch_catches_handler_exception():
    reg = ModelRegistry()
    ctx_mgr = ContextManager()

    def boom(**kwargs):
        raise RuntimeError("handler 炸了")

    _HANDLERS["/boom"] = boom
    try:
        result = dispatch_command("/boom", "", registry=reg, ctx_mgr=ctx_mgr, session_state={})
        assert "❌" in result
        assert "命令执行失败" in result
        assert "/boom" in result
    finally:
        _HANDLERS.pop("/boom", None)


def test_dispatch_lets_exit_signal_propagate():
    reg = ModelRegistry()
    ctx_mgr = ContextManager()

    def raise_exit(**kwargs):
        raise ExitSignal()

    _HANDLERS["/exitnow"] = raise_exit
    try:
        with pytest.raises(ExitSignal):
            dispatch_command("/exitnow", "", registry=reg, ctx_mgr=ctx_mgr, session_state={})
    finally:
        _HANDLERS.pop("/exitnow", None)


def test_dispatch_unknown_command():
    reg = ModelRegistry()
    ctx_mgr = ContextManager()
    result = dispatch_command("/nope", "", registry=reg, ctx_mgr=ctx_mgr, session_state={})
    assert "未知命令" in result


# --------------------------- /clear 确认 ---------------------------

def test_clear_confirmed_clears_history():
    """autouse fixture 设了 XENON_ASSUME_YES=1 → /clear 直接清空。"""
    reg = ModelRegistry()
    ctx_mgr = ContextManager()
    ctx_mgr.add_user_message("test")
    result = dispatch_command("/clear", "", registry=reg, ctx_mgr=ctx_mgr, session_state={})
    assert "✅" in result
    assert len(ctx_mgr.history) == 0


def test_clear_cancelled_keeps_history(monkeypatch):
    """用户选'否' → 取消，历史保留。"""
    monkeypatch.delenv("XENON_ASSUME_YES", raising=False)
    import xenon.repl.commands as cmds
    monkeypatch.setattr(cmds, "_confirm", lambda *a, **kw: False)

    reg = ModelRegistry()
    ctx_mgr = ContextManager()
    ctx_mgr.add_user_message("keep me")
    result = dispatch_command("/clear", "", registry=reg, ctx_mgr=ctx_mgr, session_state={})
    assert "已取消" in result
    assert len(ctx_mgr.history) == 1  # 未清空


# --------------------------- /shortcut run 未找到 ---------------------------

def test_shortcut_run_not_found():
    reg = ModelRegistry()
    ctx_mgr = ContextManager()
    result = dispatch_command(
        "/shortcut", "run nonexistent", registry=reg, ctx_mgr=ctx_mgr, session_state={}
    )
    assert "未找到" in result


def test_shortcut_run_no_args():
    reg = ModelRegistry()
    ctx_mgr = ContextManager()
    result = dispatch_command(
        "/shortcut", "run", registry=reg, ctx_mgr=ctx_mgr, session_state={}
    )
    assert "用法" in result
