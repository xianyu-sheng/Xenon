"""The call tree is derived from ledgers, not from session state (#356)."""

from __future__ import annotations

import xenon.kernel  # noqa: F401  — ledger<->kernel import cycle
from xenon.harness.child import run_child_task
from xenon.harness.llm import ModelConfig
from xenon.ledger.events import EventKind, EventSource
from xenon.ledger.store import Ledger
from xenon.replay import children_of, render_children


def _child_ledger(path, task_id: str, parent: str, *, debt: str = "") -> None:
    ledger = Ledger(path, task_id)
    ledger.append(
        EventKind.TASK_STARTED,
        EventSource.RUNTIME,
        {
            "objective": f"child {task_id}",
            "user_objective": "",
            "workspace": ".",
            "mode": "auto",
            "constraints": [],
            "parent_task_id": parent,
        },
    )
    if debt:
        ledger.append(
            EventKind.DEBT_OPENED,
            EventSource.RUNTIME,
            {"obligation_id": "o1", "reason": debt, "scope": "task"},
        )
    ledger.append(
        EventKind.STATE_TRANSITION,
        EventSource.RUNTIME,
        {"from": "running", "to": "blocked"},
    )


def _workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "calc.py").write_text("x = 1\n", encoding="utf-8")
    return ws


def test_the_parent_id_is_recorded_in_the_child_ledger(tmp_path) -> None:
    """A real child run: the link must be in the ledger, not only in the name."""

    ledger_dir = tmp_path / "children"
    outcome = run_child_task(
        objective="x",
        workspace=_workspace(tmp_path),
        model=ModelConfig(model_id="test/fake"),
        profile="react",
        parent_task_id="parent-abc",
        ledger_dir=ledger_dir,
        client=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no model")),
    )

    events = Ledger.open(outcome.ledger_path, task_id=outcome.child_id).read()
    started = next(
        e.payload for e in events if e.kind is EventKind.TASK_STARTED
    )

    assert started["parent_task_id"] == "parent-abc"


def test_children_are_found_by_the_recorded_link(tmp_path) -> None:
    root = tmp_path / "ledger"
    (root / "children").mkdir(parents=True)
    _child_ledger(root / "children" / "p-sub1-aaaa.jsonl", "p-sub1-aaaa", "p")
    _child_ledger(root / "children" / "p-sub2-bbbb.jsonl", "p-sub2-bbbb", "p", debt="x")
    _child_ledger(root / "children" / "q-sub1-cccc.jsonl", "q-sub1-cccc", "q")

    rows = children_of("p", root=root)

    assert {row["child_id"] for row in rows} == {"p-sub1-aaaa", "p-sub2-bbbb"}
    assert all(row["parent_task_id"] == "p" for row in rows)


def test_a_child_named_like_a_parent_but_linked_elsewhere_is_not_listed(tmp_path) -> None:
    """The link is data, not string parsing: names must not decide the tree."""

    root = tmp_path / "ledger"
    (root / "children").mkdir(parents=True)
    # Named as if it belonged to "p", but its ledger says "q".
    _child_ledger(root / "children" / "p-sub1-zzzz.jsonl", "p-sub1-zzzz", "q")

    assert children_of("p", root=root) == []
    assert len(children_of("q", root=root)) == 1


def test_an_unreadable_child_ledger_is_reported_not_skipped(tmp_path) -> None:
    root = tmp_path / "ledger"
    (root / "children").mkdir(parents=True)
    bad = root / "children" / "broken.jsonl"
    bad.write_text("{ not a ledger", encoding="utf-8")

    rows = children_of("p", root=root)

    assert len(rows) == 1
    assert rows[0]["phase"] == "unreadable"
    assert rows[0]["error"]


def test_no_children_directory_is_simply_empty(tmp_path) -> None:
    assert children_of("p", root=tmp_path / "nothing") == []


def test_the_rendered_tree_names_phase_debt_and_ledger(tmp_path) -> None:
    root = tmp_path / "ledger"
    (root / "children").mkdir(parents=True)
    _child_ledger(root / "children" / "p-sub1-aaaa.jsonl", "p-sub1-aaaa", "p", debt="x")

    text = render_children(children_of("p", root=root), parent="p")

    assert "children of p: 1" in text
    assert "phase=blocked" in text
    assert "debt=1" in text
    assert "ledger:" in text


