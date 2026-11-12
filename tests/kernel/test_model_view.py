"""ModelView projection tests (#197)."""

from __future__ import annotations

from dataclasses import dataclass

from xenon.harness.llm import render_state
from xenon.kernel.contracts import (
    Budget,
    Capability,
    Debt,
    EffectKind,
    ModelView,
    Obligation,
    PropositionKind,
    ReadFact,
    ReceiptStatus,
    Risk,
    TaskMode,
    TaskSpec,
    ToolReceipt,
    ToolSpec,
)
from xenon.kernel.view import build_model_view
from xenon.ledger.events import EventKind, EventSource
from xenon.ledger.store import Ledger

SPECS = (
    ToolSpec(
        name="inspect",
        pack="fake",
        capabilities=frozenset({Capability.READ}),
        effects=frozenset({EffectKind.READ}),
        risk=Risk.INFO,
        description="Read a file.",
    ),
    ToolSpec(
        name="mutate",
        pack="fake",
        capabilities=frozenset({Capability.EDIT}),
        effects=frozenset({EffectKind.FS_WRITE}),
        risk=Risk.WRITE,
        description="Write a file.",
    ),
    ToolSpec(
        name="check",
        pack="fake",
        capabilities=frozenset({Capability.VERIFY}),
        effects=frozenset({EffectKind.TEST_RUN}),
        risk=Risk.INFO,
        description="Run checks.",
    ),
    ToolSpec(
        name="record",
        pack="fake",
        capabilities=frozenset({Capability.VCS}),
        effects=frozenset({EffectKind.VCS_COMMIT}),
        risk=Risk.SENSITIVE,
        description="Record a commit.",
    ),
)


@dataclass
class FakeCatalog:
    specs_data: tuple[ToolSpec, ...] = SPECS

    def specs(self, profile: str = "core") -> tuple[ToolSpec, ...]:
        return self.specs_data


def _spec(mode: TaskMode = TaskMode.AUTO, *, max_tokens: int | None = None) -> TaskSpec:
    return TaskSpec(
        task_id="t1",
        objective="fix the calculator",
        workspace=".",
        mode=mode,
        budget=Budget(max_tokens=max_tokens, max_steps=20),
    )


def _receipt(
    identifier: str,
    *,
    status: ReceiptStatus = ReceiptStatus.OK,
    path: str = "calc/add.py",
    error: str = "",
) -> ToolReceipt:
    return ToolReceipt(
        receipt_id=identifier,
        call_id=f"call-{identifier}",
        task_id="t1",
        tool="mutate",
        status=status,
        effects=(ReadFact(path=path, content_hash="h"),),
        error=error,
    )


def _ledger_with(receipts: int, *, failed: bool = False) -> Ledger:
    ledger = Ledger.in_memory("t1")
    for index in range(receipts):
        ledger.append_receipt(
            _receipt(f"r-{index}", path=f"file-{index}.py")
        )
    if failed:
        ledger.append_receipt(
            _receipt("failed-1", status=ReceiptStatus.FAILED, error="boom")
        )
    return ledger


def test_tool_schema_and_system_prompt_come_from_catalog() -> None:
    view = build_model_view(
        spec=_spec(), catalog=FakeCatalog(), ledger=Ledger.in_memory("t1")
    )

    assert [tool["name"] for tool in view.tools] == [
        "inspect",
        "mutate",
        "check",
        "record",
    ]
    assert "inspect" in view.system
    assert "Capability catalog" in view.system
    assert "Action protocol" in view.system


def test_generic_prompt_does_not_advertise_actions_the_parser_rejects() -> None:
    """The phase protocol is appended after this prompt; it owns the list.

    The generic prompt used to name submit_plan/update_plan/request_verification,
    none of which parse_action accepts for an actor.  A model that followed the
    prompt got ``unsupported action`` (#202 translation-layer class).
    """

    view = build_model_view(
        spec=_spec(), catalog=FakeCatalog(), ledger=Ledger.in_memory("t1")
    )
    for action in ("submit_plan", "update_plan", "request_verification"):
        assert action not in view.system


def test_catalog_swap_changes_prompt_and_hash() -> None:
    ledger = Ledger.in_memory("t1")
    first = build_model_view(
        spec=_spec(), catalog=FakeCatalog(), ledger=ledger
    )
    renamed = (
        ToolSpec(
            name="peek",
            pack="fake",
            capabilities=frozenset({Capability.READ}),
            effects=frozenset({EffectKind.READ}),
            risk=Risk.INFO,
            description="Read a file.",
        ),
    )
    second = build_model_view(
        spec=_spec(), catalog=FakeCatalog(renamed), ledger=ledger
    )

    assert first.hash != second.hash
    assert "inspect" in first.system
    assert "inspect" not in second.system
    assert "peek" in second.system


