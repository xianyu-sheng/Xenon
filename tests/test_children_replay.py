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