def test_an_empty_tree_says_so_instead_of_printing_nothing() -> None:
    assert render_children([], parent="p") == "children of p: (none)"


# ── batch_id: one dispatch is distinguishable from several (#361) ────


def test_one_dispatch_shares_a_batch_id(tmp_path) -> None:
    from xenon.harness.child import run_child_tasks

    ws = _workspace(tmp_path)
    outcomes = run_child_tasks(
        objectives=["a", "b", "c"],
        workspace=ws,
        model=ModelConfig(model_id="test/fake"),
        profile="react",
        parent_task_id="p",
        ledger_dir=tmp_path / "children",
        client=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no model")),
        isolate=False,
    )

    batches = {outcome.batch_id for outcome in outcomes}

    assert len(outcomes) == 3
    assert len(batches) == 1, f"one dispatch must be one batch, got {batches}"
    assert all(batches) and "" not in batches


def test_two_dispatches_have_different_batch_ids(tmp_path) -> None:
    from xenon.harness.child import run_child_tasks

    ws = _workspace(tmp_path)
    kwargs = dict(
        workspace=ws,
        model=ModelConfig(model_id="test/fake"),
        profile="react",
        parent_task_id="p",
        client=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no model")),
        isolate=False,
    )
    first = run_child_tasks(
        objectives=["a"], ledger_dir=tmp_path / "one", **kwargs
    )
    second = run_child_tasks(
        objectives=["a"], ledger_dir=tmp_path / "two", **kwargs
    )

    assert first[0].batch_id != second[0].batch_id


def test_a_lone_child_is_still_a_batch_of_one(tmp_path) -> None:
    from xenon.harness.child import run_child_task

    outcome = run_child_task(
        objective="x",
        workspace=_workspace(tmp_path),
        model=ModelConfig(model_id="test/fake"),
        profile="react",
        parent_task_id="p",
        ledger_dir=tmp_path / "children",
        client=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no model")),
    )

    assert outcome.batch_id


def test_the_batch_id_is_in_the_child_ledger(tmp_path) -> None:
    from xenon.harness.child import run_child_task

    outcome = run_child_task(
        objective="x",
        workspace=_workspace(tmp_path),
        model=ModelConfig(model_id="test/fake"),
        profile="react",
        parent_task_id="p",
        batch_id="p-batch-fixed",
        ledger_dir=tmp_path / "children",
        client=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no model")),
    )
    events = Ledger.open(outcome.ledger_path, task_id=outcome.child_id).read()
    started = next(e.payload for e in events if e.kind is EventKind.TASK_STARTED)

    assert started["batch_id"] == "p-batch-fixed"


def test_children_are_grouped_by_batch_in_the_report(tmp_path) -> None:
    root = tmp_path / "ledger"
    (root / "children").mkdir(parents=True)
    _child_ledger(root / "children" / "p-sub1-a.jsonl", "p-sub1-a", "p")
    _child_ledger(root / "children" / "p-sub2-b.jsonl", "p-sub2-b", "p")

    rows = children_of("p", root=root)
    text = render_children(rows, parent="p")

    # Ledgers written before batch_id existed must be labelled, not merged away.
    assert "[batch (no batch)] 2 child(ren), 0 delivered" in text


def test_batches_are_rendered_separately(tmp_path) -> None:
    root = tmp_path / "ledger"
    (root / "children").mkdir(parents=True)
    a = root / "children" / "p-sub1-a.jsonl"
    b = root / "children" / "p-sub2-b.jsonl"
    _child_ledger(a, "p-sub1-a", "p")
    _child_ledger(b, "p-sub2-b", "p")

    # Rewrite with explicit batch ids by appending a second started event is not
    # valid; instead build them directly.
    for path, batch, task_id in ((a, "b1", "p-sub1-a"), (b, "b2", "p-sub2-b")):
        ledger = Ledger(path.parent / f"{path.stem}-x.jsonl", task_id)
        ledger.append(
            EventKind.TASK_STARTED,
            EventSource.RUNTIME,
            {
                "objective": task_id,
                "workspace": ".",
                "mode": "auto",
                "constraints": [],
                "parent_task_id": "p",
                "batch_id": batch,
            },
        )

    rows = children_of("p", root=root)
    text = render_children(rows, parent="p")

    assert text.count("[batch b1]") == 1
    assert text.count("[batch b2]") == 1
