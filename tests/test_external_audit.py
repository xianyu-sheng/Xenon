"""Another agent's log may only make claims; the worktree decides the facts.

The temptation this guards against is real and specific: a transcript contains
tool results, including test output that says "12 passed".  Reading those as
evidence would let the party under review certify itself, which is the one
thing Xenon exists to prevent.  These tests pin the split.
"""

from __future__ import annotations

import json
from pathlib import Path

from xenon.external import (
    ClaimedChange,
    ExternalAudit,
    Finding,
    audit,
    read_claude_code_claims,
    render_audit,
    to_report,
)
from xenon.kernel.contracts import PropositionKind, WorkspaceProbe


class FakeProbe:
    """Stands in for GitWorktreeProbe; no git involved."""

    def __init__(
        self,
        *,
        dirty: tuple[str, ...] = (),
        since: dict[str, tuple[str, ...]] | None = None,
        head: str = "head123",
        base: str = "",
    ) -> None:
        self.dirty = dirty
        self.since = since or {}
        self._head = head
        self.base = base

    def probe(self, *, verified_paths: tuple[str, ...] = ()) -> WorkspaceProbe:
        return WorkspaceProbe(
            dirty_paths=self.dirty,
            verified_paths=tuple(verified_paths),
            head=self._head,
        )

    def changed_since(self, base: str) -> tuple[str, ...] | None:
        return self.since.get(base, self.dirty)

    def commit_before(self, when: float) -> str:
        return self.base

    def head(self) -> str:
        return self._head


def _write_transcript(path: Path, events: list[dict]) -> Path:
    path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )
    return path


def _tool_use(name: str, payload: dict, identifier: str = "t1") -> dict:
    return {
        "type": "assistant",
        "timestamp": "2026-11-06T10:00:00.000Z",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": identifier, "name": name, "input": payload}
            ],
        },
    }


def test_a_claim_matches_the_worktree(tmp_path: Path) -> None:
    transcript = _write_transcript(
        tmp_path / "session.jsonl",
        [_tool_use("Edit", {"file_path": str(tmp_path / "calc.py")})],
    )
    probe = FakeProbe(dirty=("calc.py",))

    result = audit(transcript, workspace=tmp_path, probe=probe)

    assert [item.path for item in result.claimed_changes] == ["calc.py"]
    assert result.findings == (Finding("calc.py", True, "工作区确有改动（calc.py）"),)
    assert result.evidence_status == "verified"


def test_a_claim_the_worktree_does_not_show_is_contradicted(tmp_path: Path) -> None:
    """The core case: the log says the file was edited, the disk disagrees."""

    transcript = _write_transcript(
        tmp_path / "session.jsonl",
        [_tool_use("Write", {"file_path": str(tmp_path / "ghost.py")})],
    )

    result = audit(transcript, workspace=tmp_path, probe=FakeProbe(dirty=()))

    assert result.contradicted_paths == ("ghost.py",)
    assert result.evidence_status == "contradicted"


def test_a_tool_result_saying_tests_passed_is_not_evidence(tmp_path: Path) -> None:
    """A transcript can claim anything.  Nothing in it becomes a fact."""

    transcript = _write_transcript(
        tmp_path / "session.jsonl",
        [
            _tool_use("Bash", {"command": "python -m pytest -q"}),
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": "12 passed in 0.4s",
                            "is_error": False,
                        }
                    ],
                },
            },
        ],
    )

    result = audit(transcript, workspace=tmp_path, probe=FakeProbe(dirty=()))

    # The command is recorded as a claim; it never becomes a test fact, so the
    # run cannot be "verified" on the strength of its own output.
    assert [item.command for item in result.claimed_commands] == ["python -m pytest -q"]
    assert result.evidence_status == "unverified"
    assert to_report(result)["tools"]["tests"] == []


def test_work_committed_by_the_session_counts_via_the_baseline(tmp_path: Path) -> None:
    """A clean worktree is not the same as "nothing was done"."""

    transcript = _write_transcript(
        tmp_path / "session.jsonl",
        [_tool_use("Edit", {"file_path": str(tmp_path / "calc.py")})],
    )
    probe = FakeProbe(dirty=(), since={"abc1234": ("calc.py",)}, base="abc1234")

    result = audit(transcript, workspace=tmp_path, probe=probe)

    assert result.base == "abc1234"
    assert result.verified_paths == ("calc.py",)
    assert result.evidence_status == "verified"


