"""/workspace add must actually widen the fence (#412).

A dogfood run wanted to write CI files into a repo one directory over.  The
sandbox refused (correctly), but the only remedy was `XENON_EXTRA_ROOTS=` at
launch — undiscoverable from the refusal and impossible mid-session, so the
run ended in "waiting for your answer" with no way forward.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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


def _repl(root: Path):
    return SimpleNamespace(_workspace_root=lambda: root)


def test_the_workspace_command_lists_every_root_source(tmp_path, monkeypatch):
    from xenon.repl.command_groups.workspace import _cmd_workspace

    env_root = tmp_path / "env"
    env_root.mkdir()
    monkeypatch.setenv("XENON_EXTRA_ROOTS", str(env_root))
    session_root = tmp_path / "session"
    session_root.mkdir()
    sandbox_roots.add_root(session_root)

    text = _cmd_workspace(
        args="", session_state={"_repl": _repl(tmp_path / "ws")}
    )

    assert "工作区（写入围栏的根）" in text
    assert str(env_root) in text
    assert str(session_root) in text
    assert "读取不受限" in text


def test_the_workspace_command_adds_and_removes(tmp_path):
    from xenon.repl.command_groups.workspace import _cmd_workspace

    target = tmp_path / "repo"
    target.mkdir()
    state = {"_repl": _repl(tmp_path)}

    added = _cmd_workspace(args=f"add {target}", session_state=state)
    assert "已加入可写目录" in added
    assert sandbox_roots.session_roots() == (target.resolve(),)

    removed = _cmd_workspace(args=f"remove {target}", session_state=state)
    assert "已移除" in removed
    assert sandbox_roots.session_roots() == ()


def test_the_workspace_command_reports_bad_input(tmp_path):
    from xenon.repl.command_groups.workspace import _cmd_workspace

    state = {"_repl": _repl(tmp_path)}

    assert "用法" in _cmd_workspace(args="add", session_state=state)
    assert "用法" in _cmd_workspace(args="remove", session_state=state)
    assert "用法" in _cmd_workspace(args="dance", session_state=state)
    assert "无法添加" in _cmd_workspace(
        args=f"add {tmp_path / 'missing'}", session_state=state
    )
    assert "不在本会话" in _cmd_workspace(
        args=f"remove {tmp_path}", session_state=state
    )


def test_the_workspace_command_clears(tmp_path):
    from xenon.repl.command_groups.workspace import _cmd_workspace

    target = tmp_path / "one"
    target.mkdir()
    sandbox_roots.add_root(target)

    assert "已清空" in _cmd_workspace(
        args="clear", session_state={"_repl": _repl(tmp_path)}
    )
    assert sandbox_roots.session_roots() == ()


def test_an_unlistable_directory_is_reported_not_raised(tmp_path, monkeypatch):
    """A directory we may not list must still answer, not explode."""
    from xenon.nodes.tool_families.read_only_files import ReadOnlyFileToolsMixin

    holder = tmp_path / "locked"
    holder.mkdir()

    class _Tools(ReadOnlyFileToolsMixin):
        id = "exec_read"

        def __init__(self, target):
            self.file_path = str(target)

        def _resolve_template(self, value, context):
            return value

        def _validate_path(self, value, *, for_write=False):
            return Path(value)

        def _write_output(self, context, text):
            return None

    def _boom(self):
        raise OSError("access denied")

    monkeypatch.setattr(Path, "iterdir", _boom)
    result = _Tools(holder)._read_file(None)

    assert result["is_directory"] is True
    assert result["entries"] == []
    assert "列出失败" in result["error"]


def test_an_unparseable_env_root_is_skipped():
    # A NUL byte makes Path() raise ValueError before any filesystem call.
    # Letting it escape would break every tool call in the process, so the
    # parser (which takes the raw value, not just the environment) skips it.
    assert sandbox_roots.env_roots("bad" + chr(0) + "entry") == ()


def test_the_listing_survives_an_empty_default_root_set(tmp_path, monkeypatch):
    import xenon.nodes.tool_node as tool_node
    from xenon.repl.command_groups.workspace import _cmd_workspace

    monkeypatch.setattr(tool_node, "_DEFAULT_EXTRA_ROOTS", ())
    text = _cmd_workspace(args="", session_state={"_repl": _repl(tmp_path)})

    assert "额外可写目录" in text
    assert "默认临时目录" in text


def test_a_root_that_cannot_be_read_is_skipped(monkeypatch):
    """``is_dir()`` can raise (bad encoding, NUL byte); parsing must survive."""
    def _boom(_path):
        raise ValueError("embedded null character")

    monkeypatch.setattr(Path, "is_dir", _boom)

    assert sandbox_roots.env_roots("anything") == ()


def test_the_listing_shows_a_default_root(tmp_path, monkeypatch):
    import xenon.nodes.tool_node as tool_node
    from xenon.repl.command_groups.workspace import _cmd_workspace

    monkeypatch.setattr(tool_node, "_DEFAULT_EXTRA_ROOTS", (tmp_path / "scratch",))
    text = _cmd_workspace(args="list", session_state={"_repl": _repl(tmp_path)})

    assert "默认临时目录" in text
    assert "·" in text, "each configured root is listed"


def test_an_approved_write_is_allowed_once(tmp_path, real_path_validation):
    """#413: the approval panel asks *before* the tool runs.

    Without a one-shot grant the fence refused anyway, so answering "yes" in
    the panel had no effect — the only way forward was an env var and a
    restart, which is what made Xenon feel far more restricted than Claude
    Code or pi.
    """
    from xenon.nodes.tool_node import SecurityError

    workspace = tmp_path / "repo"
    sibling = tmp_path / "product"
    workspace.mkdir()
    sibling.mkdir()
    node = _node(workspace)
    target = sibling / "ci.yml"

    sandbox_roots.grant_once(target)

    assert node._validate_path(str(target), for_write=True) == target.resolve()
    # One approval, one write.
    with pytest.raises(SecurityError):
        node._validate_path(str(target), for_write=True)


def test_an_approval_for_one_path_does_not_open_the_directory(
    tmp_path, real_path_validation
):
    from xenon.nodes.tool_node import SecurityError

    workspace = tmp_path / "repo"
    sibling = tmp_path / "product"
    workspace.mkdir()
    sibling.mkdir()
    node = _node(workspace)

    sandbox_roots.grant_once(sibling / "ci.yml")

    with pytest.raises(SecurityError):
        node._validate_path(str(sibling / "other.yml"), for_write=True)


def test_a_grant_is_never_used_for_reads(tmp_path, real_path_validation):
    """Reads are free anyway; a grant must not be silently consumed by one."""
    workspace = tmp_path / "repo"
    sibling = tmp_path / "product"
    workspace.mkdir()
    sibling.mkdir()
    node = _node(workspace)
    target = sibling / "ci.yml"

    sandbox_roots.grant_once(target)
    node._validate_path(str(target))  # a read

    assert sandbox_roots.pending_grants() == (target.resolve(),)
