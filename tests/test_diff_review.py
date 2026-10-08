"""A②：审批面板 diff 预览 + /review 事后审查与恢复。"""

from __future__ import annotations

import io
import sys

from rich.console import Console

from xenon.repl.command_groups.review import _cmd_review
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.repl import REPL


def _make_repl(tmp_path, monkeypatch) -> REPL:
    monkeypatch.setenv("XENON_SESSION_EVENTS_DIR", str(tmp_path))
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    return REPL(registry=registry, streaming=False)


def test_diff_preview_write_file_over_existing(tmp_path):
    target = tmp_path / "a.py"
    target.write_text("old_line\n", encoding="utf-8")
    diff = REPL._diff_preview(
        "write_file", {"file_path": str(target), "content": "new_line\n"}
    )
    assert "-old_line" in diff
    assert "+new_line" in diff


def test_diff_preview_new_file_shows_content():
    diff = REPL._diff_preview(
        "write_file", {"file_path": "C:/x/new.py", "content": "print(1)\n"}
    )
    assert "+print(1)" in diff


def test_diff_preview_edit_file_and_batch():
    diff = REPL._diff_preview(
        "edit_file",
        {"file_path": "a.py", "old_text": "x = 1", "new_text": "x = 2"},
    )
    assert "-x = 1" in diff and "+x = 2" in diff
    diff = REPL._diff_preview(
        "batch_write", {"files": [{"file_path": "a.py"}, {"file_path": "b.py"}]}
    )
    assert "a.py" in diff and "b.py" in diff


def test_approval_panel_includes_diff(monkeypatch, tmp_path):
    output = io.StringIO()
    monkeypatch.setattr(
        "xenon.repl.repl.console", Console(file=output, width=120, force_terminal=False)
    )
    monkeypatch.setattr(
        "xenon.repl.repl.get_config",
        lambda: type("C", (), {"interaction": type("I", (), {"assume_yes": False})()})(),
    )
    monkeypatch.setattr(sys, "stdin", type("T", (), {"isatty": lambda self: True})())
    monkeypatch.setattr("xenon.repl.repl.Prompt.ask", lambda *a, **k: "n")

    repl = _make_repl(tmp_path, monkeypatch)
    repl._confirm_tool_approval(
        "write_file",
        {"file_path": str(tmp_path.parent / "outside.py"), "content": "print(1)\n"},
    )

    assert "+print(1)" in output.getvalue()


def test_review_lists_changes_and_reverts_bak(monkeypatch, tmp_path):
    repl = _make_repl(tmp_path, monkeypatch)
    target = tmp_path / "mod.py"
    target.write_text("new", encoding="utf-8")
    (tmp_path / "mod.py.bak").write_text("original", encoding="utf-8")
    repl.ctx_mgr.update_working_memory("session_modified_files", [str(target)])

    listed = _cmd_review(args="", session_state={"_repl": repl})
    assert "mod.py" in listed

    reverted = _cmd_review(args=f"revert {target}", session_state={"_repl": repl})
    assert "已" in reverted
    assert target.read_text(encoding="utf-8") == "original"


def test_review_revert_without_bak_explains(monkeypatch, tmp_path):
    repl = _make_repl(tmp_path, monkeypatch)
    missing = tmp_path / "nope.py"
    output = _cmd_review(args=f"revert {missing}", session_state={"_repl": repl})
    assert "没有 .bak" in output
