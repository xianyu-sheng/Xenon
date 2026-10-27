"""Deterministic git finalizer (#201).

This is the delivery-layer counterpart to ``LegacyToolGateway``: it stages only
verified paths, refuses stray source changes, and creates a deterministic commit
without an LLM.  It intentionally lives outside ``xenon.kernel`` because it runs
git commands; the runtime receives a ``Finalizer`` protocol implementation.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

_IGNORED_PARTS = frozenset(
    {
        ".git",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        "node_modules",
    }
)
_IGNORED_SUFFIXES = (".pyc", ".pyo")


@dataclass(frozen=True, slots=True)
class FinalizeOutcome:
    accepted: bool
    reason: str = ""
    commit_sha: str | None = None
    files: tuple[str, ...] = field(default_factory=tuple)
    created: bool = False
    staged: tuple[str, ...] = field(default_factory=tuple)


class GitFinalizer:
    def __init__(self, workspace: str | Path, *, timeout: float = 60.0) -> None:
        self.workspace = Path(workspace).resolve()
        self.timeout = timeout

    def finalize(
        self,
        *,
        verified_paths: Sequence[str],
        objective: str,
        task_id: str,
        existing_commit_sha: str | None = None,
    ) -> FinalizeOutcome:
        verified = self._verified(verified_paths)
        if verified is None:
            return FinalizeOutcome(
                accepted=False, reason="verified path escapes workspace"
            )
        changes = self._status_changes()
        if changes is None:
            return FinalizeOutcome(
                accepted=False, reason="cannot read git status"
            )
        unverified = sorted(
            path
            for path in changes
            if not self._ignored(path) and path not in verified
        )
        if unverified:
            return FinalizeOutcome(
                accepted=False,
                reason="worktree contains unverified paths: "
                + ", ".join(unverified[:10]),
            )

        staged_paths = sorted(verified.intersection(changes))
        if staged_paths:
            added = self._run("add", "--all", "--", *staged_paths)
            if added.returncode != 0:
                return FinalizeOutcome(
                    accepted=False,
                    reason="git add failed: " + self._output(added),
                )
        staged = self._staged_paths()
        if staged is None:
            return FinalizeOutcome(
                accepted=False, reason="cannot read staged paths"
            )
        illegal = sorted(set(staged) - verified)
        if illegal:
            return FinalizeOutcome(
                accepted=False,
                reason="staged paths are not verified: " + ", ".join(illegal),
            )
        if not staged:
            if existing_commit_sha:
                return self._existing(existing_commit_sha, verified)
            return FinalizeOutcome(accepted=True)

        message = self._message(objective, task_id)
        committed = self._run("commit", "-m", message)
        if committed.returncode != 0:
            return FinalizeOutcome(
                accepted=False,
                reason="git commit failed: " + self._output(committed),
            )
        sha = self._head()
        if not sha:
            return FinalizeOutcome(
                accepted=False, reason="commit succeeded but HEAD is unavailable"
            )
        files = self._files_at(sha)
        unverified_files = sorted(set(files) - verified)
        if unverified_files:
            return FinalizeOutcome(
                accepted=False,
                reason="new commit contains unverified paths: "
                + ", ".join(unverified_files),
                commit_sha=sha,
            )
        return FinalizeOutcome(
            accepted=True,
            commit_sha=sha,
            files=files,
            created=True,
            staged=tuple(staged),
        )

    def _existing(
        self, sha: str, verified: set[str]
    ) -> FinalizeOutcome:
        files = self._files_at(sha)
        if not files:
            return FinalizeOutcome(
                accepted=False,
                reason="existing commit has no file manifest",
                commit_sha=sha,
            )
        unverified = sorted(set(files) - verified)
        if unverified:
            return FinalizeOutcome(
                accepted=False,
                reason="existing commit contains unverified paths: "
                + ", ".join(unverified),
                commit_sha=sha,
            )
        return FinalizeOutcome(
            accepted=True, commit_sha=sha, files=files
        )

    def _verified(self, paths: Sequence[str]) -> set[str] | None:
        result: set[str] = set()
        for path in paths:
            candidate = Path(path)
            if candidate.is_absolute():
                try:
                    candidate = candidate.resolve(strict=False).relative_to(
                        self.workspace
                    )
                except (OSError, ValueError):
                    return None
            text = candidate.as_posix()
            while text.startswith("./"):
                text = text[2:]
            if not text or text == ".." or text.startswith("../"):
                return None
            if len(text) >= 2 and text[1] == ":":
                return None
            result.add(text)
        return result

    def _status_changes(self) -> tuple[str, ...] | None:
        result = self._run(
            "status", "--porcelain", "-z", "--untracked-files=all"
        )
        if result.returncode != 0:
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
                paths.append(entries[index])
                index += 1
        return tuple(paths)

    def _staged_paths(self) -> tuple[str, ...] | None:
        result = self._run("diff", "--cached", "--name-only", "-z")
        if result.returncode != 0:
            return None
        return tuple(
            item
            for item in result.stdout.decode(
                "utf-8", errors="replace"
            ).split("\0")
            if item
        )

    def _head(self) -> str:
        result = self._run("rev-parse", "HEAD")
        if result.returncode != 0:
            return ""
        return result.stdout.decode("utf-8", errors="replace").strip()

    def _files_at(self, sha: str) -> tuple[str, ...]:
        result = self._run("show", "--pretty=format:", "--name-only", sha)
        if result.returncode != 0:
            return ()
        return tuple(
            line.strip()
            for line in result.stdout.decode(
                "utf-8", errors="replace"
            ).splitlines()
            if line.strip()
        )

    @staticmethod
    def _ignored(path: str) -> bool:
        parts = Path(path).parts
        if any(part in _IGNORED_PARTS for part in parts):
            return True
        return path.endswith(_IGNORED_SUFFIXES)

    @staticmethod
    def _message(objective: str, task_id: str) -> str:
        subject = " ".join(objective.split())[:72] or "complete task"
        return f"xenon: {subject}"

    def _run(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                ["git", *args],
                cwd=self.workspace,
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return subprocess.CompletedProcess(
                args=["git", *args],
                returncode=1,
                stdout=b"",
                stderr=str(exc).encode("utf-8", errors="replace"),
            )

    @staticmethod
    def _output(result: subprocess.CompletedProcess[bytes]) -> str:
        text = (result.stderr or result.stdout or b"").decode(
            "utf-8", errors="replace"
        )
        return " ".join(text.split())[:500] or "unknown git error"


__all__ = ["FinalizeOutcome", "GitFinalizer"]
