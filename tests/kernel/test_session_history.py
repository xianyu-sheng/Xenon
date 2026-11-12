"""Session history is pulled, not pushed (design doc §3.2).

The decision: a turn carries only the fixed prefix and this turn's message.
Earlier tasks are reachable through ``get_state what=session`` — one line each,
projected from their own ledgers — and a constant pointer tells the model the
query exists.  Nothing about earlier tasks is precomputed into the prompt.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import xenon.kernel  # noqa: F401 — ledger<->kernel import cycle
from xenon.harness.llm import _TERMINAL_TOOLS, current_turn, render_state
from xenon.kernel.contracts import Budget, ModelView, TaskPhase, TaskSpec
from xenon.kernel.session_history import recent_task_lines


def _ledger(path: Path, *, objective: str, session: str, workspace: str) -> Path:
    records = [
        {"kind": "task_started", "payload": {
            "objective": objective,
            "user_objective": objective,
            "workspace": workspace,
            "session_id": session,
        }},
        {"kind": "delivered", "payload": {"answer": f"done: {objective}"}},
        {"kind": "finalized", "payload": {"evidence_status": "verified"}},
    ]
    path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n",
        encoding="utf-8",
    )
    return path


def _view(**overrides: Any) -> ModelView:
    fields: dict[str, Any] = {
        "task": TaskSpec(task_id="t", objective="继续", workspace="/w"),
        "system": "system",
        "tools": (),
        "working_set": (),
        "debt": (),
        "budget": Budget(),
        "input_seq": 0,
        "hash": "h",
        "phase": TaskPhase.RUNNING,
    }
    fields.update(overrides)
    return ModelView(**fields)


def test_recent_tasks_are_newest_first_and_filtered_by_session(tmp_path: Path) -> None:
    import os
    import time

    workspace = str(tmp_path)
    older = _ledger(tmp_path / "a.jsonl", objective="修 calc.py", session="s1", workspace=workspace)
    newer = _ledger(tmp_path / "b.jsonl", objective="跑测试", session="s1", workspace=workspace)
    other = _ledger(tmp_path / "c.jsonl", objective="别的会话", session="s2", workspace=workspace)
    current = _ledger(tmp_path / "d.jsonl", objective="继续", session="s1", workspace=workspace)
    for offset, item in enumerate((older, newer, other, current)):
        os.utime(item, (time.time() + offset, time.time() + offset))

    lines = recent_task_lines(current, session_id="s1", workspace=workspace)

    assert [line.split(" · ", 1)[1].split(" → ")[0] for line in lines] == [
        "跑测试",
        "修 calc.py",
    ]
    assert all("别的会话" not in line for line in lines)
    assert all(line.startswith(("b ·", "a ·")) for line in lines)
    assert "verified" in lines[0]


def test_workspace_is_the_fallback_boundary_without_a_session(tmp_path: Path) -> None:
    _ledger(tmp_path / "a.jsonl", objective="本项目的任务", session="", workspace=str(tmp_path))
    _ledger(tmp_path / "b.jsonl", objective="别的项目", session="", workspace=str(tmp_path / "other"))

    lines = recent_task_lines(tmp_path / "cur.jsonl", workspace=str(tmp_path))

    assert [line for line in lines if "本项目的任务" in line]
    assert not [line for line in lines if "别的项目" in line]


def test_limit_and_broken_lines(tmp_path: Path) -> None:
    for index in range(7):
        _ledger(tmp_path / f"t{index}.jsonl", objective=f"task {index}", session="s", workspace="/w")
    junk = tmp_path / "t6.jsonl"
    junk.write_text(junk.read_text(encoding="utf-8") + "{not json\n", encoding="utf-8")

    lines = recent_task_lines(tmp_path / "cur.jsonl", session_id="s", limit=3)

    assert len(lines) == 3


def test_render_state_session_is_a_projection_of_the_lines() -> None:
    view = _view(session_history=("t1 · 修 calc.py → verified",))

    payload = json.loads(render_state(view, "session", {}))

    assert payload["earlier_tasks"] == ["t1 · 修 calc.py → verified"]
    assert "No earlier tasks" in render_state(_view(), "session", {})


def test_current_turn_points_at_the_query_only_when_history_exists() -> None:
    with_history = current_turn(
        _view(session_history=("t1 · 修 calc.py → verified",)),
        instruction="go",
    )
    without = current_turn(_view(), instruction="go")

    assert 'get_state with what "session"' in with_history
    assert "session" not in without


def test_get_state_offers_the_session_slice() -> None:
    what = _TERMINAL_TOOLS["get_state"][1]["properties"]["what"]

    assert "session" in what["enum"]
    assert "earlier tasks of this session" in what["description"]


def test_task_started_records_the_session(tmp_path: Path) -> None:
    from xenon.catalog.catalog import ToolCatalog
    from xenon.catalog.gateway import LegacyToolGateway
    from xenon.catalog.specs import CORE_SPECS
    from xenon.kernel.contracts import Finish
    from xenon.kernel.controller import VerificationController
    from xenon.kernel.policies import PermissionPolicy
    from xenon.kernel.runtime import LoopRuntime
    from xenon.ledger.events import EventKind
    from xenon.ledger.store import Ledger

    class _Finish:
        def propose(self, view: Any) -> Any:
            return Finish("done")

    policy = PermissionPolicy(workspace=str(tmp_path))
    ledger = Ledger.open(tmp_path / "run.jsonl", "t1")
    runtime = LoopRuntime(
        catalog=ToolCatalog(CORE_SPECS, "core"),
        gateway=LegacyToolGateway(specs=CORE_SPECS, workspace=tmp_path, policy=policy),
        controller=VerificationController(),
        ledger=ledger,
        policies=(policy,),
    )

    runtime.run(
        TaskSpec(
            task_id="t1",
            objective="say done",
            workspace=str(tmp_path),
            session_id="s-42",
        ),
        _Finish(),
    )

    started = [
        event.payload for event in ledger.read() if event.kind is EventKind.TASK_STARTED
    ]
    assert started[0]["session_id"] == "s-42"
