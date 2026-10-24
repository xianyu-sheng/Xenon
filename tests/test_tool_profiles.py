"""Model-facing tool profiles and core aliases (#190)."""

from __future__ import annotations

from xenon.engine.context import AgentContext
from xenon.engine.react_engine import ReActEngine
from xenon.engine.tool_profiles import (
    CORE_TOOL_NAMES,
    filter_tools_for_profile,
    resolve_tool_profile,
)
from xenon.nodes.tool_node import ToolNode


def test_core_profile_filters_react_schema(monkeypatch):
    monkeypatch.setenv("XENON_TOOL_PROFILE", "core")
    engine = ReActEngine(["fake/model"])

    assert engine._tool_profile == "core"
    assert set(engine.tools) == set(CORE_TOOL_NAMES)
    assert "read" in engine.tools
    assert "search" in engine.tools
    assert "apply_patch" in engine.tools
    assert "bash" in engine.tools
    assert "write_file" not in engine.tools
    assert "run_tests" not in engine.tools


def test_legacy_profile_is_default(monkeypatch):
    monkeypatch.delenv("XENON_TOOL_PROFILE", raising=False)

    assert resolve_tool_profile() == "legacy"
    engine = ReActEngine(["fake/model"])
    assert "write_file" in engine.tools
    assert "run_tests" in engine.tools


def test_profile_filter_does_not_mutate_source():
    source = {"read": {}, "write_file": {}}
    filtered = filter_tools_for_profile(source, "core")

    assert set(filtered) == {"read"}
    assert set(source) == {"read", "write_file"}


def test_read_search_and_apply_patch_aliases_work(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hello\nworld\n", encoding="utf-8")

    read = ToolNode("r", action_type="read", file_path=str(target), cwd=str(tmp_path))
    read_result = read._read(AgentContext({}))
    assert read_result["success"] is True
    assert "hello" in (read_result.get("stdout") or read_result.get("content") or "")

    search = ToolNode(
        "s",
        action_type="search",
        file_path=str(tmp_path),
        search_pattern="world",
        cwd=str(tmp_path),
    )
    search_result = search._search_core(AgentContext({}))
    assert search_result["success"] is True
    assert "world" in (search_result.get("stdout") or search_result.get("content") or "")

    patch = ToolNode(
        "p",
        action_type="apply_patch",
        files=[{"path": "new.txt", "content": "new\n"}],
        cwd=str(tmp_path),
    )
    patch_result = patch._apply_patch(AgentContext({}))
    assert patch_result["success"] is True
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "new\n"

    edit = ToolNode(
        "e",
        action_type="apply_patch",
        edits=[
            {
                "file_path": str(target),
                "old_text": "world",
                "new_text": "there",
            }
        ],
        cwd=str(tmp_path),
    )
    edit_result = edit._apply_patch(AgentContext({}))
    assert edit_result["success"] is True
    assert "there" in target.read_text(encoding="utf-8")


def test_core_bash_preserves_test_counts(tmp_path):
    import sys

    (tmp_path / "test_x.py").write_text(
        "def test_ok():\n    assert 2 + 2 == 4\n", encoding="utf-8"
    )
    node = ToolNode(
        "b",
        action_type="bash",
        action=f"{sys.executable} -m pytest test_x.py -q",
        cwd=str(tmp_path),
    )
    result = node._bash(AgentContext({}))

    assert result["success"] is True
    assert result["tests_run"] == 1
    assert result["tests_passed"] == 1
    assert result["tests_failed"] == 0