def test_an_unclaimed_change_is_reported_but_not_called_a_lie(tmp_path: Path) -> None:
    transcript = _write_transcript(
        tmp_path / "session.jsonl",
        [_tool_use("Edit", {"file_path": str(tmp_path / "calc.py")})],
    )
    probe = FakeProbe(dirty=("calc.py", "generated.py"))

    result = audit(transcript, workspace=tmp_path, probe=probe)

    assert result.unclaimed_paths == ("generated.py",)
    # A formatter or a shell command can dirty a file; the report states it and
    # stops there.  It is a gap, not a contradiction.
    assert result.evidence_status == "partial"


def test_an_unobservable_workspace_proves_nothing(tmp_path: Path) -> None:
    transcript = _write_transcript(
        tmp_path / "session.jsonl",
        [_tool_use("Edit", {"file_path": str(tmp_path / "calc.py")})],
    )

    class BlindProbe:
        def probe(self, *, verified_paths: tuple[str, ...] = ()):
            return None

        def changed_since(self, base: str):
            return None

        def commit_before(self, when: float) -> str:
            return ""

        def head(self) -> str:
            return ""

    result = audit(transcript, workspace=tmp_path, probe=BlindProbe())

    assert result.verified_paths == ()
    assert result.evidence_status == "contradicted"
    assert result.notes


def test_multiple_edits_to_one_file_are_one_claim(tmp_path: Path) -> None:
    transcript = _write_transcript(
        tmp_path / "session.jsonl",
        [
            _tool_use("Edit", {"file_path": str(tmp_path / "calc.py")}, "t1"),
            _tool_use("Edit", {"file_path": str(tmp_path / "calc.py")}, "t2"),
        ],
    )

    claims, _ = read_claude_code_claims(transcript, workspace=tmp_path)

    assert len(claims) == 1
    assert claims[0].calls == 2
    assert claims[0].kind is PropositionKind.FILE_CHANGED


def test_a_write_then_edit_is_a_changed_file_not_a_created_one(tmp_path: Path) -> None:
    transcript = _write_transcript(
        tmp_path / "session.jsonl",
        [
            _tool_use("Write", {"file_path": str(tmp_path / "new.py")}, "t1"),
            _tool_use("Edit", {"file_path": str(tmp_path / "new.py")}, "t2"),
        ],
    )

    claims, _ = read_claude_code_claims(transcript, workspace=tmp_path)

    assert claims[0].kind is PropositionKind.FILE_CHANGED


def test_a_truncated_transcript_does_not_abort_the_audit(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text(
        json.dumps(_tool_use("Edit", {"file_path": str(tmp_path / "calc.py")})) + "\n"
        + '{"type": "assistant", "message": {"content": [',
        encoding="utf-8",
    )

    claims, _ = read_claude_code_claims(path, workspace=tmp_path)

    assert [item.path for item in claims] == ["calc.py"]


def test_a_path_outside_the_workspace_is_reported_as_written(tmp_path: Path) -> None:
    outside = str(tmp_path.parent / "elsewhere.py")
    transcript = _write_transcript(
        tmp_path / "session.jsonl",
        [_tool_use("Edit", {"file_path": outside})],
    )

    result = audit(transcript, workspace=tmp_path, probe=FakeProbe(dirty=()))

    # Xenon cannot observe it, so it cannot vouch for it.
    assert result.contradicted_paths


def test_the_report_never_claims_a_verified_answer(tmp_path: Path) -> None:
    transcript = _write_transcript(
        tmp_path / "session.jsonl",
        [_tool_use("Edit", {"file_path": str(tmp_path / "calc.py")})],
    )
    result = audit(transcript, workspace=tmp_path, probe=FakeProbe(dirty=("calc.py",)))

    report = to_report(result)

    # "Files changed" was proven.  "The change is correct" was not, so the
    # answer is never marked verified on the strength of a dirty file.
    assert report["status"]["evidence_status"] == "verified"
    assert report["limits"]["note"]


def test_render_says_claim_and_observation_not_receipt(tmp_path: Path) -> None:
    result = ExternalAudit(
        agent="claude-code",
        workspace=str(tmp_path),
        session="abc123",
        claimed_changes=(ClaimedChange("a.py", PropositionKind.FILE_CHANGED, ("Edit",), 1),),
        findings=(Finding("a.py", True, "工作区确有改动"),),
        dirty_paths=("a.py",),
        base="deadbeef",
    )

    text = render_audit(result)

    assert "External Audit" in text
    assert "日志声明" in text
    assert "工作区实测" in text
    assert "receipt" not in text.lower()
