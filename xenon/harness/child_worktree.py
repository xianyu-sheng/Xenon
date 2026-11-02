"""Per-child git worktrees: isolation instead of a race (#358).

Two children sharing one workspace clobber each other.  Observed on a real run:
both edited a different function of the same file, and the second write erased
the first — while both ledgers reported the edit as done.

`parallel-branching.md` says write conflicts must be statically decidable (disjoint
target sets).  A child's objective is natural language, so there is no target set
to compare.  Isolation is therefore not an optimisation, it is the only honest
answer: each child gets its own checkout, and its work comes back as a patch that
the parent applies deliberately.

What this deliberately does not do: merge.  A merge conflict needs an arbiter,
and the arbiter must not be the model.  The patch either applies cleanly or it is
reported as a conflict the caller has to deal with.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class WorktreeUnavailable(RuntimeError):
    """The workspace is not a git checkout, so isolation cannot be provided."""


def _git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
        stdin=subprocess.DEVNULL,
    )


def is_git_workspace(workspace: str | Path) -> bool:
    """True when a worktree can be created here."""

    root = Path(workspace)
    if not root.is_dir():
        return False
    probe = _git("rev-parse", "--is-inside-work-tree", cwd=root, check=False)
    return probe.returncode == 0 and probe.stdout.strip() == "true"


@dataclass
class ChildWorktree:
    """One child's private checkout, plus the patch it produced."""

    workspace: Path
    path: Path
    base: str
    _created_by_us: bool = True

    @classmethod
    def create(cls, workspace: str | Path, child_id: str) -> ChildWorktree:
        root = Path(workspace).resolve()
        if not is_git_workspace(root):
            raise WorktreeUnavailable(f"{root} is not a git worktree")
        head = _git("rev-parse", "HEAD", cwd=root).stdout.strip()
        if not head:
            raise WorktreeUnavailable("repository has no commits to branch from")
        parent = Path(tempfile.mkdtemp(prefix=f"{child_id}-wt-"))
        target = parent / "tree"
        created = _git(
            "worktree", "add", "--detach", str(target), head, cwd=root, check=False
        )
        if created.returncode != 0:
            shutil.rmtree(parent, ignore_errors=True)
            raise WorktreeUnavailable(
                "git worktree add failed: " + (created.stderr or "").strip()[:200]
            )
        return cls(workspace=root, path=target, base=head)

    def patch(self) -> str:
        """The child's work as a unified diff against the base commit.

        The work is committed inside the throwaway worktree first.  That is not
        bookkeeping for its own sake: ``git apply --3way`` can only merge when
        both the pre- and post-image blobs exist in the object database, and the
        post-image only exists once the child's tree has been written as an
        object.  Without it, two children editing *disjoint* functions of the
        same small file would still look like a conflict.

        Untracked files are included because a new file is a result, not noise.
        """

        _git("add", "-A", cwd=self.path, check=False)
        _git(
            "-c",
            "user.email=child@xenon.invalid",
            "-c",
            "user.name=xenon-child",
            "commit",
            "-q",
            "--no-verify",
            "-m",
            "child work",
            cwd=self.path,
            check=False,
        )
        diff = _git("diff", "--binary", self.base, cwd=self.path, check=False)
        return diff.stdout

    def cleanup(self) -> None:
        """Remove the checkout.  Never raises: cleanup must not mask a result."""

        if not self._created_by_us:
            return
        try:
            _git("worktree", "remove", "--force", str(self.path), cwd=self.workspace)
        except Exception:  # noqa: BLE001 — best effort
            pass
        shutil.rmtree(self.path.parent, ignore_errors=True)
        _git("worktree", "prune", cwd=self.workspace, check=False)


def apply_patch(workspace: str | Path, patch: str) -> tuple[bool, str]:
    """Apply a child's patch to the parent workspace.

    ``--3way`` is used so a conflict is reported as a conflict instead of being
    silently applied at the wrong offset.  Returns ``(applied, reason)``.
    """

    if not patch.strip():
        return True, "empty patch"
    root = Path(workspace)
    if not is_git_workspace(root):
        return False, f"{root} is not a git worktree"
    applied = subprocess.run(
        # ``--ignore-whitespace`` is not laziness: a workspace with
        # ``core.autocrlf=true`` holds CRLF files while the patch is LF, and
        # ``git apply`` refuses the pre-image on a byte comparison.  Combined
        # with ``--3way`` the merge is still a real three-way merge.
        ["git", "apply", "--3way", "--ignore-whitespace", "--whitespace=nowarn", "-"],
        cwd=str(root),
        input=patch,
        capture_output=True,
        text=True,
        check=False,
    )
    if applied.returncode == 0:
        return True, "applied"
    reason = (applied.stderr or applied.stdout or "").strip()
    return False, reason[:400] or "git apply failed"


def worktree_paths(workspace: str | Path) -> list[str]:
    """Every worktree git knows about (used to assert cleanup)."""

    root = Path(workspace)
    if not is_git_workspace(root):
        return []
    listing = _git("worktree", "list", "--porcelain", cwd=root, check=False)
    return [
        line.split(" ", 1)[1]
        for line in listing.stdout.splitlines()
        if line.startswith("worktree ")
    ]


def as_dict(item: Any) -> dict[str, Any]:
    """Small helper for evidence payloads."""

    return {
        "workspace": str(getattr(item, "workspace", "")),
        "path": str(getattr(item, "path", "")),
        "base": str(getattr(item, "base", "")),
    }


__all__ = [
    "ChildWorktree",
    "WorktreeUnavailable",
    "apply_patch",
    "as_dict",
    "is_git_workspace",
    "worktree_paths",
]
