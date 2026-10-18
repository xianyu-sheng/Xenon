"""P1-3：恢复会话时检测工作目录变化。"""

from __future__ import annotations

from xenon.repl.session import cwd_change_notice


def test_same_cwd_has_no_notice() -> None:
    assert cwd_change_notice({"cwd": "/repo"}, current="/repo") == ""


def test_missing_cwd_has_no_notice() -> None:
    assert cwd_change_notice({}, current="/repo") == ""
    assert cwd_change_notice({"cwd": ""}, current="/repo") == ""


def test_different_cwd_warns_with_both_paths() -> None:
    notice = cwd_change_notice({"cwd": "C:\\old"}, current="D:\\new")
    assert "C:\\old" in notice
    assert "D:\\new" in notice
    assert "旧路径" in notice
