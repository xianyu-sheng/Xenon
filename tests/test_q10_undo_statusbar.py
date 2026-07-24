"""P3-Q10 undo_stack 上限 + status_bar try/except 兜底测试。"""

from __future__ import annotations

import io

from rich.console import Console

from xenon.repl.context_manager import ContextManager
from xenon.repl.status_bar import StatusBar


# --------------------------- _undo_stack 上限 ---------------------------

def test_undo_stack_default_limit():
    cm = ContextManager()
    assert cm.max_undo_snapshots == 5


def test_undo_stack_caps_at_limit():
    cm = ContextManager()
    cm.max_undo_snapshots = 3
    for i in range(10):
        cm.add_user_message(f"msg{i}")
        cm.save_snapshot()
    assert cm.undo_depth == 3  # 不超上限


def test_undo_stack_drops_oldest():
    cm = ContextManager()
    cm.max_undo_snapshots = 2
    for i in range(5):
        cm.add_user_message(f"m{i}")
        cm.save_snapshot()
    # cap=2 → 只保留最近 2 个快照，最早的 [m0]/[m0,m1]/[m0..m2] 被丢弃
    assert cm.undo_depth == 2
    # undo 弹出最近快照（m0..m4），再弹次新（m0..m3），第三次栈空
    assert cm.undo() is True
    assert cm.history[-1].content == "m4"
    assert cm.undo() is True
    assert cm.history[-1].content == "m3"
    assert cm.undo() is False  # 栈空——更早快照已丢弃
    assert len(cm.history) == 4  # 只能回到 m0..m3，回不到 m0..m2


def test_undo_stack_no_limit_when_zero():
    # max_undo_snapshots=0 → 立即丢弃（极端配置，但不应崩）
    cm = ContextManager()
    cm.max_undo_snapshots = 0
    cm.add_user_message("x")
    cm.save_snapshot()
    assert cm.undo_depth == 0


def test_undo_stack_large_limit_keeps_all():
    cm = ContextManager()
    cm.max_undo_snapshots = 100
    for i in range(5):
        cm.add_user_message(f"m{i}")
        cm.save_snapshot()
    assert cm.undo_depth == 5


# --------------------------- status_bar try/except 兜底 ---------------------------

def _make_bar(ctx_mgr=None, registry=None):
    from xenon.repl.model_registry import ModelRegistry
    console = Console(file=io.StringIO(), width=120, force_terminal=False)
    cm = ctx_mgr or ContextManager()
    reg = registry or ModelRegistry()
    return StatusBar(console, cm, reg)


def test_render_normal_returns_panel():
    bar = _make_bar()
    panel = bar.render()
    assert panel is not None  # 不崩即过


def test_render_stats_exception_returns_fallback():
    """stats() 抛异常 → render 不崩，返回"状态不可用"降级面板。"""
    class BoomCtx:
        def stats(self):
            raise RuntimeError("stats 炸了")
        def __getattr__(self, name):
            raise AttributeError(name)
    bar = _make_bar()
    bar.ctx_mgr = BoomCtx()
    panel = bar.render()  # 不应抛
    # 降级面板内容为"状态不可用"（直接读 renderable，绕开 Panel 终端渲染）
    assert "状态不可用" in str(panel.renderable)


def test_render_missing_stats_field_returns_fallback():
    """stats 缺字段（KeyError）→ 降级。"""
    class HalfCtx:
        def stats(self):
            return {"estimated_tokens": 0}  # 缺 max_tokens 等
        def __getattr__(self, name):
            raise AttributeError(name)
    bar = _make_bar()
    bar.ctx_mgr = HalfCtx()
    panel = bar.render()
    assert "状态不可用" in str(panel.renderable)


def test_print_status_stats_exception_does_not_raise():
    class BoomCtx:
        def stats(self):
            raise RuntimeError("boom")
        def __getattr__(self, name):
            raise AttributeError(name)
    bar = _make_bar()
    bar.ctx_mgr = BoomCtx()
    bar.print_status()  # 不应抛


# --------------------------- ⚠需压缩 警告置首（窄屏可见） ---------------------------

def test_render_needs_compact_warning_first():
    """needs_compact 时 ⚠需压缩 在状态行首位，窄屏截断不丢核心信号。"""
    cm = ContextManager()
    cm.add_user_message("x" * 200000)  # 触发 needs_compact（~78%）
    bar = _make_bar(ctx_mgr=cm)
    panel = bar.render()
    content = str(panel.renderable)
    assert "⚠需压缩" in content
    # 警告出现在 "模型:" 之前（首位，窄屏截断只丢末尾）
    assert content.index("⚠需压缩") < content.index("模型:")


def test_render_no_warning_when_not_needed():
    cm = ContextManager()
    cm.add_user_message("hi")
    bar = _make_bar(ctx_mgr=cm)
    panel = bar.render()
    assert "⚠需压缩" not in str(panel.renderable)


# --------------------------- _parse_pct ---------------------------

def test_parse_pct_string_percent():
    assert StatusBar._parse_pct("85.0%") == 85.0


def test_parse_pct_float_ratio():
    assert StatusBar._parse_pct(0.85) == 85.0


def test_parse_pct_invalid_returns_zero():
    assert StatusBar._parse_pct("abc") == 0.0
    assert StatusBar._parse_pct(None) == 0.0
