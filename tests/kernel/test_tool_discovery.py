from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import xenon.kernel  # noqa: F401 — ledger<->kernel import cycle
from xenon.catalog.catalog import build_catalog
from xenon.catalog.gateway import LegacyToolGateway
from xenon.kernel.contracts import (
    Budget,
    Capability,
    EffectKind,
    Risk,
    TaskSpec,
    ToolCall,
    ToolSpec,
)
from xenon.kernel.policies import PermissionMode, PermissionPolicy
from xenon.ledger.events import receipt_from_payload, receipt_to_payload

# ── catalog scoring ─────────────────────────────────────────────────


def test_an_exact_tool_name_wins() -> None:
    catalog = build_catalog("core")
    matches, truncated, fallback = catalog.discover("read")

    assert [item["name"] for item in matches] == ["read"]
    assert truncated is False


def test_intent_words_rank_the_matching_tool_first() -> None:
    catalog = build_catalog("core")
    matches, _, _ = catalog.discover("search the workspace for files", limit=3)

    assert matches[0]["name"] == "search"


def test_capability_filter_restricts_candidates() -> None:
    catalog = build_catalog("core")
    matches, _, _ = catalog.discover("anything", capability="edit", limit=5)

    assert all("edit" in item["capabilities"] for item in matches)
    assert any(item["name"] == "apply_patch" for item in matches)


def test_results_are_bounded_and_report_truncation() -> None:
    catalog = build_catalog("core")
    matches, truncated, _ = catalog.discover("a b c d e f", limit=2)

    assert len(matches) == 2
    assert truncated is True


def test_matches_carry_full_parameters() -> None:
    catalog = build_catalog("core")
    matches, _, _ = catalog.discover("read")

    assert "file_path" in matches[0]["parameters"]


def test_an_unknown_capability_returns_nothing() -> None:
    catalog = build_catalog("core")
    matches, truncated, fallback = catalog.discover("x", capability="not-real")

    assert matches == () and truncated is False


# ── gateway receipt ─────────────────────────────────────────────────


def _gateway(ws: Path, mode: PermissionMode) -> LegacyToolGateway:
    catalog = build_catalog("core")
    return LegacyToolGateway(
        specs=catalog.specs(),
        workspace=ws,
        policy=PermissionPolicy(mode=mode, workspace=str(ws)),
        catalog=catalog,
    )


class _Services:
    task_id = "t1"
    current_step_id = None

    def __init__(self, workspace: str) -> None:
        self.workspace = workspace


@pytest.mark.parametrize(
    "mode",
    (PermissionMode.READ_ONLY, PermissionMode.AUTO_EDIT, PermissionMode.BYPASS),
)
def test_find_tools_is_free_in_every_mode(mode: PermissionMode) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        gateway = _gateway(Path(tmp), mode)
        call = ToolCall(
            spec_name="find_tools",
            params={"query": "edit a file", "capability": "edit", "limit": 3},
            capability=Capability.DISCOVER,
        )

        receipt = gateway.execute(call, _Services(tmp))

    assert receipt.status.value == "ok"
    effect = receipt.effects[0]
    assert effect.kind is EffectKind.TOOL_DISCOVERY
    assert any(item["name"] == "apply_patch" for item in effect.matches)


def test_find_tools_roundtrips_through_the_payload() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        gateway = _gateway(Path(tmp), PermissionMode.AUTO_EDIT)
        call = ToolCall(
            spec_name="find_tools",
            params={"query": "read a file"},
            capability=Capability.DISCOVER,
        )
        receipt = gateway.execute(call, _Services(tmp))

    payload = receipt_to_payload(receipt)
    rebuilt = receipt_from_payload(payload)

    assert rebuilt.effects[0].count == receipt.effects[0].count
    assert rebuilt.effects[0].matches[0]["name"] == "read"


def test_a_named_remote_call_injects_tool_name() -> None:
    """The native function name is the remote tool; the model must not repeat
    it inside arguments to make binding work."""

    from xenon.catalog.catalog import ToolCatalog
    from xenon.harness.llm import action_from_tool_call
    from xenon.kernel.view import build_model_view
    from xenon.ledger.store import Ledger

    remote = ToolSpec(
        name="zpx_alpha",
        pack="plugin",
        capabilities=frozenset({Capability.REMOTE}),
        effects=frozenset({EffectKind.NETWORK}),
        risk=Risk.SENSITIVE,
        params_schema={"endpoint_id": "endpoint", "region": "region"},
        description="convert markdown to pdf",
    )
    with tempfile.TemporaryDirectory() as tmp:
        catalog = ToolCatalog(
            tuple(build_catalog("core").specs("all")) + (remote,), "all"
        )
        view = build_model_view(
            spec=TaskSpec(task_id="t1", objective="x", workspace=tmp, budget=Budget()),
            catalog=catalog,
            ledger=Ledger.in_memory("t1"),
        )
        action = action_from_tool_call(
            view, "zpx_alpha", {"endpoint_id": "ep1", "region": "us"}
        )

    assert action.intent.params.get("tool_name") == "zpx_alpha"
    # The business arguments must be nested where the executor entry reads
    # them; flattening lost every one (ToolNode swallowed known kwargs and
    # raised on unknown ones, so a real MCP call could never carry arguments).
    assert action.intent.params.get("tool_args") == {
        "endpoint_id": "ep1",
        "region": "us",
    }


def test_find_tools_without_a_catalog_still_receipts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        catalog = build_catalog("core")
        gateway = LegacyToolGateway(
            specs=catalog.specs(),
            workspace=Path(tmp),
            policy=PermissionPolicy(
                mode=PermissionMode.BYPASS, workspace=tmp
            ),
            catalog=None,
        )
        call = ToolCall(
            spec_name="find_tools",
            params={"query": "x"},
            capability=Capability.DISCOVER,
        )

        receipt = gateway.execute(call, _Services(tmp))

    assert receipt.status.value == "ok"
    assert receipt.effects[0].count == 0


def test_a_zero_match_search_is_marked_fallback() -> None:
    """Candidates are still returned, but the caller is told they are guesses."""

    catalog = build_catalog("core")
    matches, _, fallback = catalog.discover("qqqqzzzz vvvvwww")

    assert fallback is True
    assert matches, "the fallback listing still offers candidates"


def test_a_zero_match_receipt_carries_the_note() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        gateway = _gateway(Path(tmp), PermissionMode.AUTO_EDIT)
        call = ToolCall(
            spec_name="find_tools",
            params={"query": "qqqqzzzz vvvvwww"},
            capability=Capability.DISCOVER,
        )
        receipt = gateway.execute(call, _Services(tmp))

    effect = receipt.effects[0]
    assert effect.matches
    assert "No tool matched" in effect.note


def test_an_unknown_capability_filter_says_so() -> None:
    """Zero matches with a bad filter must not look like "no such tool".

    ``discover`` fails closed on an unparseable capability, but silence left
    the model concluding the capability did not exist at all.
    """

    with tempfile.TemporaryDirectory() as tmp:
        gateway = _gateway(Path(tmp), PermissionMode.AUTO_EDIT)
        call = ToolCall(
            spec_name="find_tools",
            params={"query": "anything", "capability": "not-a-capability"},
            capability=Capability.DISCOVER,
        )
        receipt = gateway.execute(call, _Services(tmp))

    effect = receipt.effects[0]
    assert effect.matches == ()
    assert "Unknown capability filter" in effect.note
