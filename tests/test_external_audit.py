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
    assert result.contradicted_paths == ()
    assert result.unverifiable_paths
    assert result.evidence_status == "unverified"
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

    # Xenon cannot observe it, so it can neither confirm nor refute the claim.
    assert result.contradicted_paths == ()
    assert result.unverifiable_paths == (outside,)
    assert result.evidence_status == "unverified"


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


# ── adapter selection (§11.4) ────────────────────────────────────────


def test_detection_names_the_adapter_it_used(tmp_path: Path) -> None:
    transcript = _write_transcript(
        tmp_path / "session.jsonl",
        [_tool_use("Edit", {"file_path": str(tmp_path / "calc.py")})],
    )

    result = audit(transcript, workspace=tmp_path, probe=FakeProbe(dirty=("calc.py",)))

    assert result.agent == "claude-code"
    assert result.claimed_changes


def test_an_unreadable_format_yields_no_claims_and_says_so(tmp_path: Path) -> None:
    # Xenon's own ledger shape: JSONL, but not a transcript any adapter reads.
    transcript = _write_transcript(
        tmp_path / "ledger.jsonl",
        [
            {"kind": "task_started", "seq": 1, "payload": {"objective": "x"}},
            {"kind": "finalized", "seq": 2, "payload": {"evidence_status": "verified"}},
        ],
    )

    result = audit(transcript, workspace=tmp_path, probe=FakeProbe(dirty=("calc.py",)))

    assert result.claimed_changes == ()
    assert result.claimed_commands == ()
    assert result.agent == "unknown"
    assert any("无法识别" in note for note in result.notes)
    # No claim was parsed, so nothing may be presented as "unclaimed" either.
    assert result.unclaimed_paths == ()
    assert result.findings == ()
    assert result.evidence_status == "unverified"


def test_an_explicit_unknown_agent_does_not_fall_back_to_sniffing(tmp_path: Path) -> None:
    transcript = _write_transcript(
        tmp_path / "session.jsonl",
        [_tool_use("Edit", {"file_path": str(tmp_path / "calc.py")})],
    )

    result = audit(
        transcript,
        workspace=tmp_path,
        probe=FakeProbe(dirty=("calc.py",)),
        agent="some-other-agent",
    )

    # The caller asked for a specific parser; using another one would attribute
    # claims to the wrong agent.
    assert result.agent == "some-other-agent"
    assert result.claimed_changes == ()
    assert any("无法识别" in note for note in result.notes)


def test_an_explicit_agent_reads_a_transcript_sniffing_would_skip(tmp_path: Path) -> None:
    # The tool call is there, but the record shape sniffing looks for is not:
    # explicit selection must still parse it.
    transcript = _write_transcript(
        tmp_path / "weekend.jsonl",
        [
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "Edit",
                                         "input": {"file_path": "calc.py"}}]},
            }
        ],
    )

    result = audit(
        transcript,
        workspace=tmp_path,
        probe=FakeProbe(dirty=("calc.py",)),
        agent="claude-code",
    )

    assert result.agent == "claude-code"
    assert [item.path for item in result.claimed_changes] == ["calc.py"]


def _cli_args(**overrides):
    from argparse import Namespace

    base = {
        "json": False,
        "comment": False,
        "out": None,
        "strict": False,
        "base": None,
        "agent": "",
        "workspace": str(Path.cwd()),
    }
    base.update(overrides)
    return Namespace(**base)


def test_the_audit_cli_writes_artifacts(tmp_path: Path) -> None:
    from xenon.main import _cmd_audit_external

    transcript = tmp_path / "session.jsonl"
    transcript.write_text("", encoding="utf-8")
    out = tmp_path / "out"

    code = _cmd_audit_external(
        str(transcript), _cli_args(json=True, out=str(out))
    )

    assert code == 0
    artifact = out / "session.audit.json"
    assert artifact.is_file()
    report = json.loads(artifact.read_text(encoding="utf-8"))
    assert report["status"]["evidence_status"] in {
        "verified",
        "partial",
        "unverified",
        "contradicted",
    }


def test_the_audit_cli_strict_fails_only_on_a_contradiction(
    tmp_path: Path,
) -> None:
    from xenon.main import _cmd_audit_external

    transcript = tmp_path / "session.jsonl"
    ghost = Path.cwd() / "ghost-that-was-never-written.py"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Write",
                            "input": {"file_path": str(ghost), "content": "x"},
                        }
                    ]
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"

    lenient = _cmd_audit_external(
        str(transcript),
        _cli_args(agent="claude-code", base="HEAD", json=True, out=str(out)),
    )
    strict = _cmd_audit_external(
        str(transcript),
        _cli_args(
            agent="claude-code",
            base="HEAD",
            json=True,
            out=str(out),
            strict=True,
        ),
    )

    report = json.loads(
        (out / "session.audit.json").read_text(encoding="utf-8")
    )
    assert report["status"]["evidence_status"] == "contradicted"
    assert lenient == 0
    assert strict == 1


def test_unverifiable_claims_are_not_reported_as_contradictions(tmp_path: Path) -> None:
    """Mixed audit: a bound in-workspace claim plus one outside the workspace."""

    outside = str(tmp_path.parent / "elsewhere.py")
    transcript = _write_transcript(
        tmp_path / "session.jsonl",
        [
            _tool_use("Edit", {"file_path": str(tmp_path / "calc.py")}),
            _tool_use("Edit", {"file_path": outside}),
        ],
    )

    result = audit(
        transcript, workspace=tmp_path, probe=FakeProbe(dirty=("calc.py",))
    )
    report = to_report(result)

    assert result.evidence_status == "partial"
    assert result.contradicted_paths == ()
    assert result.unverifiable_paths == (outside,)
    statuses = {claim["target"]: claim["status"] for claim in report["claims"]}
    assert statuses["calc.py"] == "bound"
    assert statuses[outside] == "out_of_scope"
    assert report["contradictions"] == []
    assert [item["claim"]["target"] for item in report["out_of_scope"]] == [outside]
    assert report["delivery"]["out_of_scope_paths"] == [outside]

    rendered = render_audit(result)
    assert "## 无法核验（1）" in rendered
    assert "## 与工作区冲突" not in rendered


def test_the_audit_cli_strict_allows_unverifiable_claims(tmp_path: Path) -> None:
    """``--strict`` fails on refuted claims, not on ones it could not check."""

    from xenon.main import _cmd_audit_external

    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Write",
                            "input": {
                                "file_path": str(tmp_path / "outside.py"),
                                "content": "x",
                            },
                        }
                    ]
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"

    code = _cmd_audit_external(
        str(transcript),
        _cli_args(
            agent="claude-code",
            base="HEAD",
            json=True,
            out=str(out),
            strict=True,
        ),
    )

    report = json.loads((out / "session.audit.json").read_text(encoding="utf-8"))
    assert report["status"]["evidence_status"] == "unverified"
    assert report["out_of_scope"]
    assert code == 0
