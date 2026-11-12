"""Deterministic worktree probe.

``NO_STRAY_CHANGES`` used to abstain forever because the kernel has no way to
look at the worktree — and it must not: the kernel is pure, it does not shell
out.  So the observation is made here (outside the kernel), handed to the
runtime, and recorded as a receipt effect.  The resolver then reads a *ledger
fact*, which keeps replay reproducible: the same ledger always answers the
same way, even if the worktree has changed since.

Returning ``None`` means "cannot tell" (no git, unreadable repo).  That stays
honest: a probe that cannot run must not look like a clean worktree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence

from xenon.kernel.contracts import WorkspaceProbe

__all__ = ["GitWorktreeProbe"]


def _filtered(paths: Sequence[str]) -> tuple[str, ...]:
    """Cache and VCS bookkeeping are not "stray source changes"."""

    return tuple(
        path
        for path in paths
        if "__pycache__/" not in path
        and ".pytest_cache/" not in path
        and not path.endswith((".pyc", ".pyo"))
        and not path.startswith(".xenon/")
    )


class GitWorktreeProbe:
    """Read ``git status`` and the current head for one workspace."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        timeout: float = 30.0,
        base: str | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.timeout = timeout
        # ``base`` anchors what counts as "different from where this session
        # started".  Reviewing a finished session against a bare worktree asks
        # the wrong question: work the agent already committed is not dirty, so
        # every committed file would read as an unmet claim.
        self.base = base

    def probe(self, *, verified_paths: tuple[str, ...] = ()) -> WorkspaceProbe | None:
        changes = self._status_changes()
        if changes is None:
            return None
        return WorkspaceProbe(
            dirty_paths=changes,
            verified_paths=tuple(verified_paths),
            head=self._head(),
        )

    def changed_since(self, base: str) -> tuple[str, ...] | None:
        """Paths differing between ``base`` and the worktree.  None = cannot tell."""

        if not base:
            return self._status_changes()
        committed = self._run("diff", "--name-only", "-z", base, "HEAD")
        if committed is None or committed.returncode != 0:
            return None
        uncommitted = self._status_changes()
        if uncommitted is None:
            return None
        paths = {
            item
            for item in committed.stdout.decode("utf-8", errors="replace").split("\0")
            if item
        }
        return _filtered(sorted(paths | set(uncommitted)))

    def commit_before(self, when: float) -> str:
        """Newest commit at or before ``when`` (a UNIX timestamp).

        Used to anchor an audit at the state the reviewed session started
        from.  An empty result means "cannot tell", which the caller must
        treat as such rather than as "no changes".
        """

        result = self._run(
            "rev-list", "-1", f"--before={int(when)}", "HEAD"
        )
        if result is None or result.returncode != 0:
            return ""
        return result.stdout.decode("utf-8", errors="replace").strip()

    def head(self) -> str:
        return self._head()

    def _run(self, *args: str) -> subprocess.CompletedProcess[bytes] | None:
        try:
            return subprocess.run(
                ["git", *args],
                cwd=self.workspace,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                check=False,
                timeout=self.timeout,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    def _status_changes(self) -> tuple[str, ...] | None:
        result = self._run("status", "--porcelain", "-z", "--untracked-files=all")
        if result is None or result.returncode != 0:
            return None
        entries = result.stdout.decode("utf-8", errors="replace").split("\0")
        paths: list[str] = []
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if len(entry) < 3:
                continue
            code = entry[:2]
            paths.append(entry[3:])
            if code[0] in {"R", "C"} and index < len(entries):
                # A rename/copy carries the original path in the next entry.
                index += 1
        return _filtered(paths)

    def _head(self) -> str:
        result = self._run("rev-parse", "HEAD")
        if result is None or result.returncode != 0:
            return ""
        return result.stdout.decode("utf-8", errors="replace").strip()
