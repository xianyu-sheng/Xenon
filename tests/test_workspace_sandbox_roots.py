"""/workspace add must actually widen the fence (#412).

A dogfood run wanted to write CI files into a repo one directory over.  The
sandbox refused (correctly), but the only remedy was `XENON_EXTRA_ROOTS=` at
launch — undiscoverable from the refusal and impossible mid-session, so the
run ended in "waiting for your answer" with no way forward.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from xenon.nodes import sandbox_roots
from xenon.nodes.tool_node import ToolNode


class _Node(ToolNode):
    """A ToolNode with the workspace pointed somewhere specific."""

    def __init__(self, workspace: Path) -> None:  # noqa: D107 - test double
        object.__setattr__(self, "cwd", workspace)


def _node(workspace: Path) -> _Node:
    node = object.__new__(ToolNode)
    node.cwd = workspace
    node.security_enabled = True
    return node


@pytest.fixture(autouse=True)
def _clean_roots(monkeypatch):
    import xenon.nodes.tool_node as tool_node

    sandbox_roots.clear_session_roots()
    monkeypatch.delenv("XENON_EXTRA_ROOTS", raising=False)
    # tmp_path lives under the system temp dir, which is a *default* extra
    # root; clear it so "outside the fence" means what the test says.
    monkeypatch.setattr(tool_node, "_DEFAULT_EXTRA_ROOTS", ())
    yield
    sandbox_roots.clear_session_roots()


def test_a_sibling_directory_is_outside_until_it_is_added(
    tmp_path, real_path_validation
):
    from xenon.nodes.tool_node import SecurityError

    workspace = tmp_path / "repo"
    sibling = tmp_path / "product"
    workspace.mkdir()
    sibling.mkdir()
    node = _node(workspace)

    with pytest.raises(SecurityError) as excinfo:
        node._validate_path(str(sibling / "ci.yml"), for_write=True)

    assert "/workspace add" in str(excinfo.value), (
        "the refusal must name the command that fixes it"
    )

    sandbox_roots.add_root(sibling)
    resolved = node._validate_path(str(sibling / "ci.yml"), for_write=True)
    assert resolved == (sibling / "ci.yml").resolve()


def test_reads_stay_free_outside_the_fence(tmp_path, real_path_validation):
    """The fence constrains writes; reading is not a permission event."""
    workspace = tmp_path / "repo"
    outside = tmp_path / "elsewhere"
    workspace.mkdir()
    outside.mkdir()

    resolved = _node(workspace)._validate_path(str(outside / "note.md"))

    assert resolved == (outside / "note.md").resolve()


def test_adding_a_missing_directory_is_refused(tmp_path):
    with pytest.raises(ValueError):
        sandbox_roots.add_root(tmp_path / "nope")


def test_removing_and_clearing(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    sandbox_roots.add_root(a)
    sandbox_roots.add_root(b)

    assert sandbox_roots.remove_root(a) is True
    assert sandbox_roots.remove_root(a) is False
    assert sandbox_roots.session_roots() == (b,)

    sandbox_roots.clear_session_roots()
    assert sandbox_roots.session_roots() == ()


def test_the_env_var_still_works(monkeypatch, tmp_path):
    extra = tmp_path / "env-root"
    extra.mkdir()
    monkeypatch.setenv("XENON_EXTRA_ROOTS", str(extra))

    assert sandbox_roots.env_roots() == (extra,)


def test_a_directory_read_says_it_is_a_directory(tmp_path):
    """`read` on a directory raised PermissionError [Errno 13] on Windows,
    which read as "the sandbox denied you" (#411)."""
    from xenon.nodes.tool_families.read_only_files import ReadOnlyFileToolsMixin

    holder = tmp_path / "dir"
    holder.mkdir()
    (holder / "inside.txt").write_text("x", encoding="utf-8")

    class _Tools(ReadOnlyFileToolsMixin):
        id = "exec_read"

        def __init__(self, target: Path) -> None:
            self.file_path = str(target)

        def _resolve_template(self, value, context):  # noqa: ANN001
            return value

        def _validate_path(self, value, *, for_write=False):  # noqa: ANN001
            return Path(value)

        def _write_output(self, context, text):  # noqa: ANN001
            return None

    result = _Tools(holder)._read_file(None)

    assert result["success"] is False
    assert result["is_directory"] is True
    assert result["entries"] == ["inside.txt"]
    assert "这是一个目录" in result["error"]
