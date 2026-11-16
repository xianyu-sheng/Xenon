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
from xenon.harness.llm import (
    _TERMINAL_TOOLS,
    action_from_tool_call,
    build_messages,
    current_turn,
    render_state,
)
from xenon.kernel.contracts import (
    Budget,
    ModelView,
    RequestEscalation,
    SessionTaskRecord,
    TaskPhase,
    TaskSpec,
)
from xenon.kernel.session_history import (
    recent_task_lines,
    recent_tasks,
    task_record,
)
from xenon.kernel.view.base import session_section


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
    assert "task" in what["enum"]
    assert "earlier turns of this session" in what["description"]


def test_recent_tasks_carry_the_answer_excerpt_and_ledger(tmp_path: Path) -> None:
    workspace = str(tmp_path)
    _ledger(tmp_path / "a.jsonl", objective="代码审阅", session="s", workspace=workspace)
    current = _ledger(tmp_path / "cur.jsonl", objective="继续", session="s", workspace=workspace)

    records = recent_tasks(current, session_id="s")

    assert [record.task_id for record in records] == ["a"]
    assert records[0].status == "verified"
    assert records[0].answer == "done: 代码审阅"
    assert records[0].ledger == str(tmp_path / "a.jsonl")


def test_task_record_looks_up_one_turn_and_rejects_paths(tmp_path: Path) -> None:
    workspace = str(tmp_path)
    _ledger(tmp_path / "a.jsonl", objective="代码审阅", session="s", workspace=workspace)
    current = _ledger(tmp_path / "cur.jsonl", objective="继续", session="s", workspace=workspace)

    record = task_record(current, "a")
    assert record is not None and record.objective == "代码审阅"
    assert task_record(current, "..\\a") is None
    assert task_record(current, "missing") is None


def test_chat_ledgers_one_level_down_are_the_same_session(tmp_path: Path) -> None:
    """A chat turn is an earlier turn even though it sits in ``chat/``."""

    import os
    import time

    workspace = str(tmp_path)
    chat = tmp_path / "chat"
    chat.mkdir()
    task = _ledger(tmp_path / "a.jsonl", objective="审阅仓库", session="s", workspace=workspace)
    turn = _ledger(chat / "chat-1.jsonl", objective="解释缓存", session="s", workspace=workspace)
    current = _ledger(tmp_path / "cur.jsonl", objective="继续", session="s", workspace=workspace)
    for offset, item in enumerate((task, turn, current)):
        os.utime(item, (time.time() + offset, time.time() + offset))

    records = recent_tasks(current, session_id="s")

    assert [record.task_id for record in records] == ["chat-1", "a"]
    assert records[0].answer == "done: 解释缓存"
    assert task_record(current, "chat-1") is not None


def test_a_chat_ledger_sees_its_own_session_too(tmp_path: Path) -> None:
    """The current ledger may itself be a chat record; the parent counts."""

    workspace = str(tmp_path)
    chat = tmp_path / "chat"
    chat.mkdir()
    _ledger(tmp_path / "a.jsonl", objective="审阅仓库", session="s", workspace=workspace)
    current = _ledger(chat / "chat-1.jsonl", objective="解释缓存", session="s", workspace=workspace)

    records = recent_tasks(current, session_id="s")

    assert [record.task_id for record in records] == ["a"]


def test_session_section_renders_excerpts_and_points_at_the_full_record() -> None:
    records = (
        SessionTaskRecord(
            task_id="repl-1",
            objective="审阅仓库并列出改进点",
            status="partial",
            answer="1. 把认证抽成中间件\n2. 补端到端测试",
            ledger="/ledger/repl-1.jsonl",
        ),
    )

    text = session_section(records)

    assert "Earlier turns this session" in text
    assert "1. 把认证抽成中间件" in text
    assert 'get_state {"what":"task","task_id":"repl-1"}' in text
    assert session_section(()) == ""


def test_session_section_bounds_the_excerpt_at_the_miss_price() -> None:
    """The excerpt is new bytes on every task's first call.

    It used to be 1,200 characters per earlier turn -- the single largest
    part of a new task's cache miss -- for text the model can read in full
    with one ``get_state`` call.
    """

    records = (
        SessionTaskRecord(
            task_id="repl-1",
            objective="审阅仓库",
            status="delivered",
            answer="甲" * 900,
            ledger="/ledger/repl-1.jsonl",
        ),
    )
    text = session_section(records)
    excerpt = text.split("Answer excerpt:", 1)[1]
    assert excerpt.count("甲") == 300
    assert "…" in excerpt
    assert 'get_state {"what":"task","task_id":"repl-1"}' in text


