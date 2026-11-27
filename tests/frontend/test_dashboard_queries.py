"""Read-only dashboard queries: every panel answers from facts, never re-runs.

The dashboard is a view.  These tests hold that line: a query may fail, but it
must not raise into the host loop, must not invent a fact the session did not
produce, and must not write anything.
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

import xenon.kernel  # noqa: F401 — ledger<->kernel import cycle
from xenon.frontend.host import ProtocolHost
from xenon.frontend.queries import DashboardQueries
from xenon.ledger.events import EventKind, EventSource
from xenon.ledger.store import Ledger


class _StubHost:
    """The attributes the queries read, nothing else."""

    def __init__(self, cwd: Path, *, run=None, ledger=None, repl=None) -> None:
        self.cwd = cwd
        self.last_run = run
        self.last_ledger = ledger
        self.repl = repl
        self.turn = ""
        self.turn_objective = ""
        self.turn_started_at = 0.0
        self.turn_thread = None
        self._pending: dict = {}
        self._prompt_waiters: dict = {}

    def _current_models(self) -> list[str]:
        return ["vendor/model-a"]

    def _status_facts(self) -> dict:
        return {"tokens": 42, "steps": 2}


class _Run:
    def __init__(self, ledger_path: Path, task_id: str = "t-1", result=None) -> None:
        self.ledger_path = ledger_path
        self.task_id = task_id
        self.result = result


class _Obligation:
    def __init__(self) -> None:
        self.id = "ob-1"
        self.kind = "file_write"
        self.target = "a.txt"


class _Debt:
    def __init__(self) -> None:
        self.obligation = _Obligation()
        self.reason = "not verified"
        self.next_action = "write a test for a.txt"
        self.attempts = 2
        self.acceptable = True
        self.user_accepted = False


class _Ctx:
    def __init__(self, memory: dict) -> None:
        self.memory = memory

    def get_working_memory(self) -> dict:
        return dict(self.memory)


class _Repl:
    def __init__(self, memory: dict | None = None) -> None:
        self.ctx_mgr = _Ctx(memory or {})


class _Result:
    def __init__(self) -> None:
        self.accepted = True
        self.evidence_status = _Status("partial")
        self.debt = ()


class _Status:
    def __init__(self, value: str) -> None:
        self.value = value


def _ledger(tmp_path: Path) -> Path:
    path = tmp_path / "t-1.jsonl"
    ledger = Ledger.open(path, task_id="t-1")
    ledger.append(EventKind.TASK_STARTED, EventSource.RUNTIME, {"objective": "do it"})
    ledger.append(
        EventKind.PLAN_PROPOSED,
        EventSource.RUNTIME,
        {
            "plan": {
                "steps": [
                    {"id": "1", "kind": "edit", "intent": "write", "status": "closed"}
                ]
            }
        },
    )
    ledger.append(
        EventKind.TOOL_RECEIPT,
        EventSource.TOOL,
        {"tool": "fs_write", "ok": True, "paths": ["a.txt"]},
    )
    ledger.append(
        EventKind.STEP_VERDICT,
        EventSource.RUNTIME,
        {"step_id": "1", "status": "satisfied", "fact_ids": ["f-1"]},
    )
    ledger.append(
        EventKind.STEP_CLOSED,
        EventSource.RUNTIME,
        {"step_id": "1", "status": "closed"},
    )
    ledger.append(
        EventKind.CLAIM_PROPOSED,
        EventSource.RUNTIME,
        {"claim_id": "c-1", "text": "a.txt written"},
    )
    ledger.append(
        EventKind.DELIVERED,
        EventSource.RUNTIME,
        {
            "answer": "done",
            "finalize": {"commit": "abc123", "verified_paths": ["tracked.txt"]},
        },
    )
    return path


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        check=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-m", "init")
    (tmp_path / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
    (tmp_path / "fresh.txt").write_text("new\n", encoding="utf-8")
    return tmp_path


# ── snapshot ────────────────────────────────────────────────────────


def test_snapshot_carries_the_last_run_and_price_of_truth(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    host = _StubHost(tmp_path, run=_Run(ledger, result=_Result()), ledger=ledger)
    snap = DashboardQueries(host).snapshot()

    assert snap["cwd"] == str(tmp_path)
    assert snap["models"] == ["vendor/model-a"]
    assert snap["ledger"] == str(ledger)
    assert snap["evidence"] == {"status": "partial", "accepted": True}
    assert snap["status"]["tokens"] == 42
    json.dumps(snap)  # every panel value must survive the wire


def test_snapshot_without_a_run_is_empty_but_valid(tmp_path: Path) -> None:
    snap = DashboardQueries(_StubHost(tmp_path)).snapshot()
    assert snap["ledger"] is None
    assert snap["debt"] == []
    assert snap["delivery"] is None
    assert snap["approvals"] == []
    assert snap["evidence"]["status"] == "未报告"


def test_snapshot_debt_carries_next_action_and_delivery(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    result = _Result()
    result.debt = (_Debt(),)
    snap = DashboardQueries(
        _StubHost(tmp_path, run=_Run(ledger, result=result), ledger=ledger)
    ).snapshot()

    item = snap["debt"][0]
    assert item["obligation_id"] == "ob-1"
    assert item["next_action"] == "write a test for a.txt"
    assert item["attempts"] == 2
    assert snap["delivery"] == {
        "commit": "abc123",
        "verified_paths": ["tracked.txt"],
        "unverified_paths": [],
        "attributed_paths": [],
    }


# ── workspace / diff ────────────────────────────────────────────────


def test_workspace_reports_branch_and_dirty_files(repo: Path) -> None:
    status = DashboardQueries(_StubHost(repo)).workspace()
    assert status["is_git"] is True
    paths = {item["path"] for item in status["dirty"]}
    assert {"tracked.txt", "fresh.txt"} <= paths
    assert status["branch"]


def test_diff_matches_git_including_untracked_content(repo: Path) -> None:
    payload = DashboardQueries(_StubHost(repo)).diff()

    assert payload["files"], "changed files must be listed"
    assert "+two" in payload["diff"], "the tracked change must be in the patch"
    assert "new" in payload["diff"], "an untracked file's content must be shown"
    assert payload["truncated"] is False


def test_git_never_inherits_the_host_stdin(monkeypatch, tmp_path: Path) -> None:
    """The host's stdin is the protocol pipe; a git child that inherits it
    deadlocks the query (observed end-to-end: every panel timed out at 5s while
    git sat until the 8s GIT_TIMEOUT)."""

    seen: dict = {}
    real = subprocess.run

    def spy(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202 — capture-only
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr("xenon.frontend.queries.subprocess.run", spy)
    DashboardQueries(_StubHost(tmp_path))._git("rev-parse", "--is-inside-work-tree")

    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert seen["env"]["GIT_OPTIONAL_LOCKS"] == "0"


def test_diff_outside_a_repo_says_so_instead_of_raising(tmp_path: Path) -> None:
    payload = DashboardQueries(_StubHost(tmp_path / "plain")).diff()
    assert payload["files"] == []
    assert payload["baseline"] == []
    assert payload["baseline_count"] == 0
    assert payload["delivery"] is None
    assert "git" in payload["error"]


def test_diff_marks_origin_baseline_and_verification(repo: Path) -> None:
    (repo / "stray.txt").write_text("dirty before the session\n", encoding="utf-8")
    # The ledger must live outside the worktree: inside it would show up as a
    # dirty file and make the baseline assertion test the fixture.
    outside = repo.parent / f"{repo.name}-ledger"
    outside.mkdir()
    ledger = _ledger(outside)
    repl = _Repl(
        {
            "session_created_files": ["fresh.txt"],
            "session_modified_files": [str(repo / "tracked.txt")],
        }
    )
    host = _StubHost(repo, run=_Run(ledger, result=_Result()), ledger=ledger, repl=repl)
    payload = DashboardQueries(host).diff()

    files = {item["path"]: item for item in payload["files"]}
    assert files["tracked.txt"]["origin"] == "modified"
    assert files["tracked.txt"]["verification"] == "verified"
    assert files["fresh.txt"]["origin"] == "created"
    assert files["fresh.txt"]["verification"] == "attributed"
    assert files["stray.txt"]["origin"] == "baseline"
    assert files["stray.txt"]["verification"] == "unverified"
    assert [item["path"] for item in payload["baseline"]] == ["stray.txt"]
    assert payload["delivery"]["commit"] == "abc123"

    status = DashboardQueries(host).workspace()
    assert [item["path"] for item in status["baseline"]] == ["stray.txt"]
    assert status["baseline_count"] == 1
    assert status["delivery"]["verified_paths"] == ["tracked.txt"]


def test_diff_falls_back_to_the_delivery_commit_when_the_worktree_is_clean(
    tmp_path: Path,
) -> None:
    """A finalized turn commits its files; the panel must still show them.

    Observed end-to-end: after ``vcs_commit`` the Diff cell said
    ``0 个文件`` while 总览 displayed the delivery commit — the panel was
    reading ``git status`` only, so delivered work became invisible.
    """

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "init")
    (repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "deliver")
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()

    outside = tmp_path / "ledger"
    outside.mkdir()
    ledger_path = outside / "t-1.jsonl"
    ledger = Ledger.open(ledger_path, task_id="t-1")
    ledger.append(EventKind.TASK_STARTED, EventSource.RUNTIME, {"objective": "do it"})
    ledger.append(
        EventKind.DELIVERED,
        EventSource.RUNTIME,
        {
            "answer": "done",
            "finalize": {"commit": sha, "verified_paths": ["tracked.txt"]},
        },
    )

    payload = DashboardQueries(
        _StubHost(repo, run=_Run(ledger_path), ledger=ledger_path)
    ).diff()

    files = {item["path"]: item for item in payload["files"]}
    assert files["tracked.txt"]["verification"] == "verified"
    assert files["tracked.txt"]["origin"] == "modified"
    assert "+two" in payload["diff"], "the delivered patch must be shown"
    assert payload["baseline"] == []


# ── ledger / evidence / plan ────────────────────────────────────────


def test_ledger_events_are_returned_with_a_chain_verdict(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    payload = DashboardQueries(_StubHost(tmp_path, run=_Run(ledger), ledger=ledger)).ledger_events()

    kinds = [event["kind"] for event in payload["events"]]
    assert kinds[0] == "task_started"
    assert payload["chain"]["valid"] is True
    assert payload["chain"]["events"] == len(payload["events"])


def test_ledger_events_filter_and_limit(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    queries = DashboardQueries(_StubHost(tmp_path, run=_Run(ledger), ledger=ledger))

    only = queries.ledger_events(kinds=["tool_receipt"])
    assert [event["kind"] for event in only["events"]] == ["tool_receipt"]

    last_two = queries.ledger_events(limit=2)
    assert [event["kind"] for event in last_two["events"]] == [
        "claim_proposed",
        "delivered",
    ]


def test_evidence_chain_links_receipt_verdict_claim_delivery(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    payload = DashboardQueries(
        _StubHost(tmp_path, run=_Run(ledger, result=_Result()), ledger=ledger)
    ).evidence_chain()

    assert payload["facts"][0]["payload"]["tool"] == "fs_write"
    assert payload["verdicts"][0]["payload"]["status"] == "satisfied"
    assert payload["claims"][0]["payload"]["claim_id"] == "c-1"
    assert payload["delivered"]["payload"]["answer"] == "done"
    assert payload["status"] == "partial"


def test_a_resumed_ledger_supplies_evidence_without_a_live_run(
    tmp_path: Path, monkeypatch
) -> None:
    """A restored session has a ledger and a result file, but no run object.

    The terminal's ``/evidence`` shows it; the dashboard must read the same
    facts instead of reporting "未报告" just because no turn ran yet.
    """

    import xenon.repl.kernel_evidence as kernel_evidence

    ledger = _ledger(tmp_path)
    ledger.with_suffix(".result.json").write_text(
        json.dumps(
            {
                "task_id": "t-1",
                "phase": "finalized",
                "accepted": True,
                "evidence_status": "verified",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(kernel_evidence, "kernel_ledger_dir", lambda: tmp_path)
    queries = DashboardQueries(
        _StubHost(tmp_path, run=None, ledger=ledger, repl=_Repl())
    )

    chain = queries.evidence_chain()
    assert chain["status"] == "verified"
    assert chain["ledger"] == str(ledger)
    assert queries.snapshot()["evidence"] == {
        "status": "verified",
        "accepted": True,
    }


def test_plan_projection_is_read_from_the_ledger(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    payload = DashboardQueries(_StubHost(tmp_path, run=_Run(ledger), ledger=ledger)).plan()
    assert payload["plan"]["steps"]["1"]["status"] == "closed"
    assert payload["plan"]["versions"] == 1
    # This ledger delivered, so its plan was executed — not parked for approval.
    assert payload["proposed"] is False
    assert payload["accepted"] is True
    assert payload["rejected"] is False
    assert payload["current"] is None


class _SessionEvents:
    def __init__(self, events: list[dict]) -> None:
        self._events = events

    def read(self) -> list[dict]:
        return list(self._events)


def _plan_only_ledger(tmp_path: Path) -> Path:
    """The shape a plan-only kernel turn leaves: parked at PLANNED.

    ``PLAN_ACCEPTED`` here is the runtime's own marker ("accepted; waiting for
    approval"), not the operator's decision.
    """

    path = tmp_path / "t-1.jsonl"
    ledger = Ledger.open(path, task_id="t-1")
    ledger.append(EventKind.TASK_STARTED, EventSource.RUNTIME, {"objective": "add"})
    ledger.append(
        EventKind.STATE_TRANSITION,
        EventSource.RUNTIME,
        {"from": "running", "to": "planned", "reason": "plan accepted; waiting for approval"},
    )
    ledger.append(
        EventKind.PLAN_PROPOSED,
        EventSource.RUNTIME,
        {"plan": {"steps": [{"step_id": 1, "kind": "edit", "targets": ["a.py"]}]}},
    )
    ledger.append(
        EventKind.PLAN_ACCEPTED,
        EventSource.RUNTIME,
        {"plan": {"steps": [{"step_id": 1, "kind": "edit", "targets": ["a.py"]}]}},
    )
    return path


def test_an_awaiting_plan_is_not_reported_as_accepted(tmp_path: Path) -> None:
    ledger = _plan_only_ledger(tmp_path)
    repl = _Repl()
    repl._session_events = _SessionEvents([])

    payload = DashboardQueries(
        _StubHost(tmp_path, run=_Run(ledger), ledger=ledger, repl=repl)
    ).plan()

    assert payload["proposed"] is True
    assert payload["accepted"] is False
    assert payload["rejected"] is False


def test_the_operators_approval_is_the_authority(tmp_path: Path, monkeypatch) -> None:
    import xenon.repl.kernel_evidence as kernel_evidence

    ledger = _plan_only_ledger(tmp_path)
    monkeypatch.setattr(kernel_evidence, "kernel_ledger_dir", lambda: tmp_path)
    repl = _Repl()
    repl._session_events = _SessionEvents(
        [
            {
                "type": "plan/approved",
                "task_id": "t-1",
                "plan_digest": "d",
                "at": 1.0,
                "event_id": "e-1",
            }
        ]
    )

    payload = DashboardQueries(
        _StubHost(tmp_path, run=_Run(ledger), ledger=ledger, repl=repl)
    ).plan()

    assert payload["accepted"] is True
    assert payload["proposed"] is False
    assert payload["rejected"] is False


def test_a_rejected_plan_is_reported_as_rejected(tmp_path: Path, monkeypatch) -> None:
    import xenon.repl.kernel_evidence as kernel_evidence

    ledger = _plan_only_ledger(tmp_path)
    monkeypatch.setattr(kernel_evidence, "kernel_ledger_dir", lambda: tmp_path)
    repl = _Repl()
    repl._session_events = _SessionEvents(
        [{"type": "plan/rejected", "task_id": "t-1", "plan_digest": "d", "at": 2.0}]
    )

    payload = DashboardQueries(
        _StubHost(tmp_path, run=_Run(ledger), ledger=ledger, repl=repl)
    ).plan()

    assert payload["rejected"] is True
    assert payload["accepted"] is False
    assert payload["proposed"] is False


def test_a_decision_for_another_task_never_leaks(tmp_path: Path, monkeypatch) -> None:
    import xenon.repl.kernel_evidence as kernel_evidence

    ledger = _plan_only_ledger(tmp_path)
    monkeypatch.setattr(kernel_evidence, "kernel_ledger_dir", lambda: tmp_path)
    repl = _Repl()
    repl._session_events = _SessionEvents(
        [
            {
                "type": "plan/approved",
                "task_id": "other-task",
                "plan_digest": "d",
                "at": 1.0,
                "event_id": "e-1",
            }
        ]
    )

    payload = DashboardQueries(
        _StubHost(tmp_path, run=_Run(ledger), ledger=ledger, repl=repl)
    ).plan()

    assert payload["proposed"] is True
    assert payload["accepted"] is False


def test_sessions_tree_reports_the_fork_parent(tmp_path: Path, monkeypatch) -> None:
    events = tmp_path / "events"
    events.mkdir()
    (events / "root.jsonl").write_text(
        json.dumps({"type": "turn/user", "at": 1.0}) + "\n", encoding="utf-8"
    )
    (events / "fork-1.jsonl").write_text(
        json.dumps({"type": "turn/user", "at": 2.0})
        + "\n"
        + json.dumps({"type": "fork", "parent": "root", "turns": 1, "at": 3.0})
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "xenon.session.events.session_events_dir", lambda: str(events)
    )

    payload = DashboardQueries(_StubHost(tmp_path)).sessions_tree()
    nodes = {node["id"]: node for node in payload["nodes"]}
    assert nodes["fork-1"]["parent"] == "root"
    assert nodes["fork-1"]["turns"] == 1
    assert nodes["root"]["parent"] is None
    assert payload["roots"] == ["root"]


def test_queries_report_a_missing_ledger_instead_of_raising(tmp_path: Path) -> None:
    queries = DashboardQueries(_StubHost(tmp_path))
    assert "error" in queries.ledger_events()
    evidence = queries.evidence_chain()
    assert "error" in evidence
    assert evidence["projection"] == {}
    plan = queries.plan()
    assert "error" in plan
    # A panel must not have to branch on whether a ledger exists: the same
    # fields answer in both cases.
    assert plan["accepted"] is False
    assert plan["proposed"] is False
    assert plan["rejected"] is False
    assert plan["current"] is None
    assert "ledger" in plan


# ── host dispatch ───────────────────────────────────────────────────


class _Frames:
    def __init__(self) -> None:
        self.stream = io.StringIO()

    def read(self) -> list[dict]:
        return [json.loads(line) for line in self.stream.getvalue().splitlines() if line]


def test_handle_query_answers_with_a_matching_result_frame(tmp_path: Path) -> None:
    frames = _Frames()
    host = ProtocolHost(["test/fake"], out=frames.stream, cwd=tmp_path)
    host.handle_query({"type": "dashboard.snapshot", "id": "q-7"})

    frame = frames.read()[-1]
    assert frame["type"] == "dashboard.snapshot.result"
    assert frame["id"] == "q-7"
    assert frame["cwd"] == str(tmp_path)


def test_a_broken_query_becomes_an_error_field_not_a_crash(
    tmp_path: Path, monkeypatch
) -> None:
    frames = _Frames()
    host = ProtocolHost(["test/fake"], out=frames.stream, cwd=tmp_path)

    def boom(self):  # noqa: ANN001, ANN202 — deliberately exploding
        raise RuntimeError("panel exploded")

    monkeypatch.setattr(DashboardQueries, "snapshot", boom)
    host.handle_query({"type": "dashboard.snapshot", "id": "q-1"})
    frame = frames.read()[-1]
    assert frame["type"] == "dashboard.snapshot.result"
    assert "panel exploded" in frame["error"]

    # Same contract through the real class: an unreadable ledger is a field.
    host.last_ledger = tmp_path / "missing.jsonl"
    host.handle_query({"type": "ledger.events", "id": "q-2"})
    frame = frames.read()[-1]
    assert frame["type"] == "ledger.events.result"
    assert "error" in frame


def test_unknown_query_kinds_are_not_dispatchable() -> None:
    assert "not.a.query" not in ProtocolHost.QUERY_KINDS


# ── the command every front-end must agree on ──────────────────────


def test_dashboard_is_registered_for_help_and_completion() -> None:
    import xenon.repl.commands  # noqa: F401 — importing it registers every group
    from xenon.repl.command_registry import _HANDLERS, COMMANDS

    info = COMMANDS.get("/dashboard")
    assert info is not None, "/help and the completer read this catalog"
    assert "面板" in info["description"]
    # The TS front-end intercepts the command; the handler is what a
    # non-TS entry (the Python editor) prints, so it must not pretend to open
    # a browser it cannot open.
    message = _HANDLERS["/dashboard"]()
    assert "--dashboard" in message
    assert "Python" in message


def test_ledger_events_fold_bookkeeping_runs_by_default_off(tmp_path: Path) -> None:
    """#441: a real turn is mostly `state_transition`; the view hides it.

    The ledger keeps every event — this is a projection, it must say what it
    hid, and `fold=False` must return the raw rows unchanged.
    """

    path = tmp_path / "t-2.jsonl"
    ledger = Ledger.open(path, task_id="t-2")
    ledger.append(EventKind.TASK_STARTED, EventSource.RUNTIME, {"objective": "o"})
    for _ in range(5):
        ledger.append(
            EventKind.STATE_TRANSITION,
            EventSource.RUNTIME,
            {"from": "running", "to": "thinking"},
        )
    ledger.append(
        EventKind.TOOL_RECEIPT, EventSource.TOOL, {"tool": "search", "ok": True}
    )
    ledger.append(EventKind.MODEL_USAGE, EventSource.RUNTIME, {"total_tokens": 12})
    queries = DashboardQueries(_StubHost(tmp_path, run=_Run(path), ledger=path))

    raw = queries.ledger_events()
    assert len(raw["events"]) == 8
    assert raw["folded"] == 0

    folded = queries.ledger_events(fold=True)
    kinds = [event["kind"] for event in folded["events"]]
    assert kinds == ["task_started", "tool_receipt"]
    assert folded["hidden_by_kind"] == {"state_transition": 5, "model_usage": 1}
    assert folded["folded"] == 6
    assert folded["total"] == 8 and folded["shown"] == 2


def test_a_verdict_that_says_something_went_wrong_is_never_folded(
    tmp_path: Path,
) -> None:
    """Satisfied verdicts are the expected state; failures are the reason to look."""

    path = tmp_path / "t-3.jsonl"
    ledger = Ledger.open(path, task_id="t-3")
    for status in ("satisfied", "satisfied"):
        ledger.append(
            EventKind.STEP_VERDICT,
            EventSource.RUNTIME,
            {"obligation_id": "o1", "status": status, "stage": "v0"},
        )
    ledger.append(
        EventKind.STEP_VERDICT,
        EventSource.RUNTIME,
        {"obligation_id": "o2", "status": "violated", "stage": "v0", "reason": "no fact"},
    )
    queries = DashboardQueries(_StubHost(tmp_path, run=_Run(path), ledger=path))

    folded = queries.ledger_events(fold=True)

    kinds = [event["kind"] for event in folded["events"]]
    assert kinds.count("step_verdict") == 1, "验证失败的那条必须留在表里"
    shown = [e for e in folded["events"] if e["kind"] == "step_verdict"][0]
    assert shown["payload"]["status"] == "violated"
    assert folded["folded"] == 2


def test_events_group_under_the_step_that_produced_them(tmp_path: Path) -> None:
    """#441: a reader asks "what did this step do", not "what was event 47"."""

    path = tmp_path / "t-4.jsonl"
    ledger = Ledger.open(path, task_id="t-4")
    ledger.append(EventKind.TASK_STARTED, EventSource.RUNTIME, {"objective": "o"})
    ledger.append(
        EventKind.STEP_STARTED, EventSource.RUNTIME, {"step_id": "1"}, step_id="1"
    )
    ledger.append(
        EventKind.TOOL_RECEIPT, EventSource.TOOL, {"tool": "bash", "ok": True}, step_id="1"
    )
    ledger.append(
        EventKind.STEP_VERDICT,
        EventSource.RUNTIME,
        {"step_id": "1", "status": "violated", "reason": "no fact"},
        step_id="1",
    )
    ledger.append(
        EventKind.STEP_VERDICT,
        EventSource.RUNTIME,
        {"step_id": "2", "status": "satisfied"},
        step_id="2",
    )
    ledger.append(
        EventKind.TOOL_RECEIPT, EventSource.TOOL, {"tool": "read", "ok": True}, step_id="2"
    )
    queries = DashboardQueries(_StubHost(tmp_path, run=_Run(path), ledger=path))

    payload = queries.ledger_events(group="step")

    steps = {step["step_id"]: step for step in payload["steps"]}
    assert set(steps) == {"1", "2"}
    assert steps["1"]["tools"] == 1
    assert steps["1"]["status"] == "violated"
    assert steps["2"]["status"] == "satisfied"
    # The task header belongs to no step: it must not be filed under one.
    assert [event["kind"] for event in payload["loose"]] == ["task_started"]
    assert "steps" not in queries.ledger_events(), "分组是显式请求，不是默认行为"


