"""Transactional and atomic guarantees for Agent file mutation tools."""

from __future__ import annotations

import stat

from xenon.engine.context import AgentContext
from xenon.nodes.tool_node import ToolNode
from xenon.utils.atomic_write import atomic_write_text


def test_atomic_write_preserves_mode_and_creates_exact_backup(tmp_path):
    target = tmp_path / "script.sh"
    target.write_text("old\n")
    target.chmod(0o754)

    atomic_write_text(target, "new\n", backup=True)

    assert target.read_text() == "new\n"
    assert (tmp_path / "script.sh.bak").read_text() == "old\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o754


def test_write_file_failure_keeps_original(monkeypatch, tmp_path):
    from xenon.nodes.tool_families import file_mutation

    target = tmp_path / "important.txt"
    target.write_text("original")
    monkeypatch.setattr(
        file_mutation,
        "atomic_write_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    result = ToolNode(
        "write",
        action_type="write_file",
        file_path=str(target),
        content="replacement",
    ).execute(AgentContext())

    assert result["success"] is False
    assert target.read_text() == "original"


def test_edit_file_is_atomic_and_leaves_backup(tmp_path):
    target = tmp_path / "main.py"
    target.write_text("value = 'old'\n")

    result = ToolNode(
        "edit",
        action_type="edit_file",
        file_path=str(target),
        old_text="'old'",
        new_text="'new'",
    ).execute(AgentContext())

    assert result["success"] is True
    assert target.read_text() == "value = 'new'\n"
    assert (tmp_path / "main.py.bak").read_text() == "value = 'old'\n"


def test_batch_write_rolls_back_existing_and_new_files(monkeypatch, tmp_path):
    from xenon.nodes.tool_families import file_mutation

    existing = tmp_path / "a.txt"
    created = tmp_path / "b.txt"
    failing = tmp_path / "c.txt"
    existing.write_text("old-a")
    real_atomic_write = file_mutation.atomic_write_text

    def fail_last(path, content, **kwargs):
        if str(path) == str(failing):
            raise OSError("simulated disk failure")
        return real_atomic_write(path, content, **kwargs)

    monkeypatch.setattr(file_mutation, "atomic_write_text", fail_last)
    result = ToolNode(
        "batch",
        action_type="batch_write",
        files=[
            {"path": str(existing), "content": "new-a"},
            {"path": str(created), "content": "new-b"},
            {"path": str(failing), "content": "new-c"},
        ],
    ).execute(AgentContext())

    assert result["success"] is False
    assert result["rolled_back"] is True
    assert result["success_count"] == 0
    assert existing.read_text() == "old-a"
    assert not created.exists()
    assert not failing.exists()


def test_batch_write_preflight_error_changes_nothing(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("old")

    result = ToolNode(
        "batch",
        action_type="batch_write",
        files=[
            {"path": str(target), "content": "new"},
            {"content": "missing path"},
        ],
    ).execute(AgentContext())

    assert result["success"] is False
    assert result["success_count"] == 0
    assert target.read_text() == "old"


def test_batch_edit_supports_ordered_edits_to_same_file(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("one two")

    result = ToolNode(
        "batch-edit",
        action_type="batch_edit",
        edits=[
            {"file_path": str(target), "old_text": "one", "new_text": "three"},
            {"file_path": str(target), "old_text": "two", "new_text": "four"},
        ],
    ).execute(AgentContext())

    assert result["success"] is True
    assert result["success_count"] == 2
    assert target.read_text() == "three four"


def test_batch_edit_execution_failure_restores_every_file(monkeypatch, tmp_path):
    from xenon.nodes.tool_families import file_mutation

    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text("old-a")
    second.write_text("old-b")
    real_atomic_write = file_mutation.atomic_write_text

    def fail_second(path, content, **kwargs):
        if str(path) == str(second):
            raise OSError("simulated disk failure")
        return real_atomic_write(path, content, **kwargs)

    monkeypatch.setattr(file_mutation, "atomic_write_text", fail_second)
    result = ToolNode(
        "batch-edit",
        action_type="batch_edit",
        edits=[
            {"file_path": str(first), "old_text": "old", "new_text": "new"},
            {"file_path": str(second), "old_text": "old", "new_text": "new"},
        ],
    ).execute(AgentContext())

    assert result["success"] is False
    assert result["rolled_back"] is True
    assert first.read_text() == "old-a"
    assert second.read_text() == "old-b"


class TestNoOpEditDetection:
    """编辑无变化检测（SWE-bench matplotlib-24970 实测）。

    new_text 与 old_text 内容相同（或归一化匹配吞缩进后等价）→ 之前
    edit 报告成功但 git diff 为空 → patch 丢失。现在视为失败让补救接管。
    """

    def test_identical_new_text_rejected(self, tmp_path):
        from xenon.nodes.tool_node import ToolNode as FileMutationTools

        target = tmp_path / "a.py"
        target.write_text("        value = 1\n", encoding="utf-8")
        tool = FileMutationTools(
            "e1",
            action_type="edit_file",
            file_path=str(target),
            old_text="value = 1",
            new_text="value = 1",
        )
        result = tool._edit_file(AgentContext())
        assert result["success"] is False
        assert "未产生任何变化" in result["error"]
        assert target.read_text(encoding="utf-8") == "        value = 1\n"

    def test_real_change_still_passes(self, tmp_path):
        from xenon.nodes.tool_node import ToolNode as FileMutationTools

        target = tmp_path / "a.py"
        target.write_text("value = 1\n", encoding="utf-8")
        tool = FileMutationTools(
            "e1",
            action_type="edit_file",
            file_path=str(target),
            old_text="value = 1",
            new_text="value = 2",
        )
        result = tool._edit_file(AgentContext())
        assert result["success"] is True
        assert target.read_text(encoding="utf-8") == "value = 2\n"
