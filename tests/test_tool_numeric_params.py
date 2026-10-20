"""Model-supplied numeric tool params must not leak strings into comparisons."""

from __future__ import annotations

from xenon.nodes.tool_node import ToolNode


def test_list_files_max_depth_string_is_coerced(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "deep.py").write_text("x = 1\n", encoding="utf-8")

    node = ToolNode(
        "t",
        action_type="list_files",
        file_path=str(tmp_path),
        pattern="*.py",
        max_depth="0",
    )

    found = list(node._walk_with_depth(tmp_path, "*.py", node.max_depth))
    assert node.max_depth == 0
    assert found == []


def test_numeric_tool_params_are_normalized():
    node = ToolNode(
        "t",
        action_type="read_file",
        timeout="5.5",
        max_depth="2",
        limit="10",
        max_pages="3",
        max_chars="4000",
        start_line="7",
        max_lines="20",
        line="3",
        column="4",
    )

    assert node.timeout == 5.5
    assert node.max_depth == 2
    assert node.limit == 10
    assert node.max_pages == 3
    assert node.max_chars == 4000
    assert node._extra_start_line == 7
    assert node._extra_max_lines == 20
    assert node._lsp_line == 3
    assert node._lsp_column == 4


def test_invalid_numeric_tool_params_fall_back():
    node = ToolNode(
        "t",
        action_type="read_file",
        timeout="not-a-number",
        max_depth="nope",
        limit="nope",
        start_line="nope",
    )

    assert node.timeout == 60.0
    assert node.max_depth == 5
    assert node.limit is None
    assert node._extra_start_line is None
