"""Deterministic finalize only commits task-owned, verified, staged paths."""

from __future__ import annotations

import subprocess
from pathlib import Path

from xenon.engine.context import AgentContext
from xenon.engine.task_runtime import finalize_verified_work, run_with_delivery
from xenon.engine.task_state import TaskState


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    assert _git(repo, "commit", "-q", "-m", "base").returncode == 0
    return repo


def _verified_state(repo: Path, **updates) -> TaskState:
    (repo / "mathx.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "test_mathx.py").write_text(
        "from mathx import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    _git(repo, "add", "mathx.py", "test_mathx.py")
    state = TaskState(
        objective="add verified source",
        intent="write_code",
        execution_level=3,
        required=["verify", "commit"],
        artifacts=["mathx.py"],
        test_artifacts=["test_mathx.py"],
        tests=["run_tests pytest test_mathx.py"],
        artifacts_revision=1,
        verified_revision=1,
        dirty_staged_paths=["mathx.py", "test_mathx.py"],
        dirty_unstaged_paths=[],
    )
    for key, value in updates.items():
        setattr(state, key, value)
    return state


def test_finalize_commits_verified_staged_source_and_cleans_tree(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XENON_AUTO_FINALIZE", "1")
    repo = _repo(tmp_path)
    state = _verified_state(repo)
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    result = finalize_verified_work(state, repo)

    assert result["status"] == "committed"
    assert result["commit"] and result["commit"] != before
    assert state.commit == result["commit"]
    assert state.missing_delivery() == []
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""
    message = _git(repo, "log", "-1", "--pretty=%B").stdout
    assert "finalize verified changes" in message
    assert "Co-authored-by: Xenon" in message


def test_finalize_never_commits_unrelated_staged_paths(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XENON_AUTO_FINALIZE", "1")
    repo = _repo(tmp_path)
    (repo / "unrelated.txt").write_text("user change\n", encoding="utf-8")
    _git(repo, "add", "unrelated.txt")
    state = _verified_state(repo)
    state.dirty_staged_paths.append("unrelated.txt")
    before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    result = finalize_verified_work(state, repo)

    assert result["status"] == "blocked"
    assert "unrelated" in result["reason"]
    assert state.commit == ""
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == before
    assert "unrelated.txt" in _git(repo, "status", "--porcelain").stdout


def test_finalize_blocks_when_verified_paths_are_unstaged(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    state = _verified_state(repo)
    state.dirty_unstaged_paths = ["mathx.py"]

    result = finalize_verified_work(state, repo)

    assert result["status"] == "blocked"
    assert "unstaged" in result["reason"]


def test_finalize_blocks_stale_verification(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    state = _verified_state(repo)
    state.artifacts_revision = 2
    state.verified_revision = 1

    result = finalize_verified_work(state, repo)

    assert result["status"] == "blocked"
    assert "stale" in result["reason"]


def test_finalize_opt_out_does_not_commit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XENON_AUTO_FINALIZE", "0")
    repo = _repo(tmp_path)
    state = _verified_state(repo)

    result = finalize_verified_work(state, repo)

    assert result["status"] == "disabled"
    assert state.commit == ""


class _FakeEngine:
    def __init__(self) -> None:
        self._last_tracker = None
        self._last_iterations = 1
        self._last_termination_reason = None

    def run(self, user_input, context=None, ctx_mgr=None):
        return "done"


def test_run_with_delivery_finalizes_without_extra_model_turn(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XENON_AUTO_FINALIZE", "1")
    repo = _repo(tmp_path)
    state = _verified_state(repo)
    context = AgentContext(
        {
            "_task_state": state.to_dict(),
            "_workspace_root": str(repo),
        }
    )
    engine = _FakeEngine()

    answer = run_with_delivery(engine, "finish", context, max_delivery_retries=0)

    assert str(answer) == "done"
    # run_with_delivery owns a deserialized copy when no live ledger is
    # attached; the durable context snapshot is the source of truth here.
    completed = TaskState.from_context(context)
    assert completed is not None
    assert completed.status == "completed"
    assert completed.missing_delivery() == []
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""
    assert context.get("_task_state")["status"] == "completed"


def test_run_with_delivery_projects_finalize_commit_when_ledger_exists(
    tmp_path: Path, monkeypatch
) -> None:
    from xenon.engine.evidence_runtime import (
        EventKind,
        EvidenceLedger,
        EvidenceSource,
        LifecyclePhase,
    )

    monkeypatch.setenv("XENON_AUTO_FINALIZE", "1")
    repo = _repo(tmp_path)
    state = _verified_state(repo)
    ledger = EvidenceLedger("finalize-projection")
    ledger.append(
        LifecyclePhase.TASK,
        EventKind.TASK_FACT,
        EvidenceSource.ENGINE,
        {"task_id": state.task_id},
    )
    context = AgentContext(
        {
            "_task_state": state.to_dict(),
            "_workspace_root": str(repo),
            "_evidence_ledger": ledger,
        }
    )

    answer = run_with_delivery(
        _FakeEngine(), "finish", context, max_delivery_retries=0
    )

    completed = TaskState.from_context(context)
    assert str(answer) == "done"
    assert completed is not None
    assert completed.status == "completed"
    assert completed.commit
    assert completed.missing_delivery() == []
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""
    assert any(
        event.kind == EventKind.DELIVERY.value
        and event.payload.get("commit") == completed.commit
        for event in ledger.events
    )
