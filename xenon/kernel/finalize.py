"""Deterministic delivery/finalize guard (#201, first slice).

It does not create a commit.  It checks the commit already produced by the
gateway: every committed path must be covered by a changed ``FsWrite`` /
``FsDelete`` fact from this run, and committed file hashes must still match the
verified hashes.  A push is rejected unless the task explicitly allows it.

A full finalizer that stages and commits only verified source still belongs to
the delivery layer; this guard is the fail-closed check before ``Delivered``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from xenon.kernel.contracts import (
    FsDelete,
    FsWrite,
    VcsCommit,
    VcsPush,
)
from xenon.kernel.controller import index_facts


@dataclass(frozen=True, slots=True)
class FinalizeDecision:
    accepted: bool
    reason: str = ""
    commit_sha: str | None = None
    verified_paths: tuple[str, ...] = field(default_factory=tuple)
    unverified_paths: tuple[str, ...] = field(default_factory=tuple)
    hash_mismatches: tuple[str, ...] = field(default_factory=tuple)


def _normalize(path: str, workspace: Path) -> str:
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            candidate = candidate.resolve(strict=False).relative_to(
                workspace.resolve()
            )
        except (OSError, ValueError):
            return candidate.as_posix()
    text = candidate.as_posix()
    while text.startswith("./"):
        text = text[2:]
    return text


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _within_workspace(normalized: str) -> bool:
    if not normalized or normalized in {".", ".."}:
        return False
    if normalized.startswith("../") or normalized.startswith("/"):
        return False
    if len(normalized) >= 2 and normalized[1] == ":":
        return False
    return True


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


class FinalizeGuard:
    """Check that a delivered commit contains only verified workspace facts."""

    def check(
        self,
        ledger: Any,
        *,
        since_seq: int = 0,
        workspace: str | Path = ".",
        allow_push: bool = False,
    ) -> FinalizeDecision:
        root = Path(workspace).resolve()
        writes: dict[str, str] = {}
        deletes: set[str] = set()
        commits: list[VcsCommit] = []
        pushes: list[VcsPush] = []

        for fact in index_facts(ledger):
            if fact.seq <= since_seq:
                continue
            effect = fact.effect
            if isinstance(effect, FsWrite) and effect.changed:
                for path in effect.paths:
                    normalized = _normalize(path, root)
                    if not _within_workspace(normalized):
                        continue
                    writes[normalized] = str(
                        effect.hashes.get(path)
                        or effect.hashes.get(normalized)
                        or ""
                    )
            elif isinstance(effect, FsDelete) and effect.changed:
                for path in effect.paths:
                    normalized = _normalize(path, root)
                    if _within_workspace(normalized):
                        deletes.add(normalized)
            elif isinstance(effect, VcsCommit):
                commits.append(effect)
            elif isinstance(effect, VcsPush):
                pushes.append(effect)

        if pushes and not allow_push:
            return FinalizeDecision(
                accepted=False,
                reason="push is not allowed for this task",
            )

        if not commits:
            return FinalizeDecision(
                accepted=True,
                verified_paths=tuple(sorted(set(writes) | deletes)),
            )

        commit = commits[-1]
        if not commit.files:
            return FinalizeDecision(
                accepted=False,
                reason="commit has no file manifest; cannot verify staged source",
                commit_sha=commit.sha or None,
            )

        verified = set(writes) | deletes
        commit_paths = {
            _normalize(path, root) for path in commit.files
        }
        unverified = tuple(
            sorted(
                path
                for path in commit_paths
                if not _within_workspace(path) or path not in verified
            )
        )
        if unverified:
            return FinalizeDecision(
                accepted=False,
                reason="commit contains paths without verified write/delete facts",
                commit_sha=commit.sha or None,
                verified_paths=tuple(sorted(verified)),
                unverified_paths=unverified,
            )

        mismatches: list[str] = []
        for path in sorted(commit_paths):
            expected = writes.get(path)
            if (
                not expected
                or path in deletes
                or not _SHA256_RE.match(expected)
            ):
                # A fake/legacy fact may carry a non-content hash.  It can
                # still prove the path, but content verification is only
                # possible for a real SHA-256.
                continue
            actual = _sha256(root / path)
            if actual != expected:
                mismatches.append(path)
        if mismatches:
            return FinalizeDecision(
                accepted=False,
                reason="committed files changed after verification",
                commit_sha=commit.sha or None,
                verified_paths=tuple(sorted(verified)),
                hash_mismatches=tuple(mismatches),
            )

        return FinalizeDecision(
            accepted=True,
            commit_sha=commit.sha or None,
            verified_paths=tuple(sorted(verified)),
        )


__all__ = ["FinalizeDecision", "FinalizeGuard"]
