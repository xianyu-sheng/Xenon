"""Session-stable MCP tool contract: append-only, ordered, capped."""

from __future__ import annotations

from types import SimpleNamespace

from xenon.kernel.contracts import (
    Capability,
    EffectKind,
    ReceiptStatus,
    Risk,
    ToolReceipt,
    ToolSpec,
)
from xenon.ledger.store import Ledger
from xenon.repl.runtime_adapter import (
    _remember_expanded_tools,
    _session_tool_contract,
)
from xenon.repl.session_tools import SessionToolContract


def _specs(*names: str) -> tuple[ToolSpec, ...]:
    return tuple(
        ToolSpec(
            name=name,
            pack="mcp",
            capabilities=frozenset({Capability.REMOTE}),
            effects=frozenset({EffectKind.NETWORK}),
            risk=Risk.SENSITIVE,
            params_schema={"query": "string"},
            description=f"{name} tool",
        )
        for name in names
    )


def test_a_later_task_appends_without_reordering_earlier_tools() -> None:
    contract = SessionToolContract(session_id="s1")
    first = contract.select(
        _specs("github:alpha", "github:beta", "weather:forecast"),
        "alpha",
        limit=1,
    )
    assert [spec.name for spec in first] == ["github:alpha"]

    second = contract.select(
        _specs("github:alpha", "github:beta", "weather:forecast"),
        "forecast",
        limit=1,
    )
    names = [spec.name for spec in second]
    # The first task's prefix is unchanged; the new tool is at the tail.
    assert names[:1] == ["github:alpha"]
    assert names[1] == "weather:forecast"
    assert contract.last_added == ("weather:forecast",)


def test_an_unrelated_task_does_not_grow_the_contract() -> None:
    """A weak selection must not become a permanent tool-block change.

    Live measurement: a task that only ran ``print(11*12)`` appended eight
    unrelated GitHub tools to the session contract, so every later task paid
    for a rewritten tool tail.  The contract now grows only when the objective
    names the tool; a task that names nothing leaves it byte-identical.
    """

    contract = SessionToolContract(session_id="s1", cap=8)
    specs = _specs("github:alpha", "github:beta", "github:gamma", "github:delta")

    first = contract.select(specs, "run print 11 * 12", limit=3)
    assert first == ()
    assert contract.last_added == ()

    # A task with real evidence appends only the tool it justified -- not the
    # rest of the canonical budget around it.
    second = contract.select(specs, "gamma", limit=3)
    assert contract.last_added == ("github:gamma",)
    assert [spec.name for spec in second] == ["github:gamma"]

    # And a later task with no evidence keeps that exact block.
    third = contract.select(specs, "run print 99 * 7", limit=3)
    assert [spec.name for spec in third] == ["github:gamma"]
    assert contract.last_added == ()


def test_the_contract_keeps_the_first_schema_it_showed() -> None:
    contract = SessionToolContract(session_id="s1")
    contract.select(_specs("github:alpha"), "alpha", limit=1)
    original = contract._specs["github:alpha"]
    changed = (
        ToolSpec(
            name="github:alpha",
            pack="mcp",
            capabilities=frozenset({Capability.REMOTE}),
            effects=frozenset({EffectKind.NETWORK}),
            risk=Risk.SENSITIVE,
            params_schema={"different": "schema"},
            description="rewritten",
        ),
    )
    current = contract.select(changed, "alpha", limit=1)
    assert current[0] is original


def test_the_cap_evicts_the_least_recently_selected_not_this_task() -> None:
    contract = SessionToolContract(session_id="s1", cap=2)
    everything = _specs("github:one", "github:two", "github:three", "github:aux")
    contract.select(everything, "one", limit=1)
    contract.select(everything, "two", limit=1)
    assert contract.names == ("github:one", "github:two")

    # Selecting a third tool must evict the oldest (github:one), never
    # github:two (selected last task) or github:three (selected now).
    current = contract.select(everything, "three", limit=1)
    assert [spec.name for spec in current] == ["github:two", "github:three"]


def test_the_allowlist_prunes_tools_outside_it() -> None:
    contract = SessionToolContract(session_id="s1")
    contract.select(
        _specs("github:alpha", "weather:forecast"), "alpha forecast", limit=2
    )
    assert contract.names == ("github:alpha", "weather:forecast")

    current = contract.select(
        _specs("github:alpha", "weather:forecast"),
        "anything",
        limit=2,
        allowed=("weather:forecast",),
    )
    assert [spec.name for spec in current] == ["weather:forecast"]


def test_expansion_is_session_sticky_but_only_for_known_tools() -> None:
    contract = SessionToolContract(session_id="s1")
    contract.select(_specs("github:alpha"), "alpha", limit=1)
    contract.mark_expanded(["github:alpha", "not-in-the-contract"])
    assert contract.expanded == {"github:alpha"}

    # A tool that leaves the contract cannot stay expanded.
    contract.select(
        _specs("weather:forecast"),
        "forecast",
        limit=1,
        allowed=("weather:forecast",),
    )
    assert "github:alpha" not in contract.expanded


def test_the_repl_keeps_one_contract_per_session() -> None:
    class _Events:
        session_id = "s1"

    repl = SimpleNamespace(_session_events=_Events(), agent_context=None)
    first = _session_tool_contract(repl)
    assert first is not None
    assert _session_tool_contract(repl) is first

    repl._session_events = SimpleNamespace(session_id="s2")
    second = _session_tool_contract(repl)
    assert second is first, "the contract object is reused"
    assert second.session_id == "s2"
    assert second.names == ()


