"""P3优化功能测试：百分比解析、/compact持久化反馈、max_undo_snapshots配置。

当前状态：
1. _parse_pct("0.5%") 已实现，返回0.5（表示0.5%）✓
2. /compact持久化反馈：_last_snapshot_path/_last_snapshot_error 未实现 ✗
3. max_undo_snapshots 默认值为5，不是10；环境变量配置未实现 ✗
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from xenon.repl.context_manager import ContextManager
from xenon.repl.status_bar import StatusBar


def test_parse_pct_string_with_percent_sign():
    """测试_parse_pct("0.5%")返回0.005（即0.5%的比例值）。

    P3-Low 2.8 修复：统一返回0.0-1.0范围的比例值。
    "0.5%" → 0.005（0.5% = 0.5/100 = 0.005）
    "50%" → 0.5（50% = 50/100 = 0.5）

    状态：✓ 已实现
    """
    result = StatusBar._parse_pct("0.5%")
    assert result == 0.005, f"Expected 0.005, got {result}"

    result = StatusBar._parse_pct("50%")
    assert result == 0.5, f"Expected 0.5, got {result}"


def test_compact_persistence_failure_feedback():
    """测试/compact持久化失败时用户收到提示。

    P3-Low 2.9：当压缩快照持久化失败时，_last_snapshot_error应被设置，
    以便命令处理器向用户显示错误信息。

    状态：✓ 已实现（持久化失败时设置_last_snapshot_error）
    """
    cm = ContextManager()
    cm.persist_dir = Path("/proc/invalid_path_that_cannot_be_written")
    cm.session_id = "test_session"

    for i in range(10):
        cm.add_user_message(f"message {i}")
        cm.add_assistant_message(f"response {i}")

    _ = cm.compact(summary="Test summary")

    # P3-Low 问题2修复：持久化失败时_last_snapshot_error应被设置
    assert hasattr(cm, "_last_snapshot_error"), "应该有_last_snapshot_error属性"
    assert cm._last_snapshot_error is not None, "持久化失败时应设置错误信息"
    assert (
        "No such file or directory" in cm._last_snapshot_error
        or "Permission denied" in cm._last_snapshot_error
    )


def test_max_undo_snapshots_configurable():
    """测试max_undo_snapshots可配置。

    P3-Low 2.10：验证max_undo_snapshots支持配置，并且默认值为10。

    状态：✓ 已实现（可配置，默认值为10）
    """
    # P3-Low 问题3修复：默认值已从5提升到10
    cm = ContextManager()
    assert cm.max_undo_snapshots == 10, "默认值应为10"

    # 可以配置
    cm.max_undo_snapshots = 15
    assert cm.max_undo_snapshots == 15, "应该能够设置自定义值"

    # 测试配置生效：验证快照栈上限
    cm.max_undo_snapshots = 3
    for i in range(10):
        cm.add_user_message(f"message {i}")
        cm.save_snapshot()

    assert cm.undo_depth == 3, f"应该只保留3个快照，实际保留了{cm.undo_depth}个"


def test_max_undo_snapshots_env_override():
    """测试通过环境变量XENON_MAX_UNDO_SNAPSHOTS覆盖max_undo_snapshots。

    P3-Low 2.10：支持通过环境变量配置undo栈深度。

    状态：✓ 已实现（环境变量XENON_MAX_UNDO_SNAPSHOTS配置功能已存在）
    """
    import os

    old_val = os.environ.get("XENON_MAX_UNDO_SNAPSHOTS")
    try:
        os.environ["XENON_MAX_UNDO_SNAPSHOTS"] = "20"
        # P3-Low 问题3修复：需要reload配置以应用环境变量
        from xenon.repl.system_config import reload_config

        reload_config()

        cm = ContextManager()
        assert cm.max_undo_snapshots == 20, (
            f"环境变量应生效，期望20，实际{cm.max_undo_snapshots}"
        )
    finally:
        if old_val is None:
            os.environ.pop("XENON_MAX_UNDO_SNAPSHOTS", None)
        else:
            os.environ["XENON_MAX_UNDO_SNAPSHOTS"] = old_val
        reload_config()


def test_compact_persistence_success_feedback():
    """测试/compact持久化成功时设置_last_snapshot_path。

    P3-Low 2.9：压缩成功后，_last_snapshot_path应被设置为快照文件路径。

    状态：✓ 已实现（持久化成功时设置_last_snapshot_path）
    """
    cm = ContextManager()

    with tempfile.TemporaryDirectory() as tmpdir:
        cm.persist_dir = Path(tmpdir)
        cm.session_id = "test_session"

        for i in range(10):
            cm.add_user_message(f"message {i}")
            cm.add_assistant_message(f"response {i}")

        _ = cm.compact(summary="Test summary")

        # P3-Low 问题2修复：持久化成功时_last_snapshot_path应被设置
        assert hasattr(cm, "_last_snapshot_path"), "应该有_last_snapshot_path属性"
        assert cm._last_snapshot_path is not None, "持久化成功时应设置快照路径"
        assert Path(cm._last_snapshot_path).exists(), "快照文件应该存在"
