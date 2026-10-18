"""P2-2：恢复会话时自动重映射仍存在的旧路径（仅工作记忆）。"""

from __future__ import annotations

from xenon.repl.session import remap_workspace_paths


def test_existing_relative_path_is_remapped(tmp_path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    (old / "src").mkdir(parents=True)
    (new / "src").mkdir(parents=True)
    (old / "src" / "a.py").write_text("x", encoding="utf-8")
    (new / "src" / "a.py").write_text("x", encoding="utf-8")

    old_file = str(old / "src" / "a.py")
    memory = {"session_modified_files": [old_file]}
    remapped, mapping = remap_workspace_paths(
        memory, old_cwd=str(old), new_cwd=str(new)
    )

    assert remapped["session_modified_files"] == [str(new / "src" / "a.py")]
    assert mapping == {old_file: str(new / "src" / "a.py")}


def test_missing_path_is_left_untouched(tmp_path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    old_file = str(old / "gone.py")

    remapped, mapping = remap_workspace_paths(
        {"files": [old_file]}, old_cwd=str(old), new_cwd=str(new)
    )

    assert remapped["files"] == [old_file]
    assert mapping == {}


def test_unrelated_paths_and_nested_values(tmp_path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    (old / "pkg").mkdir(parents=True)
    (new / "pkg").mkdir(parents=True)
    (old / "pkg" / "b.py").write_text("x", encoding="utf-8")
    (new / "pkg" / "b.py").write_text("x", encoding="utf-8")

    memory = {
        "meta": {"note": "keep me", "count": 2},
        "files": {"path": str(old / "pkg" / "b.py"), "external": "/etc/hosts"},
    }
    remapped, mapping = remap_workspace_paths(
        memory, old_cwd=str(old), new_cwd=str(new)
    )

    assert remapped["meta"] == {"note": "keep me", "count": 2}
    assert remapped["files"]["path"] == str(new / "pkg" / "b.py")
    assert remapped["files"]["external"] == "/etc/hosts"
    assert len(mapping) == 1
