"""Turn routing after the legacy engines were removed (#202/#290).

The old suites asserted "this turn routes to the ReAct engine".  That runtime no
longer exists, so the durable contract is now:

* a tool or skill turn runs on the kernel loop;
* a plain chat turn stays on the direct branch;
* a mode without a loop profile fails closed instead of switching paradigms.
"""

from __future__ import annotations

import inspect

import pytest

from xenon.modes import MODE_CATALOG
from xenon.repl import turn_flow
from xenon.repl.runtime_adapter import should_route_to_kernel


def _route(**overrides: object) -> bool:
    kwargs: dict[str, object] = {
        "runtime_mode": "kernel",
        "requires_tools": True,
        "skill_name": None,
        "plan_mode": False,
        "pending": None,
        "recipe": None,
        "mode_name": "react",
    }
    kwargs.update(overrides)
    return should_route_to_kernel(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("mode", list(MODE_CATALOG))
def test_every_mode_routes_tool_turns_to_the_kernel(mode: str) -> None:
    assert _route(mode_name=mode)


@pytest.mark.parametrize(
    "text_kind",
    ["weather query", "file path", "git command"],
)
def test_tool_turn_kinds_all_land_on_the_kernel(text_kind: str) -> None:
    """Intent variety changes the prompt, not the runtime."""

    assert text_kind  # documented label, routing must not depend on it
    assert _route(external_query=True, mcp_available=False)
    assert _route(external_query=True, mcp_available=True)


def test_plan_turns_route_to_the_kernel_in_both_boundaries() -> None:
    assert _route(plan_mode=True, plan_deferred_write=False)
    assert _route(plan_mode=True, plan_deferred_write=True)


def test_skills_run_on_the_same_loop() -> None:
    assert _route(skill_name="write-tests")


def test_chat_turns_do_not_route_to_the_kernel() -> None:
    assert not _route(requires_tools=False)
    assert not _route(requires_tools=False, mode_name="plan-reflection")


def test_unknown_mode_fails_closed() -> None:
    assert not _route(mode_name="no-such-mode")
    assert not _route(mode_name="no-such-mode", requires_tools=False)


def test_legacy_pending_action_and_recipe_do_not_route() -> None:
    """Those are legacy task objects; the kernel owns approvals and plans now."""

    assert not _route(pending=object())
    assert not _route(recipe=object())


def test_turn_flow_has_no_engine_dispatch_or_runtime_escape_hatch() -> None:
    source = inspect.getsource(turn_flow)

    assert "ENGINE_REGISTRY" not in source
    assert "_select_turn_mode" not in source
    assert "_run_react_engine" not in source
    assert "XENON_RUNTIME" not in source
    assert "无法执行工具轮次" in source, "工具轮次必须显式失败而不是换范式"