def test_a_receipt_marks_the_tools_schema_as_session_expanded(tmp_path) -> None:
    contract = SessionToolContract(session_id="s1")
    contract.select(_specs("github:alpha"), "alpha", limit=1)

    path = tmp_path / "t.jsonl"
    ledger = Ledger(path, "t1")
    ledger.append_receipt(
        ToolReceipt(
            receipt_id="r1",
            call_id="c1",
            task_id="t1",
            tool="github:alpha",
            status=ReceiptStatus.OK,
            effects=(),
        )
    )
    run = SimpleNamespace(ledger_path=path, task_id="t1")
    _remember_expanded_tools(contract, run)

    assert contract.expanded == {"github:alpha"}


def test_a_resumed_plan_sees_the_same_session_tool_set(tmp_path, monkeypatch) -> None:
    """An approved plan executes in the session that planned it.

    Before this, ``RuntimeAdapter.resume`` passed no ``extra_specs`` at all:
    the planner could name ``github:forecast`` and the executor's catalog
    only held the generic ``mcp_call``, so the step could not bind.  The
    resumed call must also return the *whole* contract, not just the tools
    this objective would select, or the tool block is rebuilt mid-session.
    """

    from xenon.repl.runtime_adapter import RuntimeAdapter

    monkeypatch.setenv("XENON_MCP_TOOL_LIMIT", "1")
    registry = SimpleNamespace(
        tool_map={
            "github:forecast": {
                "description": "forecast",
                "inputSchema": {"properties": {"q": {"description": "query"}}},
            },
            "weather:alert": {
                "description": "alerts",
                "inputSchema": {"properties": {"city": {"description": "city"}}},
            },
        }
    )
    adapter = RuntimeAdapter(
        workspace=tmp_path,
        model_config=SimpleNamespace(model_id="test/model"),
        mcp_registry=registry,
        catalog_profile="core+mcp",
    )
    contract = SessionToolContract(session_id="s1")
    adapter.tool_contract = contract

    planned, added = adapter.mcp_extra_specs("forecast tomorrow")
    assert [spec.name for spec in planned] == ["github:forecast"]
    assert added == ["github:forecast"]

    resumed, resumed_added = adapter.mcp_extra_specs("check city alerts")
    assert [spec.name for spec in resumed] == ["github:forecast", "weather:alert"]
    assert resumed_added == ["weather:alert"]
    # A second resume of the same plan adds nothing and keeps the order.
    again, again_added = adapter.mcp_extra_specs("forecast tomorrow")
    assert [spec.name for spec in again] == [spec.name for spec in resumed]
    assert again_added == []


def test_the_frozen_request_envelope_is_not_tool_evidence(tmp_path, monkeypatch) -> None:
    """A session contract grows from the human's words, not from bookkeeping.

    The request envelope prepends standing context and learned lessons.  Its
    JSON keys and quoted paths are not vocabulary the operator used: scoring
    them selected thirteen GitHub PR tools for "run python -c print(11*12)"
    because ``session_created_files`` matched ``push_files``.  The same tool
    is still selected when the user actually asks for it.
    """

    from xenon.repl.runtime_adapter import RuntimeAdapter

    monkeypatch.setenv("XENON_MCP_TOOL_LIMIT", "5")
    registry = SimpleNamespace(
        tool_map={
            "github:push_files": {
                "description": "push files to a branch",
                "inputSchema": {"properties": {"files": {"description": "files"}}},
            },
            "github:create_pull_request": {
                "description": "create a pull request",
                "inputSchema": {"properties": {"title": {"description": "title"}}},
            },
        }
    )
    adapter = RuntimeAdapter(
        workspace=tmp_path,
        model_config=SimpleNamespace(model_id="test/model"),
        mcp_registry=registry,
        catalog_profile="core+mcp",
    )
    contract = SessionToolContract(session_id="s1")
    adapter.tool_contract = contract

    envelope = (
        "[Xenon 本轮上下文快照]\n"
        '{"session_created_files": [], "session_modified_files": []}\n\n'
        "[Xenon 相关记忆]\n"
        "- worktree contains unverified paths: frontend/src/links.ts\n\n"
        "## 当前请求\n"
        '用 shell 运行 python -c "print(11*12)"，然后告诉我输出。'
    )
    selected, added = adapter.mcp_extra_specs(
        envelope, '用 shell 运行 python -c "print(11*12)"，然后告诉我输出。'
    )
    assert selected == ()
    assert added == []
    assert contract.names == ()

    selected, added = adapter.mcp_extra_specs(
        "push my files to github", "push my files to github"
    )
    assert [spec.name for spec in selected] == ["github:push_files"]
    assert added == ["github:push_files"]


def test_reset_clears_the_contract_for_a_new_session() -> None:
    contract = SessionToolContract(session_id="s1")
    contract.select(_specs("github:alpha"), "alpha", limit=1)
    contract.mark_expanded(["github:alpha"])

    contract.reset("s2")
    assert contract.session_id == "s2"
    assert contract.names == ()
    assert contract.expanded == set()
    assert contract.last_added == ()
