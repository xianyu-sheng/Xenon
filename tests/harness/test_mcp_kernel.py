"""Read-only MCP slice through the kernel (#230)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from xenon.catalog.catalog import build_catalog
from xenon.engine.context import AgentContext
from xenon.harness.runner import run_task
from xenon.kernel.contracts import (
    CallTool,
    Capability,
    Finish,
    TaskSpec,
    ToolIntent,
)
from xenon.kernel.view import build_model_view
from xenon.ledger.events import EventKind
from xenon.ledger.store import Ledger


class FakeMCPRegistry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict:
        self.calls.append((tool_name, arguments))
        return {
            "content": [
                {"type": "text", "text": "MCP result: 3 repositories found"}
            ]
        }


class _Approval:
    def __init__(self) -> None:
        self.calls = 0

    def request(self, view, action, decision) -> bool:
        self.calls += 1
        return True


class _Strategy:
    def __init__(self) -> None:
        self.actions = [
            CallTool(
                ToolIntent(
                    capability=Capability.REMOTE,
                    params={
                        "tool_name": "github:search_repositories",
                        "tool_args": {"query": "xenon"},
                    },
                )
            ),
            Finish("used MCP"),
        ]

    def propose(self, view):
        return self.actions.pop(0)


def test_core_mcp_catalog_exposes_remote_only_on_request() -> None:
    core = build_catalog("core")
    with_mcp = build_catalog("core+mcp")
    assert "mcp_call" not in [spec.name for spec in core.specs()]
    assert "mcp_call" in [spec.name for spec in with_mcp.specs()]
    bound = with_mcp.bind(
        ToolIntent(capability=Capability.REMOTE, params={"tool_name": "x"})
    )
    assert bound.status.value == "bound"
    assert bound.call is not None
    assert bound.call.spec_name == "mcp_call"


def test_mcp_call_executes_through_gateway_and_exposes_content(
    tmp_path: Path,
) -> None:
    registry = FakeMCPRegistry()
    context = AgentContext({"_mcp_registry": registry})
    approval = _Approval()
    run = run_task(
        "query github through MCP",
        strategy=_Strategy(),
        workspace=tmp_path,
        ledger_dir=tmp_path.parent / f"{tmp_path.name}-ledger",
        task_id="mcp-1",
        finalize=False,
        approval=approval,
        allow_approval=True,
        catalog_profile="core+mcp",
        gateway_context=context,
    )

    assert run.result.accepted is True, run.result.debt
    assert registry.calls == [
        ("github:search_repositories", {"query": "xenon"})
    ]
    events = Ledger(run.ledger_path, "mcp-1").read()
    approval_event = next(
        event for event in events if event.kind is EventKind.USER_APPROVAL
    )
    assert approval_event.payload["approved"] is True
    assert "github:search_repositories" in approval_event.payload["scope"]
    receipt = next(
        event for event in events if event.kind is EventKind.TOOL_RECEIPT
    )
    effects = receipt.payload["receipt"]["effects"]
    network = next(item for item in effects if item["type"] == "network")
    assert network["url"] == "mcp://github:search_repositories"
    assert "3 repositories" in network["content"]


def test_mcp_result_is_a_network_fact_for_the_model(tmp_path: Path) -> None:
    registry = FakeMCPRegistry()
    context = AgentContext({"_mcp_registry": registry})
    run = run_task(
        "query github through MCP",
        strategy=_Strategy(),
        workspace=tmp_path,
        ledger_dir=tmp_path.parent / f"{tmp_path.name}-ledger",
        task_id="mcp-2",
        finalize=False,
        approval=_Approval(),
        allow_approval=True,
        catalog_profile="core+mcp",
        gateway_context=context,
    )
    ledger = Ledger(run.ledger_path, "mcp-2")
    facts = [
        item
        for event in ledger.read()
        if event.kind is EventKind.TOOL_RECEIPT
        for item in event.payload["receipt"]["effects"]
        if item["type"] == "network"
    ]
    assert facts
    assert "MCP result" in facts[0]["content"]

    view = build_model_view(
        spec=TaskSpec(
            task_id="mcp-2",
            objective="query",
            workspace=str(tmp_path),
        ),
        catalog=build_catalog("core+mcp"),
        ledger=ledger,
    )
    visible = [
        effect.get("content")
        for item in view.working_set
        for effect in item.get("effects", ())
        if effect.get("type") == "network"
    ]
    assert any("MCP result" in str(item) for item in visible)


def _per_tool_specs():
    from xenon.catalog.specs import mcp_tool_specs

    return mcp_tool_specs(
        {
            "github:search_repositories": (
                "github",
                {
                    "name": "search_repositories",
                    "description": "Search GitHub repositories.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                    },
                },
            )
        }
    )


def test_per_tool_specs_bind_exact_and_reject_unknown() -> None:
    catalog = build_catalog("core+mcp", extra_specs=_per_tool_specs())
    bound = catalog.bind(
        ToolIntent(
            capability=Capability.REMOTE,
            params={"tool_name": "github:search_repositories"},
        )
    )
    assert bound.status.value == "bound"
    assert bound.call is not None
    assert bound.call.spec_name == "github:search_repositories"
    assert bound.call.capability is Capability.REMOTE
    assert any(spec.name == "github:search_repositories" for spec in catalog.specs())

    unknown = catalog.bind(
        ToolIntent(
            capability=Capability.REMOTE,
            params={"tool_name": "github:delete_repository"},
        )
    )
    assert unknown.status.value == "unresolved"
    assert unknown.call is None

    missing = catalog.bind(
        ToolIntent(capability=Capability.REMOTE, params={})
    )
    assert missing.status.value == "unresolved"
    assert "tool_name" in missing.reason


def test_per_tool_mcp_receipt_keeps_real_tool_name(tmp_path: Path) -> None:
    registry = FakeMCPRegistry()
    context = AgentContext({"_mcp_registry": registry})
    strategy = _Strategy()
    strategy.actions[0] = CallTool(
        ToolIntent(
            capability=Capability.REMOTE,
            params={
                "tool_name": "github:search_repositories",
                "tool_args": {"query": "xenon"},
            },
        )
    )
    run = run_task(
        "query github through MCP",
        strategy=strategy,
        workspace=tmp_path,
        ledger_dir=tmp_path.parent / f"{tmp_path.name}-ledger",
        task_id="mcp-per-tool",
        finalize=False,
        approval=_Approval(),
        allow_approval=True,
        catalog_profile="core+mcp",
        gateway_context=context,
        extra_specs=_per_tool_specs(),
    )

    assert run.result.accepted is True, run.result.debt
    assert registry.calls == [
        ("github:search_repositories", {"query": "xenon"})
    ]
    events = Ledger(run.ledger_path, "mcp-per-tool").read()
    receipt = next(
        event for event in events if event.kind is EventKind.TOOL_RECEIPT
    )
    assert receipt.payload["receipt"]["tool"] == "github:search_repositories"
    network = next(
        item
        for item in receipt.payload["receipt"]["effects"]
        if item["type"] == "network"
    )
    assert network["url"] == "mcp://github:search_repositories"
    approval_event = next(
        event for event in events if event.kind is EventKind.USER_APPROVAL
    )
    assert "github:search_repositories" in approval_event.payload["scope"]
