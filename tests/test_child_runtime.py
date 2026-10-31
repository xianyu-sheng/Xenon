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
