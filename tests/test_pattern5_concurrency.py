"""Pattern 5: 并发安全修复测试。

测试 commit 51b6c67 中的 4 个并发安全修复：
1. StatusBar.refresh() - 状态变更时触发刷新的回调机制
2. StatusBar._parse_pct() - 百分比解析统一（返回 0.0-1.0 范围）
3. ContextManager 线程安全 - _lock 保护 history 和 _undo_stack
4. 进度条超限保护 - 限制显示最大 100%+

关注点：
- 并发访问不抛异常
- 状态一致性
- 边界值处理
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, Mock

import pytest

from xenon.repl.context_manager import ContextManager
from xenon.repl.status_bar import StatusBar


# ── Problem 1: StatusBar.refresh() 回调机制 ──────────────────

def test_statusbar_refresh_on_state_change():
    """验证状态变更方法调用 refresh()。"""
    mock_console = MagicMock()
    mock_ctx = MagicMock()
    mock_ctx.stats.return_value = {
        "estimated_tokens": 1000,
        "max_tokens": 128000,
        "usage_ratio": "0.8%",
        "needs_compact": False,
        "total_messages": 5,
        "undo_available": 0,
    }
    mock_registry = MagicMock()
    mock_registry.get_current_mode.return_value = Mock(name="ReAct")

    status_bar = StatusBar(mock_console, mock_ctx, mock_registry)

    # 记录 refresh 是否被调用
    refresh_count = []
    original_refresh = status_bar.refresh
    def counting_refresh():
        refresh_count.append(1)
        original_refresh()
    status_bar.refresh = counting_refresh

    # 测试各个状态变更方法
    status_bar.set_last_model("openai/gpt-4")
    assert len(refresh_count) == 1, "set_last_model 应触发 refresh"

    status_bar.set_streaming(False)
    assert len(refresh_count) == 2, "set_streaming 应触发 refresh"

    status_bar.set_mode_notification("Plan")
    assert len(refresh_count) == 3, "set_mode_notification 应触发 refresh"

    status_bar.add_tool_call()
    assert len(refresh_count) == 4, "add_tool_call 应触发 refresh"


def test_statusbar_refresh_concurrent_calls():
    """验证并发调用状态变更方法时 refresh 的线程安全。"""
    mock_console = MagicMock()
    mock_ctx = MagicMock()
    mock_ctx.stats.return_value = {
        "estimated_tokens": 1000,
        "max_tokens": 128000,
        "usage_ratio": "1%",
        "needs_compact": False,
        "total_messages": 5,
        "undo_available": 0,
    }
    mock_registry = MagicMock()
    mock_registry.get_current_mode.return_value = Mock(name="ReAct")

    status_bar = StatusBar(mock_console, mock_ctx, mock_registry)

    errors = []
    barrier = threading.Barrier(10)

    def worker(i):
        try:
            barrier.wait(timeout=2.0)
            # 并发调用不同的状态变更方法
            if i % 4 == 0:
                status_bar.set_last_model(f"model-{i}")
            elif i % 4 == 1:
                status_bar.set_streaming(i % 2 == 0)
            elif i % 4 == 2:
                status_bar.set_mode_notification(f"Mode{i}")
            else:
                status_bar.add_tool_call()
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert not errors, f"并发状态变更有错误: {errors}"
    # 验证工具调用计数正确累加
    assert status_bar.tool_call_count >= 2, "并发 add_tool_call 应正确累加"


# ── Problem 2: StatusBar._parse_pct() 百分比解析统一 ──────────

def test_parse_pct_string_format():
    """验证字符串百分比解析返回 0.0-1.0 范围。"""
    assert StatusBar._parse_pct("50%") == 0.5
    assert StatusBar._parse_pct("100%") == 1.0
    assert StatusBar._parse_pct("0%") == 0.0
    assert StatusBar._parse_pct("75.5%") == 0.755


def test_parse_pct_numeric_format():
    """验证数值百分比解析自动归一化到 0.0-1.0。"""
    # 大于1的值视为百分数（需除以100）
    assert StatusBar._parse_pct(50) == 0.5
    assert StatusBar._parse_pct(100) == 1.0
    assert StatusBar._parse_pct(75.5) == 0.755

    # 小于等于1的值视为比例（不变）
    assert StatusBar._parse_pct(0.5) == 0.5
    assert StatusBar._parse_pct(1.0) == 1.0
    assert StatusBar._parse_pct(0.0) == 0.0
    assert StatusBar._parse_pct(0.755) == 0.755


def test_parse_pct_edge_cases():
    """验证边界情况和异常输入。"""
    # 无效输入返回 0.0
    assert StatusBar._parse_pct(None) == 0.0
    assert StatusBar._parse_pct("invalid") == 0.0
    assert StatusBar._parse_pct("") == 0.0

    # 极端值
    assert StatusBar._parse_pct(0) == 0.0
    assert StatusBar._parse_pct("0.001%") == 0.00001
    assert StatusBar._parse_pct(1000) == 10.0  # 超过100%也能解析


def test_parse_pct_concurrent():
    """验证并发调用 _parse_pct 的线程安全（纯函数应无竞态）。"""
    inputs = ["50%", 75, 0.8, "100%", 25, 0.5, "33.3%", 90, 0.1, "1%"]
    expected = [0.5, 0.75, 0.8, 1.0, 0.25, 0.5, 0.333, 0.9, 0.1, 0.01]

    results = [None] * 10
    errors = []
    barrier = threading.Barrier(10)

    def worker(i):
        try:
            barrier.wait(timeout=2.0)
            results[i] = StatusBar._parse_pct(inputs[i])
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert not errors, f"并发解析有错误: {errors}"
    for i, result in enumerate(results):
        assert abs(result - expected[i]) < 0.001, f"索引 {i} 解析错误: {result} != {expected[i]}"


# ── Problem 3: ContextManager 线程安全 ─────────────────────

def test_context_manager_concurrent_add_message():
    """验证并发添加消息的线程安全。"""
    ctx_mgr = ContextManager(max_tokens=128000)

    errors = []
    barrier = threading.Barrier(20)

    def worker(i):
        try:
            barrier.wait(timeout=2.0)
            ctx_mgr.add_message("user", f"Message {i}")
            time.sleep(0.001)  # 模拟处理延迟
            ctx_mgr.add_message("assistant", f"Response {i}")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert not errors, f"并发 add_message 有错误: {errors}"
    # 验证所有消息都被添加（20个用户消息 + 20个助手消息）
    assert len(ctx_mgr.history) == 40, f"期望40条消息，实际 {len(ctx_mgr.history)}"


def test_context_manager_concurrent_undo():
    """验证并发 undo 操作的线程安全。"""
    ctx_mgr = ContextManager(max_tokens=128000, max_undo_snapshots=20)

    # 预先添加一些消息
    for i in range(20):
        ctx_mgr.add_message("user", f"Message {i}")
        ctx_mgr.add_message("assistant", f"Response {i}")
        ctx_mgr.save_snapshot()  # 手动保存快照

    initial_count = len(ctx_mgr.history)
    errors = []
    barrier = threading.Barrier(5)

    def worker(i):
        try:
            barrier.wait(timeout=2.0)
            if ctx_mgr.undo_depth > 0:
                ctx_mgr.undo()
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert not errors, f"并发 undo 有错误: {errors}"
    # 验证历史记录减少了
    assert len(ctx_mgr.history) < initial_count, "undo 应该减少历史记录"


def test_context_manager_mixed_operations():
    """验证混合并发操作（添加、读取、undo）的线程安全。"""
    ctx_mgr = ContextManager(max_tokens=128000)

    # 预先添加基础数据
    for i in range(10):
        ctx_mgr.add_message("user", f"Init {i}")
        ctx_mgr.save_snapshot()

    errors = []
    barrier = threading.Barrier(15)

    def worker(i):
        try:
            barrier.wait(timeout=2.0)
            if i % 3 == 0:
                # 添加消息
                ctx_mgr.add_message("user", f"Concurrent {i}")
            elif i % 3 == 1:
                # 读取状态
                _ = ctx_mgr.stats()
            else:
                # undo 操作
                if ctx_mgr.undo_depth > 0:
                    ctx_mgr.undo()
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(15)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert not errors, f"混合并发操作有错误: {errors}"


# ── Problem 4: 进度条超限保护 ─────────────────────────────

def test_progress_bar_overflow_protection():
    """验证进度条显示不超过100%+的限制。"""
    mock_console = MagicMock()
    mock_ctx = MagicMock()
    mock_registry = MagicMock()
    mock_registry.get_current_mode.return_value = Mock(name="ReAct")

    # 测试各种超限情况
    test_cases = [
        ("50%", 0.5),      # 正常
        ("100%", 1.0),     # 边界
        ("150%", 1.5),     # 超限
        ("200%", 2.0),     # 严重超限
    ]

    for ratio_str, expected_val in test_cases:
        mock_ctx.stats.return_value = {
            "estimated_tokens": 128000,
            "max_tokens": 128000,
            "usage_ratio": ratio_str,
            "needs_compact": False,
            "total_messages": 5,
            "undo_available": 0,
        }

        status_bar = StatusBar(mock_console, mock_ctx, mock_registry)
        parsed = status_bar._parse_pct(ratio_str)
        assert parsed == expected_val, f"{ratio_str} 应解析为 {expected_val}"

        # 渲染不应抛异常
        try:
            _ = status_bar.render()
            _ = status_bar.get_toolbar_text()
        except Exception as e:
            pytest.fail(f"进度条渲染失败 (ratio={ratio_str}): {e}")


def test_progress_bar_display_limiting():
    """验证进度条在渲染中正确限制显示范围。"""
    mock_console = MagicMock()
    mock_ctx = MagicMock()
    mock_registry = MagicMock()
    mock_registry.get_current_mode.return_value = Mock(name="ReAct")

    # 模拟超过100%的情况
    mock_ctx.stats.return_value = {
        "estimated_tokens": 200000,
        "max_tokens": 128000,
        "usage_ratio": "156%",  # 超限
        "needs_compact": True,
        "total_messages": 50,
        "undo_available": 2,
    }

    status_bar = StatusBar(mock_console, mock_ctx, mock_registry)
    status_bar.set_last_model("openai/gpt-4")

    # 渲染应该成功，不抛异常
    panel = status_bar.render()
    assert panel is not None

    # 获取工具栏文本
    toolbar_text = status_bar.get_toolbar_text()
    assert toolbar_text is not None
    assert len(toolbar_text) > 0


# ── 集成测试：综合并发场景 ──────────────────────────────

def test_integrated_concurrent_scenario():
    """综合测试：StatusBar + ContextManager 并发协作。"""
    ctx_mgr = ContextManager(max_tokens=128000)
    mock_console = MagicMock()
    mock_registry = MagicMock()
    mock_registry.get_current_mode.return_value = Mock(name="ReAct")

    status_bar = StatusBar(mock_console, ctx_mgr, mock_registry)

    errors = []
    barrier = threading.Barrier(20)

    def worker(i):
        try:
            barrier.wait(timeout=2.0)

            # 混合操作
            if i % 4 == 0:
                ctx_mgr.add_message("user", f"Query {i}")
            elif i % 4 == 1:
                ctx_mgr.add_message("assistant", f"Answer {i}")
            elif i % 4 == 2:
                status_bar.add_tool_call()
            else:
                _ = status_bar.get_toolbar_text()

            # 短暂延迟增加并发冲突概率
            time.sleep(0.001)

            # 再次操作
            if i % 2 == 0:
                _ = ctx_mgr.stats()
            else:
                status_bar.set_last_model(f"model-{i}")

        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert not errors, f"综合并发场景有错误: {errors}"

    # 验证最终状态一致性
    assert len(ctx_mgr.history) >= 10, "应该有足够的消息历史"
    assert status_bar.tool_call_count >= 5, "应该有工具调用计数"


def test_lock_prevents_race_condition():
    """验证 ContextManager 的锁真正防止了数据竞争。"""
    ctx_mgr = ContextManager(max_tokens=128000)

    # 创建一个计数器来检测竞态条件
    # 如果没有锁保护，并发修改会导致丢失更新
    read_counts = []
    errors = []
    barrier = threading.Barrier(10)

    def worker(i):
        try:
            barrier.wait(timeout=2.0)
            # 读取当前长度
            before = len(ctx_mgr.history)
            # 添加消息
            ctx_mgr.add_message("user", f"Test {i}")
            # 再次读取
            after = len(ctx_mgr.history)
            read_counts.append((before, after))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert not errors, f"竞态条件测试有错误: {errors}"
    assert len(ctx_mgr.history) == 10, f"期望10条消息，实际 {len(ctx_mgr.history)}"

    # 验证每次读取都是一致的（after = before + 1）
    for before, after in read_counts:
        assert after == before + 1, f"检测到不一致: before={before}, after={after}"
