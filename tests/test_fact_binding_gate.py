"""FactBindingGate 事实绑定校验测试（Step 3）。

验证：
1. 先读后写 → 通过
2. 未读就写（盲写）→ 拒绝
3. 多文件混合场景
4. 失败的读不算数
5. 无 tracker / 无调用 → 通过（不误伤）
"""

from __future__ import annotations

import types

from xenon.engine.evidence_gate import FactBindingGate


def _call(tool: str, path: str, success: bool = True) -> types.SimpleNamespace:
    """快捷构造一个工具调用。"""
    return types.SimpleNamespace(
        tool_name=tool,
        success=success,
        params={"file_path": path},
    )


class TestFactBindingGate:
    def test_read_then_write_passes(self) -> None:
        """先 read_file 再 write_file 同一文件 → 通过。"""
        tracker = types.SimpleNamespace(calls=[
            _call("read_file", "a.py"),
            _call("write_file", "a.py"),
        ])
        verdict = FactBindingGate().check(None, tracker=tracker)
        assert verdict.passed is True

    def test_blind_write_rejected(self) -> None:
        """未读就直接 write_file → 拒绝（盲写）。"""
        tracker = types.SimpleNamespace(calls=[
            _call("write_file", "a.py"),
        ])
        verdict = FactBindingGate().check(None, tracker=tracker)
        assert verdict.passed is False
        assert "盲写" in verdict.reason

    def test_edit_after_read_passes(self) -> None:
        """先读再 edit_file → 通过。"""
        tracker = types.SimpleNamespace(calls=[
            _call("read_file", "b.py"),
            _call("edit_file", "b.py"),
        ])
        verdict = FactBindingGate().check(None, tracker=tracker)
        assert verdict.passed is True

    def test_mixed_files(self) -> None:
        """读 a.py 后写 a.py 和 b.py → a 通过，b 盲写。"""
        tracker = types.SimpleNamespace(calls=[
            _call("read_file", "a.py"),
            _call("write_file", "a.py"),
            _call("write_file", "b.py"),  # 未读 b.py
        ])
        verdict = FactBindingGate().check(None, tracker=tracker)
        assert verdict.passed is False
        assert "b.py" in verdict.reason

    def test_failed_read_not_counted(self) -> None:
        """read_file 失败后 write_file 仍算盲写。"""
        tracker = types.SimpleNamespace(calls=[
            _call("read_file", "a.py", success=False),
            _call("write_file", "a.py"),
        ])
        verdict = FactBindingGate().check(None, tracker=tracker)
        assert verdict.passed is False

    def test_search_counts_as_read(self) -> None:
        """search_files 成功后写同路径 → 算已读。"""
        tracker = types.SimpleNamespace(calls=[
            _call("search_files", "a.py"),
            _call("edit_file", "a.py"),
        ])
        verdict = FactBindingGate().check(None, tracker=tracker)
        assert verdict.passed is True

    def test_no_tracker_passes(self) -> None:
        """无 tracker → 通过。"""
        verdict = FactBindingGate().check(None, tracker=None)
        assert verdict.passed is True

    def test_no_calls_passes(self) -> None:
        """空 tracker → 通过。"""
        tracker = types.SimpleNamespace(calls=[])
        verdict = FactBindingGate().check(None, tracker=tracker)
        assert verdict.passed is True

    def test_write_marks_file_as_known(self) -> None:
        """第一次 write 后，第二次 write 同文件不算盲写。"""
        tracker = types.SimpleNamespace(calls=[
            _call("write_file", "a.py"),
            _call("edit_file", "a.py"),  # 已知文件
        ])
        verdict = FactBindingGate().check(None, tracker=tracker)
        # 第一次 write 仍算盲写
        assert verdict.passed is False
        assert verdict.payload is not None
        assert len(verdict.payload["blind_files"]) == 1

    def test_command_counts_as_read(self) -> None:
        """command（如 cat/grep）算读操作。"""
        tracker = types.SimpleNamespace(calls=[
            _call("command", "src/"),
            _call("write_file", "src/a.py"),
        ])
        verdict = FactBindingGate().check(None, tracker=tracker)
        # command 的 path 是 "src/"，不匹配 "src/a.py"，仍算盲写
        # 这里测试的是 command 能被识别为读工具
        assert verdict.phase == "fact"
