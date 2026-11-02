"""Per-child worktrees (#358).

The bug these exist for: two parallel children sharing one workspace erased each
other's edits, and both ledgers reported success.  Isolation is the only honest
answer because a natural-language objective has no static target set to compare.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from xenon.harness.child_worktree import (
    ChildWorktree,
    WorktreeUnavailable,
    apply_patch,
    is_git_workspace,
    worktree_paths,
)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "shared.py").write_text(
        "def one():\n    return 1\n\n\ndef three():\n    return 3\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=x@y", "-c", "user.name=x", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )
    return root


def test_a_worktree_is_a_separate_checkout(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    tree = ChildWorktree.create(root, "c-sub1")
    try:
        assert tree.path != root
        assert (tree.path / "shared.py").read_text() == (root / "shared.py").read_text()
    finally:
        tree.cleanup()


def test_a_write_in_one_worktree_does_not_touch_the_shared_one(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    tree = ChildWorktree.create(root, "c-sub1")
    try:
        (tree.path / "shared.py").write_text("changed\n", encoding="utf-8")
        assert (root / "shared.py").read_text().startswith("def one")
    finally:
        tree.cleanup()


def test_two_worktrees_do_not_clobber_each_other(tmp_path: Path) -> None:
    """The whole point: both edits survive, because neither shares a file."""

    root = _repo(tmp_path)
    first = ChildWorktree.create(root, "c-sub1")
    second = ChildWorktree.create(root, "c-sub2")
    try:
        (first.path / "shared.py").write_text(
            "def one():\n    return 111\n\n\ndef three():\n    return 3\n",
            encoding="utf-8",
        )
        (second.path / "shared.py").write_text(
            "def one():\n    return 1\n\n\ndef three():\n    return 333\n",
            encoding="utf-8",
        )
        patch_a = first.patch()
        patch_b = second.patch()
    finally:
        first.cleanup()
        second.cleanup()

    ok, reason = apply_patch(root, patch_a)
    assert ok, reason
    ok, reason = apply_patch(root, patch_b)
    assert ok, reason

    result = (root / "shared.py").read_text()
    assert "return 111" in result
    assert "return 333" in result


def test_a_new_file_is_part_of_the_patch(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    tree = ChildWorktree.create(root, "c-sub1")
    try:
        (tree.path / "new.py").write_text("VALUE = 7\n", encoding="utf-8")
        patch = tree.patch()
    finally:
        tree.cleanup()

    ok, reason = apply_patch(root, patch)
    assert ok, reason
    assert (root / "new.py").read_text() == "VALUE = 7\n"


def test_a_conflicting_patch_is_reported_not_applied(tmp_path: Path) -> None:
    """Two children editing the same line: the second must be a reported conflict."""

    root = _repo(tmp_path)
    first = ChildWorktree.create(root, "c-sub1")
    second = ChildWorktree.create(root, "c-sub2")
    try:
        (first.path / "shared.py").write_text(
            "def one():\n    return 111\n\n\ndef three():\n    return 3\n",
            encoding="utf-8",
        )
        (second.path / "shared.py").write_text(
            "def one():\n    return 222\n\n\ndef three():\n    return 3\n",
            encoding="utf-8",
        )
        patch_a = first.patch()
        patch_b = second.patch()
    finally:
        first.cleanup()
        second.cleanup()

    assert apply_patch(root, patch_a)[0] is True
    applied, reason = apply_patch(root, patch_b)

    assert applied is False
    assert reason
    # The first child's work is intact: no silent winner.
    assert "return 111" in (root / "shared.py").read_text()


def test_cleanup_leaves_no_worktree_behind(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    tree = ChildWorktree.create(root, "c-sub1")
    path = tree.path
    tree.cleanup()

    assert not path.exists()
    assert all("c-sub1" not in entry for entry in worktree_paths(root))


def test_a_non_git_workspace_cannot_be_isolated(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    assert is_git_workspace(plain) is False
    with pytest.raises(WorktreeUnavailable):
        ChildWorktree.create(plain, "c-sub1")


def test_applying_a_patch_outside_git_is_a_reported_failure(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    applied, reason = apply_patch(plain, "diff --git a/x b/x\n")

    assert applied is False
    assert "not a git worktree" in reason


def test_an_empty_patch_is_a_no_op(tmp_path: Path) -> None:
    root = _repo(tmp_path)

    assert apply_patch(root, "   ")[0] is True