def test_render_state_task_returns_the_full_projection() -> None:
    record = SessionTaskRecord(
        task_id="repl-1", objective="审阅", status="partial", answer="正文", ledger="/l.jsonl"
    )
    view = _view(session_tasks=(record,))

    payload = json.loads(render_state(view, "task", {"task_id": "repl-1"}))
    assert payload["answer"] == "正文"
    assert payload["ledger"] == "/l.jsonl"
    assert "needs task_id" in render_state(view, "task", {})
    assert "No earlier task" in render_state(view, "task", {"task_id": "nope"})


def test_native_request_escalation_is_offered_and_maps_to_the_action() -> None:
    view = _view()
    action = action_from_tool_call(
        view,
        "request_escalation",
        {
            "capability": "shell",
            "params": {"command": "Get-ChildItem -Recurse -File"},
            "reason": "counting files needs a directory listing",
        },
    )

    assert isinstance(action, RequestEscalation)
    assert action.intent.capability.value == "shell"
    assert action.reason
    assert "request_escalation" in _TERMINAL_TOOLS


def test_runtime_puts_the_previous_turn_in_the_system_prompt(tmp_path: Path) -> None:
    from xenon.catalog.catalog import ToolCatalog
    from xenon.catalog.gateway import LegacyToolGateway
    from xenon.catalog.specs import CORE_SPECS
    from xenon.kernel.contracts import Finish
    from xenon.kernel.controller import VerificationController
    from xenon.kernel.policies import PermissionPolicy
    from xenon.kernel.runtime import LoopRuntime
    from xenon.ledger.store import Ledger

    _ledger(
        tmp_path / "prev.jsonl",
        objective="审阅仓库并列出改进点",
        session="s-42",
        workspace=str(tmp_path),
    )
    # The prior answer the follow-up refers back to.
    prev = tmp_path / "prev.jsonl"
    text = prev.read_text(encoding="utf-8").replace(
        "done: 审阅仓库并列出改进点", "1. 把认证抽成中间件\\n2. 补端到端测试"
    )
    prev.write_text(text, encoding="utf-8")

    seen: list[Any] = []

    class _Proposer:
        def propose(self, view: Any) -> Any:
            seen.append(view)
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
            objective="对上面的第一点起草 PR",
            workspace=str(tmp_path),
            session_id="s-42",
        ),
        _Proposer(),
    )

    view = seen[0]
    # The projection is per-turn context, so it rides in the turn message; if
    # it sat in the system prompt it would invalidate the tool block (which
    # providers serialise right behind the system) on the first call of every
    # new turn.
    turn = build_messages(view)[1]["content"]
    assert "Earlier turns this session" in turn
    assert "1. 把认证抽成中间件" in turn
    assert "prev" in turn
    assert "Earlier turns this session" not in view.system


def test_runtime_sees_a_previous_chat_turn(tmp_path: Path) -> None:
    """A tool turn must still see an earlier explanation (no ledger tool ran).

    Chat turns live under ``chat/``; before this fix the kernel prompt skipped
    that directory, so "对上面第一点" after a chat answer had nothing to point
    at and the turn answered "上一轮不存在".
    """

    from xenon.catalog.catalog import ToolCatalog
    from xenon.catalog.gateway import LegacyToolGateway
    from xenon.catalog.specs import CORE_SPECS
    from xenon.kernel.contracts import Finish
    from xenon.kernel.controller import VerificationController
    from xenon.kernel.policies import PermissionPolicy
    from xenon.kernel.runtime import LoopRuntime
    from xenon.ledger.conversation import (
        finish_conversation_turn,
        open_conversation_turn,
    )
    from xenon.ledger.store import Ledger

    turn = open_conversation_turn(
        tmp_path,
        objective="列出 3 条降内存的做法",
        workspace=str(tmp_path),
        session_id="s-42",
    )
    assert turn is not None
    finish_conversation_turn(
        turn, answer="1. 用 __slots__\n2. 用生成器\n3. 及时释放大对象", intent="chat"
    )

    seen: list[Any] = []

    class _Proposer:
        def propose(self, view: Any) -> Any:
            seen.append(view)
            return Finish("done")

    policy = PermissionPolicy(workspace=str(tmp_path))
    runtime = LoopRuntime(
        catalog=ToolCatalog(CORE_SPECS, "core"),
        gateway=LegacyToolGateway(specs=CORE_SPECS, workspace=tmp_path, policy=policy),
        controller=VerificationController(),
        ledger=Ledger.open(tmp_path / "run.jsonl", "t1"),
        policies=(policy,),
    )
    runtime.run(
        TaskSpec(
            task_id="t1",
            objective="把上面第 1 点写成代码",
            workspace=str(tmp_path),
            session_id="s-42",
        ),
        _Proposer(),
    )

    turn = build_messages(seen[0])[1]["content"]
    assert "Earlier turns this session" in turn
    assert "用 __slots__" in turn


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
