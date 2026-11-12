"""Directory walks are bounded and skip dependency/cache trees.

2026-11-12: a `search` call without `path` defaulted to the workspace, the
workspace was ``D:\\``, and the tool walked the entire drive while reading
every file.  The UI froze and no receipt was ever written.  These tests pin
the two guarantees that prevent a repeat: heavy directories are skipped, and
a walk stops at a hard cap *and says so*.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from xenon.nodes.tool_families import read_only_files
from xenon.nodes.tool_families.read_only_files import ReadOnlyFileToolsMixin


class _Tool(ReadOnlyFileToolsMixin):
    id = "walk-test"
    file_path = ""
    pattern = ""
    max_depth = 6
    limit = None
    cursor = None
    search_pattern = ""
    file_filter = ""
    encoding = "utf-8"

    def __init__(self) -> None:
        self.outputs: list[str] = []

    def _resolve_template(self, value, context):  # noqa: ANN001, ANN201
        return value

    def _validate_path(self, path, for_write=False):  # noqa: ANN001, ANN201
        return Path(path).resolve()

    def _write_output(self, context, text):  # noqa: ANN001, ANN201
        self.outputs.append(text)


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    (tmp_path / "a.py").write_text("needle in a.py\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("plain\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.py").write_text("needle in vendor\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (tmp_path / "huge.log").write_text("needle " + "x" * (3 * 1024 * 1024), encoding="utf-8")
    return tmp_path


def test_list_files_skips_vendor_and_vcs_trees(tree: Path) -> None:
    tool = _Tool()
    tool.file_path = str(tree)
    tool.pattern = "*"

    result = tool._list_files(None)

    names = {Path(name).name for name in result["files"]}
    assert {"a.py", "b.py"} <= names
    assert "dep.py" not in names
    assert "HEAD" not in names
    assert result["scan_truncated"] is False


def test_search_skips_vendor_trees_and_large_files(tree: Path) -> None:
    tool = _Tool()
    tool.file_path = str(tree)
    tool.search_pattern = "needle"
    tool.max_depth = 6

    result = tool._search_files(None)

    files = {Path(row["file"]).name for row in result["matches"]}
    assert files == {"a.py"}
    assert result["skipped_large"] >= 1
    assert "node_modules" not in result["stdout"]


def test_a_long_walk_stops_at_the_cap_and_says_so(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(read_only_files, "MAX_WALK_FILES", 1)
    tool = _Tool()
    tool.file_path = str(tree)
    tool.pattern = "*"

    result = tool._list_files(None)

    assert result["scan_truncated"] is True
    assert "不完整" in tool.outputs[-1]


def test_search_stops_after_the_file_cap(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(read_only_files, "MAX_SEARCH_FILES", 1)
    tool = _Tool()
    tool.file_path = str(tree)
    tool.search_pattern = "plain"
    tool.max_depth = 6

    result = tool._search_files(None)

    assert result["scan_truncated"] is True
    assert "不完整" in result["stdout"]
