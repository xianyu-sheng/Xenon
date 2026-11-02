"""Child runtimes run the same loop in an isolated ledger (#204 minimal)."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from xenon.harness.child import (
    CHILD_PROFILES,
    MAX_CHILD_DEPTH,
    child_evidence,
    run_child_task,
    run_child_tasks,
)
from xenon.harness.llm import ModelConfig
from xenon.kernel.policies import PermissionMode


def _worktree(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    return ws


class FakeClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    def __call__(self, model_id, messages, **kwargs):
        return self.responses.pop(0)


def _edit_then_finish(path: str = "calc.py") -> FakeClient:
    return FakeClient(
        [
            json.dumps(
                {
                    "action": "call_tool",
                    "capability": "edit",
                    "params": {
                        "files": [
                            {
                                "path": path,
                                "content": "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n",
                            }
                        ]
                    },
                }
            ),
            json.dumps(
                {
                    "action": "finish",
                    "answer": "added sub",
                    "claims": [
                        {"id": "c1", "kind": "file_changed", "target": path}
                    ],
                }
            ),
        ]
    )


def test_child_profiles_are_a_subset_without_review_loops() -> None:
    assert "kernel" in CHILD_PROFILES
    assert "react" in CHILD_PROFILES
    assert all(not profile.plan_review for profile in CHILD_PROFILES.values())
    assert MAX_CHILD_DEPTH == 1


def test_child_runs_in_its_own_ledger_and_never_commits(tmp_path: Path) -> None:
    ledger_dir = tmp_path / "children"

    outcome = run_child_task(
        objective="add sub() to calc.py",
        workspace=_worktree(tmp_path),
        model=ModelConfig(model_id="test/fake"),
        profile="react",
        parent_task_id="parent-1",
        ledger_dir=ledger_dir,
        permission=PermissionMode.AUTO_EDIT,
        client=_edit_then_finish(),
    )

    assert outcome.accepted is True
    assert outcome.phase == "finalized"
    assert outcome.delivered is True
    assert outcome.debt == ()
    assert outcome.tool_calls == 1
    assert outcome.child_id.startswith("parent-1-sub1-")
    ledger = Path(outcome.ledger_path)
    assert ledger.parent == ledger_dir
    assert ledger.exists(), "子任务必须留下自己的账本"
    assert "added sub" in outcome.answer


def test_child_does_not_share_the_parent_ledger(tmp_path: Path) -> None:
    ledger_dir = tmp_path / "children"
    parent = ledger_dir / "parent.jsonl"

    outcome = run_child_task(
        objective="add sub() to calc.py",
        workspace=_worktree(tmp_path),
        model=ModelConfig(model_id="test/fake"),
        profile="react",
        parent_task_id="parent-1",
        ledger_dir=ledger_dir,
        client=_edit_then_finish(),
    )

    assert Path(outcome.ledger_path) != parent
    assert not parent.exists(), "父账本不能被子任务写入（单写者）"


def test_unknown_child_profile_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(KeyError, match="unknown child profile"):
        run_child_task(
            objective="x",
            workspace=_worktree(tmp_path),
            model=ModelConfig(model_id="test/fake"),
            profile="not-a-profile",
        )


def test_child_timeout_is_reported_not_raised(tmp_path: Path) -> None:
    outcome = run_child_task(
        objective="x",
        workspace=_worktree(tmp_path),
        model=ModelConfig(model_id="test/fake"),
        profile="react",
        timeout=0.001,
        client=FakeClient([]),
    )

    assert outcome.error is not None
    assert outcome.phase == "failed"
    assert outcome.delivered is False


def test_parallel_children_keep_input_order(tmp_path: Path) -> None:
    outcomes = run_child_tasks(
        objectives=["first", "second", "third"],
        workspace=_worktree(tmp_path),
        model=ModelConfig(model_id="test/fake"),
        profile="react",
        parent_task_id="parent-2",
        ledger_dir=tmp_path / "children",
        client=_edit_then_finish(),
    )

    assert [item.objective for item in outcomes] == ["first", "second", "third"]
    assert [item.child_id for item in outcomes] == [
        f"parent-2-sub{index}-{item.child_id.rsplit('-', 1)[1]}"
        for index, item in enumerate(outcomes, 1)
    ]
    assert len({item.ledger_path for item in outcomes}) == 3


def test_child_evidence_is_a_pointer_not_a_copy() -> None:
    from xenon.harness.child import ChildOutcome

    outcome = ChildOutcome(
        child_id="p-sub1-abc",
        objective="do it",
        profile="react",
        accepted=True,
        phase="finalized",
        answer="done",
        ledger_path="/tmp/child.jsonl",
        debt=(),
        tool_calls=2,
        elapsed_seconds=1.5,
    )

    payload = child_evidence(outcome)

    assert payload["ledger"] == "/tmp/child.jsonl"
    assert payload["delivered"] is True
    assert "events" not in payload, "父会话只记录指针，不复制子事件"


def test_sub_agent_command_uses_the_child_runtime() -> None:
    from xenon.repl.command_groups import agent

    source = inspect.getsource(agent)
    assert "run_child_task" in source
    assert "run_child_tasks" in source
    assert "ReActEngine" not in source, "命令层不应再构造引擎"
    assert "_spawn_subagent" not in source


def test_sub_agent_records_a_pointer_event(tmp_path: Path, monkeypatch) -> None:
    from xenon.harness.child import ChildOutcome
    from xenon.repl.command_groups import agent

    captured: list[ChildOutcome] = []
    events: list[tuple[str, dict]] = []

    def fake_child(**kwargs):
        outcome = ChildOutcome(
            child_id="p-sub1-x",
            objective=kwargs["objective"],
            profile=kwargs["profile"],
            accepted=True,
            phase="finalized",
            answer="ok",
            ledger_path=str(tmp_path / "child.jsonl"),
            debt=(),
            tool_calls=1,
            elapsed_seconds=0.1,
        )
        captured.append(outcome)
        return outcome

    class Log:
        def append(self, kind, **kwargs):
            events.append((kind, kwargs.get("metadata") or {}))

    class Model:
        model_id = "test/fake"

        def get_healthy(self):
            return [self]

    class Registry:
        def get_model_by_id(self, model_id):
            return None

    repl = type(
        "Repl",
        (),
        {
            "model_pool": Model(),
            "registry": Registry(),
            "_session_events": Log(),
            "project_ctx": type("Ctx", (), {"root": tmp_path})(),
            "_last_kernel_task_id": "parent-9",
        },
    )()

    monkeypatch.setattr(agent, "run_child_task", fake_child, raising=False)
    import xenon.harness.child as child_mod

    monkeypatch.setattr(child_mod, "run_child_task", fake_child)

    output = agent._cmd_sub_agent(args="审查 calc.py", session_state={}, repl=repl)

    assert "子任务 1/1 已交付" in output
    assert captured, "命令必须调用子运行时"
    assert events and events[0][0] == "child_task"
    assert events[0][1]["ledger"].endswith("child.jsonl")


# ── process isolation (#354) ─────────────────────────────────────────
#
# The bug these tests exist for: an abandoned child thread kept writing to the
# workspace after the parent recorded a timeout.  A process group can be killed,
# and the kill is observable.


def test_a_default_child_runs_in_its_own_process(tmp_path: Path, monkeypatch) -> None:
    """No injection seam -> a real subprocess, and the outcome carries its pid."""

    import xenon.harness.child as child_mod

    seen: dict = {}

    def spy(spec, **kwargs):
        seen["spec"] = spec
        return (
            {
                "child_id": spec["child_id"],
                "accepted": True,
                "phase": "finalized",
                "answer": "done",
                "ledger_path": str(tmp_path / "c.jsonl"),
                "debt": [],
                "tool_calls": 2,
            },
            None,
            4242,
            False,
        )

    monkeypatch.setattr(child_mod, "_run_in_subprocess", spy)
    outcome = run_child_task(
        objective="x",
        workspace=_worktree(tmp_path),
        model=ModelConfig(model_id="test/fake"),
        profile="react",
    )

    assert "spec" in seen, "the default path must not be in-process"
    assert outcome.pid == 4242 and outcome.killed is False
    assert outcome.accepted is True and outcome.tool_calls == 2


def _blocking_base_url() -> tuple[str, object]:
    """A local socket that accepts the request and never answers.

    A timeout test must not depend on the child being slow: on a fast machine
    the child may finish before the deadline and then nothing is killed.  Making
    the *model call* block removes the race entirely — the child is guaranteed to
    still be running when the deadline fires.
    """

    import socket

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    port = server.getsockname()[1]
    return f"http://127.0.0.1:{port}/v1", server


def test_a_timeout_kills_the_process_group(tmp_path: Path) -> None:
    """The child is killed, not abandoned: nothing survives to write later."""

    import time

    base_url, server = _blocking_base_url()
    try:
        ws = _worktree(tmp_path)
        started = time.time()
        outcome = run_child_task(
            objective="read calc.py and report its function",
            workspace=ws,
            model=ModelConfig(
                model_id="deepseek/test", base_url=base_url, api_key="sk-test"
            ),
            profile="react",
            ledger_dir=tmp_path / "children",
            timeout=3.0,
        )
        elapsed = time.time() - started
    finally:
        server.close()

    assert outcome.killed is True, f"child was not killed: {outcome.error!r}"
    assert outcome.phase == "killed"
    assert outcome.delivered is False
    assert "killed" in (outcome.error or "")
    assert outcome.pid > 0
    assert elapsed < 60


def test_an_unknown_profile_is_rejected_before_spawning(tmp_path: Path) -> None:
    """Parent validates first: a bad name must not start a process."""

    with pytest.raises(KeyError):
        run_child_task(
            objective="x",
            workspace=_worktree(tmp_path),
            model=ModelConfig(model_id="test/fake"),
            profile="not-a-real-profile",
            ledger_dir=tmp_path / "children",
        )


def test_child_evidence_reports_process_identity(tmp_path: Path) -> None:
    from xenon.harness.child import ChildOutcome

    outcome = ChildOutcome(
        child_id="p-sub1-abc",
        objective="x",
        profile="react",
        accepted=False,
        phase="killed",
        answer="",
        ledger_path=str(tmp_path / "c.jsonl"),
        debt=(),
        tool_calls=1,
        elapsed_seconds=0.5,
        error="timeout",
        pid=999,
        killed=True,
    )
    payload = child_evidence(outcome)

    assert payload["pid"] == 999 and payload["killed"] is True
    assert payload["delivered"] is False


def test_a_killed_child_leaves_no_process_behind(tmp_path: Path) -> None:
    """`ps` shows nothing: the pid is gone once the call returns."""

    import os
    import subprocess
    import sys

    base_url, server = _blocking_base_url()
    try:
        outcome = run_child_task(
            objective="read calc.py",
            workspace=_worktree(tmp_path),
            model=ModelConfig(
                model_id="deepseek/test", base_url=base_url, api_key="sk-test"
            ),
            profile="react",
            ledger_dir=tmp_path / "children",
            timeout=3.0,
        )
    finally:
        server.close()

    assert outcome.killed is True, f"child was not killed: {outcome.error!r}"
    pid = outcome.pid
    assert pid > 0
    if sys.platform == "win32":
        listing = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        assert str(pid) not in listing, f"child pid {pid} is still alive"
    else:  # pragma: no cover — POSIX-only branch
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        raise AssertionError(f"child pid {pid} is still alive")


# ── worker contract (#354) ───────────────────────────────────────────


def _spec(tmp_path: Path, **overrides) -> dict:
    workspace = _worktree(tmp_path)
    from xenon.harness.child_worker import model_to_spec

    spec = {
        "child_id": "p-sub1-abc",
        "objective": "add a marker to calc.py",
        "workspace": str(workspace),
        "profile": "react",
        "permission": "auto-edit",
        "max_steps": 4,
        "ledger_dir": str(tmp_path / "children"),
        "model": model_to_spec(ModelConfig(model_id="test/fake")),
    }
    spec.update(overrides)
    return spec


def test_the_worker_payload_is_what_the_parent_reads(tmp_path: Path) -> None:
    """Run the real worker body in-process and check it reports the same shape
    the subprocess path parses."""

    from xenon.harness.child_worker import run_spec

    client = _edit_then_finish()
    payload = run_spec(_spec(tmp_path), client=client)

    assert payload["child_id"] == "p-sub1-abc"
    assert payload["phase"] in {"finalized", "blocked", "failed"}
    assert isinstance(payload["debt"], list)
    assert isinstance(payload["tool_calls"], int)
    assert Path(payload["ledger_path"]).name.startswith("p-sub1-abc")


def test_the_worker_rejects_an_unknown_profile(tmp_path: Path) -> None:
    from xenon.harness.child_worker import PROFILES, run_spec

    assert "not-a-profile" not in PROFILES
    with pytest.raises(KeyError):
        run_spec(_spec(tmp_path, profile="not-a-profile"))


def test_the_worker_main_writes_the_outcome_atomically(tmp_path: Path) -> None:
    """`main` is only plumbing: read spec -> run -> rename into place."""

    from xenon.harness import child_worker

    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_spec(tmp_path)), encoding="utf-8")
    out_path = tmp_path / "outcome.json"

    payload = {"child_id": "p-sub1-abc", "accepted": True, "phase": "finalized"}
    original = child_worker.run_spec
    child_worker.run_spec = lambda spec, **kwargs: payload
    try:
        assert child_worker.main([str(spec_path), str(out_path)]) == 0
    finally:
        child_worker.run_spec = original

    assert json.loads(out_path.read_text(encoding="utf-8")) == payload
    assert not (tmp_path / "outcome.json.tmp").exists()


def test_the_worker_main_rejects_bad_arguments(tmp_path: Path) -> None:
    from xenon.harness import child_worker

    assert child_worker.main([]) == 2
    assert child_worker.main([str(tmp_path / "only-one")]) == 2


def test_a_model_config_round_trips_through_the_spec() -> None:
    from xenon.harness.child_worker import model_from_spec, model_to_spec

    original = ModelConfig(
        model_id="deepseek/x",
        base_url="http://h/v1",
        api_key="sk-1",
        max_tokens=123,
        temperature=0.5,
        reasoning_effort="high",
        context_window=64000,
        # Hooks are parent-process callbacks: they must not survive the boundary.
        usage_sink=lambda *a: None,
        should_cancel=lambda: False,
        progress_sink=lambda *a: None,
    )
    restored = model_from_spec(model_to_spec(original))

    assert restored.model_id == original.model_id
    assert restored.context_window == 64000
    assert restored.usage_sink is None
    assert restored.should_cancel is None
    assert restored.progress_sink is None


def test_a_failed_child_summary_shows_the_error_not_a_fake_answer(tmp_path: Path) -> None:
    from xenon.harness.child import ChildOutcome

    outcome = ChildOutcome(
        child_id="p-sub1-x",
        objective="x",
        profile="react",
        accepted=False,
        phase="failed",
        answer="",
        ledger_path=str(tmp_path / "c.jsonl"),
        debt=(),
        tool_calls=0,
        elapsed_seconds=0.1,
        error="boom",
    )

    text = outcome.summary()

    assert "boom" in text
    assert "失败" in text


def test_a_delivered_child_summary_names_its_ledger(tmp_path: Path) -> None:
    from xenon.harness.child import ChildOutcome

    outcome = ChildOutcome(
        child_id="p-sub1-x",
        objective="x",
        profile="react",
        accepted=True,
        phase="finalized",
        answer="did the thing",
        ledger_path=str(tmp_path / "c.jsonl"),
        debt=("one gap",),
        tool_calls=2,
        elapsed_seconds=0.1,
    )

    text = outcome.summary()

    assert "did the thing" in text
    assert "one gap" in text and "ledger:" in text


def test_parallel_with_nothing_to_do_returns_nothing(tmp_path: Path) -> None:
    assert run_child_tasks(
        objectives=["", "   "],
        workspace=_worktree(tmp_path),
        model=ModelConfig(model_id="test/fake"),
    ) == ()


def test_an_exception_inside_a_child_is_an_outcome_not_a_crash(tmp_path: Path) -> None:
    """The in-process seam must swallow model/loop exceptions into data."""

    def exploding(*args, **kwargs):
        raise RuntimeError("model exploded")

    outcome = run_child_task(
        objective="x",
        workspace=_worktree(tmp_path),
        model=ModelConfig(model_id="test/fake"),
        profile="react",
        ledger_dir=tmp_path / "children",
        client=exploding,
    )

    assert outcome.phase == "failed"
    assert "model exploded" in (outcome.error or "")
    assert outcome.delivered is False


def test_a_child_that_crashes_becomes_an_error_not_an_exception(tmp_path: Path) -> None:
    """A worker that dies before writing an outcome is data, with a reason.

    The spec is missing ``model``, so the worker raises before it can write —
    exactly the shape of a crash, without patching anything in-process (a patch
    cannot reach a spawned process).
    """

    from xenon.harness import child as child_mod

    payload, error, pid, killed = child_mod._run_in_subprocess(
        {"child_id": "p-sub1-die", "profile": "react", "objective": "x"},
        ledger_dir=tmp_path / "children",
        timeout=90,
        child_id="p-sub1-die",
    )

    assert payload is None
    assert killed is False
    assert pid > 0
    assert "without an outcome" in (error or "")


def test_a_corrupt_outcome_is_an_error_not_a_silent_pass(tmp_path: Path) -> None:
    from xenon.harness.child import _read_outcome

    out = tmp_path / "outcome.json"
    out.write_text("{not json", encoding="utf-8")

    payload, error = _read_outcome(out)

    assert payload is None
    assert "unreadable child outcome" in (error or "")


def test_a_non_object_outcome_is_rejected(tmp_path: Path) -> None:
    """JSON is not enough: the parent expects an object, not a list or a scalar."""

    from xenon.harness.child import _read_outcome

    out = tmp_path / "outcome.json"
    out.write_text("[1, 2, 3]", encoding="utf-8")

    payload, error = _read_outcome(out)

    assert payload is None
    assert error == "child outcome is not an object"


def test_a_missing_outcome_file_is_merely_absent(tmp_path: Path) -> None:
    from xenon.harness.child import _read_outcome

    payload, error = _read_outcome(tmp_path / "nope.json")

    assert payload is None and error is None


def test_an_exception_under_a_deadline_is_still_an_outcome(tmp_path: Path) -> None:
    """The timeout branch must not leak an exception either."""

    def exploding(*args, **kwargs):
        raise RuntimeError("model exploded")

    outcome = run_child_task(
        objective="x",
        workspace=_worktree(tmp_path),
        model=ModelConfig(model_id="test/fake"),
        profile="react",
        ledger_dir=tmp_path / "children",
        client=exploding,
        timeout=60,
    )

    assert outcome.phase == "failed"
    assert "model exploded" in (outcome.error or "")


def test_killing_a_process_that_already_exited_is_a_no_op() -> None:
    """A child may finish between the deadline firing and the kill."""

    import subprocess
    import sys

    from xenon.harness.child import _kill_tree

    proc = subprocess.Popen([sys.executable, "-m", "site"])  # exits immediately
    proc.wait()
    _kill_tree(proc)  # must not raise

    assert proc.poll() == 0


def test_kill_tree_forces_a_kill_when_the_group_ignores_the_first_one() -> None:
    """If the group refuses to die, the child handle is killed directly."""

    from xenon.harness.child import _kill_tree

    class Stubborn:
        pid = 123456  # not a real process: every kill below is a no-op stub

        def __init__(self) -> None:
            self.killed = 0

        def poll(self):
            return None  # still "alive" as far as the caller knows

        def wait(self, timeout=None):
            raise __import__("subprocess").TimeoutExpired("x", timeout)

        def kill(self):
            self.killed += 1

    proc = Stubborn()
    _kill_tree(proc)

    assert proc.killed == 1


def test_the_module_entry_point_is_reachable_as_main() -> None:
    """`python -m xenon.harness.child_worker` is the real entry point."""

    import runpy
    import sys

    saved = sys.argv
    sys.argv = ["prog"]  # no spec/outcome -> usage error, exit 2
    try:
        with pytest.raises(SystemExit) as excinfo:
            runpy.run_module("xenon.harness.child_worker", run_name="__main__")
    finally:
        sys.argv = saved

    assert excinfo.value.code == 2


class _CountingApproval:
    """Parent-side provider: counts asks, answers yes (one-shot)."""

    def __init__(self) -> None:
        self.calls = 0
        self.last_reason = ""
        self.last_scope = ""

    def request(self, view, action, decision):
        self.calls += 1
        self.last_reason = "user approval"
        return True


def _shell_then_finish() -> FakeClient:
    return FakeClient(
        [
            json.dumps(
                {
                    "action": "call_tool",
                    "capability": "shell",
                    "params": {"command": "echo hello"},
                }
            ),
            json.dumps(
                {
                    "action": "finish",
                    "answer": "ran echo hello",
                    "claims": [
                        {"id": "c1", "kind": "tool_succeeded", "target": "echo hello"}
                    ],
                }
            ),
        ]
    )


def test_a_child_with_an_inherited_grant_never_asks(tmp_path: Path) -> None:
    """#373: the parent's session grant travels with the spec; the child's
    kernel answers the call itself and the broker is never invoked.

    Delivery (claims/verdicts) is the controller's business and covered by the
    edit-based tests; this test pins the *authorization* behaviour: who answered.
    """

    from xenon.kernel.contracts import Capability
    from xenon.kernel.session_grants import SESSION, grant_from_choice

    workspace = _worktree(tmp_path)
    grant = grant_from_choice(
        SESSION,
        capability=Capability.SHELL,
        targets=("echo hello",),
        workspace=str(workspace),
    )
    assert grant is not None
    approval = _CountingApproval()

    outcome = run_child_task(
        objective="run echo hello",
        workspace=workspace,
        model=ModelConfig(model_id="test/fake"),
        profile="react",
        parent_task_id="parent-1",
        ledger_dir=tmp_path / "children",
        permission=PermissionMode.AUTO_EDIT,
        client=_shell_then_finish(),
        approval=approval,
        session_grants=(grant,),
    )

    assert approval.calls == 0, "the inherited grant must answer without the broker"
    # The call still ran: the ledger has the tool receipt.
    from xenon.ledger.store import Ledger

    events = Ledger(Path(outcome.ledger_path), outcome.child_id).read()
    assert any(event.kind.value == "tool_receipt" for event in events)


def test_a_child_without_a_grant_asks_the_parent(tmp_path: Path) -> None:
    """#373: without an inherited grant the child's shell call is brokered."""

    approval = _CountingApproval()
    outcome = run_child_task(
        objective="run echo hello",
        workspace=_worktree(tmp_path),
        model=ModelConfig(model_id="test/fake"),
        profile="react",
        parent_task_id="parent-1",
        ledger_dir=tmp_path / "children",
        permission=PermissionMode.AUTO_EDIT,
        client=_shell_then_finish(),
        approval=approval,
    )

    assert approval.calls == 1, "without a grant the child must ask the parent"
    from xenon.ledger.store import Ledger

    events = Ledger(Path(outcome.ledger_path), outcome.child_id).read()
    assert any(event.kind.value == "tool_receipt" for event in events)