def test_projection_is_byte_stable_for_same_inputs() -> None:
    ledger = _ledger_with(3)
    first = build_model_view(
        spec=_spec(), catalog=FakeCatalog(), ledger=ledger
    )
    second = build_model_view(
        spec=_spec(), catalog=FakeCatalog(), ledger=ledger
    )
    assert isinstance(first, ModelView)
    assert first.hash == second.hash
    assert first.system == second.system
    assert first.working_set == second.working_set


def test_chat_hides_tools_and_tool_receipts() -> None:
    ledger = _ledger_with(2)
    view = build_model_view(
        spec=_spec(TaskMode.CHAT), catalog=FakeCatalog(), ledger=ledger
    )

    assert view.tools == ()
    assert all(item["kind"] != "tool_receipt" for item in view.working_set)
    assert "No tools are available" in view.system


def test_budget_keeps_debt_and_failed_receipts() -> None:
    ledger = _ledger_with(40, failed=True)
    debt = Debt(
        obligation=Obligation(
            id="o1", kind=PropositionKind.TEST_PASSED, target="tests/test_calc.py"
        ),
        reason="missing test evidence",
    )
    view = build_model_view(
        spec=_spec(max_tokens=20),
        catalog=FakeCatalog(),
        ledger=ledger,
        debt=(debt,),
    )

    failed = [
        item
        for item in view.working_set
        if item["kind"] == "tool_receipt" and item["status"] == "failed"
    ]
    assert len(failed) == 1
    assert len(view.working_set) < 41
    # Debt is per-turn state, so it is not in the cached prefix; it is carried
    # on the view and rendered by `get_state`.
    assert "o1" not in view.system
    assert view.debt == (debt,)
    assert '"id":"o1"' in render_state(view, "state", {})


def test_read_content_is_visible_but_not_raw_tool_output() -> None:
    ledger = Ledger.in_memory("t1")
    ledger.append_receipt(
        ToolReceipt(
            receipt_id="read-1",
            call_id="call-read-1",
            task_id="t1",
            tool="inspect",
            status=ReceiptStatus.OK,
            effects=(
                ReadFact(
                    path="calc/add.py",
                    content_hash="h",
                    content="def add(a, b):\n    return a + b\n",
                ),
            ),
        )
    )
    view = build_model_view(
        spec=_spec(), catalog=FakeCatalog(), ledger=ledger
    )
    content = [
        effect.get("content")
        for item in view.working_set
        for effect in item.get("effects", ())
        if effect.get("type") == "read"
    ]
    assert content == ["def add(a, b):\n    return a + b\n"]


def test_working_set_does_not_copy_raw_tool_output() -> None:
    ledger = Ledger.in_memory("t1")
    ledger.append(
        EventKind.TOOL_RECEIPT,
        EventSource.TOOL,
        {
            "receipt": {
                "receipt_id": "r1",
                "tool": "inspect",
                "status": "ok",
                "effects": [],
            },
            "raw_output": "SECRET_RAW_TOOL_OUTPUT" * 1000,
        },
    )
    view = build_model_view(
        spec=_spec(), catalog=FakeCatalog(), ledger=ledger
    )
    assert "SECRET_RAW_TOOL_OUTPUT" not in repr(view.working_set)


def test_a_state_answer_is_visible_only_after_the_event_it_accompanies() -> None:
    """The turn renders ``state_read`` rows that *follow* its event.

    That is what keeps an answer beside the receipt instead of replacing it --
    and it is also a hard ordering requirement on the writer: an answer written
    before the receipt is ledgered and never shown.  A measured run asked "am I
    done?" every other turn, never received an answer, and re-read the same two
    files until the budget ran out.
    """

    from xenon.harness.llm import current_turn
    from xenon.kernel.view.base import trailing_state_reads

    ledger = Ledger.in_memory("t1")
    ledger.append(
        EventKind.TOOL_RECEIPT,
        EventSource.TOOL,
        {"receipt": {"tool": "inspect", "status": "ok", "effects": []}},
    )
    before = ledger.append(
        EventKind.STATE_READ, EventSource.LLM, {"text": "TOO EARLY"}
    )
    after = ledger.append(
        EventKind.STATE_READ, EventSource.LLM, {"text": "ANSWER"}
    )

    view = build_model_view(
        spec=_spec(), catalog=FakeCatalog(), ledger=ledger
    )
    shown = trailing_state_reads(view.working_set)

    # Both rows sit after the receipt, so both are part of this turn.  The
    # ordering rule is about the *receipt*, not about which answer is newest:
    # the turn is ``latest_respondable`` -- a row no model can act on never
    # becomes the turn -- and every state answer after it rides alongside.
    assert [item["seq"] for item in shown] == [before.seq, after.seq]
    turn = current_turn(view, instruction="go")
    assert "ANSWER" in turn
    assert "TOO EARLY" in turn