def test_sensitive_paths_are_classified_by_reason() -> None:
    from xenon.frontend.queries import _is_sensitive_path

    workspace = r"D:\Xenon"
    assert _is_sensitive_path(r"C:\Users\x\.aws\credentials", workspace) == "credential"
    assert _is_sensitive_path(r"D:\Xenon\.env", workspace) == "credential"
    assert _is_sensitive_path(r"/etc/passwd", workspace) == "system"
    assert _is_sensitive_path(r"C:\Windows\System32\drivers\etc\hosts", workspace) == "system"
    assert _is_sensitive_path(r"D:\elsewhere\a.txt", workspace) == "outside"
    assert _is_sensitive_path(r"D:\Xenon\xenon\runtime.py", workspace) == ""


def test_a_left_zone_of_reads_is_collapsed_but_counted(tmp_path: Path) -> None:
    """#441: reads are a counter; the ones that matter are promoted instead."""

    workspace = tmp_path / "ws"
    workspace.mkdir()
    path = tmp_path / "t-5.jsonl"
    ledger = Ledger.open(path, task_id="t-5")
    ledger.append(
        EventKind.TOOL_RECEIPT,
        EventSource.TOOL,
        {
            "receipt": {
                "status": "ok",
                "declared_effects": ["fs_write"],
                "observed_effects": ["fs_write"],
                "effects": [{"type": "fs_write", "paths": [str(workspace / "out.txt")]}],
            }
        },
        step_id="1",
    )
    for name in ("a.py", "b.py"):
        ledger.append(
            EventKind.TOOL_RECEIPT,
            EventSource.TOOL,
            {
                "receipt": {
                    "status": "ok",
                    "declared_effects": ["read"],
                    "observed_effects": ["read"],
                    "effects": [{"type": "read", "path": str(workspace / name)}],
                }
            },
            step_id="1",
        )
    # Reading a credential file is the one read you want to see.
    ledger.append(
        EventKind.TOOL_RECEIPT,
        EventSource.TOOL,
        {
            "receipt": {
                "status": "ok",
                "declared_effects": ["read"],
                "observed_effects": ["read"],
                "effects": [{"type": "read", "path": str(workspace / ".env")}],
            }
        },
        step_id="1",
    )
    queries = DashboardQueries(_StubHost(tmp_path, run=_Run(path), ledger=path))

    payload = queries.ledger_events(group="step")
    step = payload["steps"][0]
    kinds = [event["kind"] for event in step["events"]]
    signals = [event.get("signal") for event in step["events"]]

    assert signals[0] in {"effect", "sensitive"}, "有副作用/敏感的事件排在最前"
    assert "reads" in kinds, "两次普通读取必须折成一行"
    reads = step["events"][kinds.index("reads")]
    assert reads["count"] == 2
    assert reads["file_count"] == 2
    # 敏感读没有被折进计数行里
    sensitive_rows = [
        event
        for event in step["events"]
        if event.get("signal") == "sensitive" and event["kind"] == "tool_receipt"
    ]
    assert sensitive_rows, ".env 的读取必须单独出现"
    assert step["status"] in {"", "satisfied"}
