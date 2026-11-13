"""Deterministic git finalizer tests (#201)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from xenon.catalog.finalizer import GitFinalizer


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


def _repo(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def test_finalizer_stages_verified_path_and_commits(tmp_path: Path) -> None:
    _repo(tmp_path)
    (tmp_path / "a.py").write_text("print('a')\n", encoding="utf-8")
    outcome = GitFinalizer(tmp_path).finalize(
        verified_paths=("a.py",),
        objective="add a.py",
        task_id="t1",
    )

    assert outcome.accepted is True
    assert outcome.created is True
    assert outcome.commit_sha
    assert outcome.files == ("a.py",)
    assert _head(tmp_path) == outcome.commit_sha
    assert _git(tmp_path, "status", "--porcelain").stdout == ""


def test_finalizer_rejects_unverified_worktree_path(tmp_path: Path) -> None:
    _repo(tmp_path)
    (tmp_path / "a.py").write_text("print('a')\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("print('b')\n", encoding="utf-8")
    outcome = GitFinalizer(tmp_path).finalize(
        verified_paths=("a.py",),
        objective="add a.py",
        task_id="t1",
    )

    assert outcome.accepted is False
    assert "unverified" in outcome.reason
    assert "b.py" in outcome.reason
    assert _git(tmp_path, "status", "--porcelain").stdout


def test_finalizer_does_not_blame_pre_existing_dirt(tmp_path: Path) -> None:
    """A repo with local WIP must still be able to commit this turn's work.

    Measured refactor run: the worktree already contained unrelated edits
    before the task started, so the finalizer refused every delivery with
    "worktree contains unverified paths: CHANGELOG.md, docs/…".  Only staging
    verified paths keeps the pre-existing work safe, so it must not veto.
    """

    _repo(tmp_path)
    (tmp_path / "wip.py").write_text("print('wip')\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("print('a')\n", encoding="utf-8")

    outcome = GitFinalizer(tmp_path).finalize(
        verified_paths=("a.py",),
        objective="add a.py",
        task_id="t1",
        baseline_paths=("wip.py",),
    )

    assert outcome.accepted is True, outcome.reason
    assert outcome.files == ("a.py",)
    assert "wip.py" in _git(tmp_path, "status", "--porcelain").stdout


def test_finalizer_still_rejects_dirt_outside_the_baseline(
    tmp_path: Path,
) -> None:
    _repo(tmp_path)
    (tmp_path / "wip.py").write_text("print('wip')\n", encoding="utf-8")
    (tmp_path / "sneaky.py").write_text("print('x')\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("print('a')\n", encoding="utf-8")

    outcome = GitFinalizer(tmp_path).finalize(
        verified_paths=("a.py",),
        objective="add a.py",
        task_id="t1",
        baseline_paths=("wip.py",),
    )

    assert outcome.accepted is False
    assert "sneaky.py" in outcome.reason


def test_finalizer_excuses_a_file_the_turn_created(tmp_path: Path) -> None:
    """A script's output is this turn's work, not unexplained residue.

    Measured data-cleaning run: the model wrote and ran a script, the script
    wrote ``sales_clean.csv`` — the exact deliverable — but the finalizer
    refused with "worktree contains unverified paths: sales_clean.csv" and
    the turn ended "delivered with gaps".  The probe had already named the
    file as created during the turn.
    """

    _repo(tmp_path)
    (tmp_path / "clean.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "out.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    outcome = GitFinalizer(tmp_path).finalize(
        verified_paths=("clean.py",),
        objective="clean data",
        task_id="t1",
        attributed_paths=("out.csv",),
    )

    assert outcome.accepted is True, outcome.reason
    assert outcome.files == ("clean.py",), "an attributed output is not staged"
    assert "out.csv" in _git(tmp_path, "status", "--porcelain").stdout


def test_finalizer_accepts_existing_commit_without_new_commit(
    tmp_path: Path,
) -> None:
    _repo(tmp_path)
    (tmp_path / "a.py").write_text("print('a')\n", encoding="utf-8")
    _git(tmp_path, "add", "a.py")
    _git(tmp_path, "commit", "-m", "initial")
    sha = _head(tmp_path)

    outcome = GitFinalizer(tmp_path).finalize(
        verified_paths=("a.py",),
        objective="no-op",
        task_id="t1",
        existing_commit_sha=sha,
    )

    assert outcome.accepted is True
    assert outcome.created is False
    assert outcome.commit_sha == sha
    assert outcome.files == ("a.py",)


def test_finalizer_ignores_generated_cache_paths(tmp_path: Path) -> None:
    _repo(tmp_path)
    (tmp_path / "a.py").write_text("print('a')\n", encoding="utf-8")
    cache = tmp_path / ".pytest_cache" / "v" / "cache"
    cache.parent.mkdir(parents=True)
    cache.write_text("cache\n", encoding="utf-8")

    outcome = GitFinalizer(tmp_path).finalize(
        verified_paths=("a.py",),
        objective="add a.py",
        task_id="t1",
    )

    assert outcome.accepted is True
    assert outcome.files == ("a.py",)


def test_finalizer_rejects_no_changes_without_commit(tmp_path: Path) -> None:
    _repo(tmp_path)

    outcome = GitFinalizer(tmp_path).finalize(
        verified_paths=(),
        objective="no-op",
        task_id="t1",
    )

    assert outcome.accepted is True
    assert outcome.commit_sha is None
    assert outcome.created is False


def test_finalizer_rolls_back_a_commit_when_hook_dirts_worktree(
    tmp_path: Path,
) -> None:
    _repo(tmp_path)
    (tmp_path / "a.py").write_text("print('a')" + chr(10), encoding="utf-8")

    def runner(args, **kwargs):
        result = subprocess.run(args, **kwargs)
        if args[:2] == ["git", "commit"] and result.returncode == 0:
            (tmp_path / "a.py").write_text("hacked" + chr(10), encoding="utf-8")
        return result

    outcome = GitFinalizer(tmp_path, runner=runner).finalize(
        verified_paths=("a.py",),
        objective="add a.py",
        task_id="t1",
    )

    assert outcome.accepted is False
    assert outcome.rolled_back is True
    assert "dirty" in outcome.reason or "changed" in outcome.reason
    assert subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=tmp_path,
        capture_output=True,
    ).returncode != 0


def test_finalizer_rolls_back_to_the_previous_commit(tmp_path: Path) -> None:
    _repo(tmp_path)
    (tmp_path / "a.py").write_text("base" + chr(10), encoding="utf-8")
    _git(tmp_path, "add", "a.py")
    _git(tmp_path, "commit", "-m", "base")
    base = _head(tmp_path)
    (tmp_path / "a.py").write_text("changed" + chr(10), encoding="utf-8")

    def runner(args, **kwargs):
        result = subprocess.run(args, **kwargs)
        if args[:2] == ["git", "commit"] and result.returncode == 0:
            (tmp_path / "a.py").write_text("hook" + chr(10), encoding="utf-8")
        return result

    outcome = GitFinalizer(tmp_path, runner=runner).finalize(
        verified_paths=("a.py",),
        objective="change a.py",
        task_id="t1",
        existing_commit_sha=base,
    )

    assert outcome.accepted is False
    assert outcome.rolled_back is True
    assert _head(tmp_path) == base


def test_public_rollback_undoes_a_finalizer_commit(tmp_path: Path) -> None:
    _repo(tmp_path)
    (tmp_path / "a.py").write_text("base" + chr(10), encoding="utf-8")
    _git(tmp_path, "add", "a.py")
    _git(tmp_path, "commit", "-m", "base")
    base = _head(tmp_path)
    (tmp_path / "a.py").write_text("changed" + chr(10), encoding="utf-8")
    outcome = GitFinalizer(tmp_path).finalize(
        verified_paths=("a.py",),
        objective="change a.py",
        task_id="t1",
        existing_commit_sha=base,
    )
    assert outcome.accepted is True
    assert outcome.created is True

    ok, reason = GitFinalizer(tmp_path).rollback(outcome.commit_sha or "")

    assert ok is True
    assert reason == ""
    assert _head(tmp_path) == base
