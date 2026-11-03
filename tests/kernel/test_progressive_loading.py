"""Progressive loading (#370): MCP tools start as a one-line index and expand
once executed; the index truncates with a visible count; unknown tools fail
closed at binding; ``tool_schema`` answers from the catalog.

The core coding tools stay full (they are few and central); MCP is where the
prompt used to explode, so MCP is where the index lives.
"""

from __future__ import annotations

from dataclasses import dataclass

from xenon.catalog.catalog import build_catalog
from xenon.kernel.contracts import (
    Budget,
    Capability,
    EffectKind,
    ReceiptStatus,
    Risk,
    TaskMode,
    TaskSpec,
    ToolIntent,
    ToolReceipt,
    ToolSpec,
)
from xenon.kernel.view import build_model_view
from xenon.ledger.store import Ledger


def _mcp_specs(count: int) -> tuple[ToolSpec, ...]:
    return tuple(
        ToolSpec(
            name=f"github:tool_{index:02d}",
            pack="mcp",
            capabilities=frozenset({Capability.REMOTE}),
            effects=frozenset({EffectKind.NETWORK}),
            risk=Risk.SENSITIVE,
            params_schema={
                "tool_args": "object",
                "query": "optional filter",
            },
            description=(
                f"GitHub tool number {index} with a longer description that "
                "goes on and on to prove the index one-liner truncates it."
            ),
        )
        for index in range(count)
    )


@dataclass
class _Catalog:
    specs_data: tuple[ToolSpec, ...]

    def specs(self, profile: str = "core") -> tuple[ToolSpec, ...]:
        return self.specs_data


def _spec() -> TaskSpec:
    return TaskSpec(
        task_id="t1",
        objective="query github",
        workspace=".",
        mode=TaskMode.AUTO,
        budget=Budget(max_tokens=4000, max_steps=20),
    )


def _receipt(tool: str) -> ToolReceipt:
    return ToolReceipt(
        receipt_id=f"r-{tool}",
        call_id="call-1",
        task_id="t1",
        tool=tool,
        status=ReceiptStatus.OK,
        effects=(),
    )


# ── the index ─────────────────────────────────────────────────────


def test_mcp_tools_are_index_rows_without_parameters() -> None:
    view = build_model_view(
        spec=_spec(),
        catalog=_Catalog((_mcp_specs(2))),
        ledger=Ledger.in_memory("t1"),
    )

    rows = {tool["name"]: tool for tool in view.tools}
    assert rows["github:tool_00"]["index"] is True
    assert "parameters" not in rows["github:tool_00"]
    # The one-liner truncates the long description.
    assert len(rows["github:tool_00"]["description"]) <= 121


def test_a_core_tool_stays_full() -> None:
    catalog = build_catalog("core")
    view = build_model_view(
        spec=_spec(),
        catalog=catalog,
        ledger=Ledger.in_memory("t1"),
    )

    rows = {tool["name"]: tool for tool in view.tools}
    assert "parameters" in rows["read"]
    assert rows["read"].get("index") is None


# ── expansion from the ledger ─────────────────────────────────────


def test_an_executed_mcp_tool_expands_for_later_views() -> None:
    ledger = Ledger.in_memory("t1")
    ledger.append_receipt(_receipt("github:tool_01"))

    view = build_model_view(
        spec=_spec(),
        catalog=_Catalog(_mcp_specs(3)),
        ledger=ledger,
    )

    rows = {tool["name"]: tool for tool in view.tools}
    assert rows["github:tool_01"]["parameters"] == {
        "tool_args": "object",
        "query": "optional filter",
    }
    # The ones that never ran stay index rows.
    assert rows["github:tool_00"].get("index") is True
    assert "parameters" not in rows["github:tool_00"]


# ── truncation with a visible count ───────────────────────────────


def test_the_index_truncates_and_counts() -> None:
    view = build_model_view(
        spec=_spec(),
        catalog=_Catalog(_mcp_specs(45)),
        ledger=Ledger.in_memory("t1"),
    )

    assert "github:tool_00" in view.system
    assert "github:tool_44" not in view.system
    assert "5 more tools in remote" in view.system


def test_a_small_surface_is_not_truncated() -> None:
    view = build_model_view(
        spec=_spec(),
        catalog=_Catalog(_mcp_specs(3)),
        ledger=Ledger.in_memory("t1"),
    )

    assert "github:tool_02" in view.system
    assert "more tools" not in view.system


# ── fail closed at binding ────────────────────────────────────────


def test_an_unlisted_tool_cannot_be_called() -> None:
    """A tool truncated out of the catalog does not exist for the kernel.

    ``build_catalog`` receives only the bounded subset (the adapter already
    limits via ``select_mcp_tool_specs``); anything beyond it binds as
    UNRESOLVED, so a model can never "guess a name" into existence.
    """

    from xenon.kernel.contracts import BindingStatus

    catalog = build_catalog("core+mcp", extra_specs=_mcp_specs(2))
    intent = ToolIntent(
        capability=Capability.REMOTE,
        params={"tool_name": "github:tool_99", "tool_args": {}},
    )
    binding = catalog.bind(intent)

    assert binding.status is BindingStatus.UNRESOLVED


def test_a_listed_tool_binds() -> None:
    from xenon.kernel.contracts import BindingStatus

    catalog = build_catalog("core+mcp", extra_specs=_mcp_specs(2))
    intent = ToolIntent(
        capability=Capability.REMOTE,
        params={"tool_name": "github:tool_00", "tool_args": {"query": "x"}},
    )
    binding = catalog.bind(intent)

    assert binding.status is BindingStatus.BOUND
