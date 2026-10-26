"""Tests for capability binding and packs."""

from __future__ import annotations

import pytest

from xenon.catalog.catalog import ToolCatalog, build_catalog
from xenon.catalog.specs import CORE_SPECS
from xenon.kernel.contracts import (
    BindingStatus,
    Capability,
    EffectKind,
    Risk,
    ToolIntent,
    ToolSpec,
)


def test_core_catalog_binds_each_capability_to_one_core_tool() -> None:
    catalog = build_catalog("core")
    expected = {
        Capability.READ: "read",
        Capability.SEARCH: "search",
        Capability.EDIT: "apply_patch",
        Capability.VERIFY: "bash",
        Capability.VCS: "bash",
        Capability.SHELL: "bash",
    }
    for capability, name in expected.items():
        binding = catalog.bind(ToolIntent(capability=capability))
        assert binding.status is BindingStatus.BOUND
        assert binding.call is not None
        assert binding.call.spec_name == name


def test_core_catalog_has_no_remote_tool() -> None:
    binding = build_catalog("core").bind(
        ToolIntent(capability=Capability.REMOTE)
    )
    assert binding.status is BindingStatus.UNRESOLVED


def test_prefer_is_a_non_binding_hint() -> None:
    catalog = build_catalog("core")
    binding = catalog.bind(
        ToolIntent(capability=Capability.EDIT, prefer="apply_patch")
    )
    assert binding.call is not None
    assert binding.call.spec_name == "apply_patch"
    # An unavailable preference falls back to the capability binding.
    fallback = catalog.bind(
        ToolIntent(capability=Capability.EDIT, prefer="no-such-tool")
    )
    assert fallback.call is not None
    assert fallback.call.spec_name == "apply_patch"


def test_equal_preferences_are_ambiguous_not_silent() -> None:
    specs = (
        ToolSpec(
            name="left",
            pack="core",
            capabilities=frozenset({Capability.EDIT}),
            effects=frozenset({EffectKind.FS_WRITE}),
            risk=Risk.WRITE,
            preferences={"edit": 1},
        ),
        ToolSpec(
            name="right",
            pack="core",
            capabilities=frozenset({Capability.EDIT}),
            effects=frozenset({EffectKind.FS_WRITE}),
            risk=Risk.WRITE,
            preferences={"edit": 1},
        ),
    )
    binding = ToolCatalog(specs, "core").bind(
        ToolIntent(capability=Capability.EDIT)
    )
    assert binding.status is BindingStatus.AMBIGUOUS
    assert binding.candidates == ("right", "left")


def test_legacy_catalog_prefers_structured_adapters() -> None:
    catalog = build_catalog("legacy")
    assert catalog.bind(
        ToolIntent(capability=Capability.VERIFY)
    ).call.spec_name == "run_tests"
    assert catalog.bind(
        ToolIntent(capability=Capability.VCS)
    ).call.spec_name == "git"


def test_unknown_profile_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown tool profile"):
        build_catalog("nope")


def test_core_specs_are_small_and_capability_complete() -> None:
    assert len(CORE_SPECS) <= 7
    covered = set()
    for spec in CORE_SPECS:
        covered.update(spec.capabilities)
    assert {
        Capability.READ,
        Capability.SEARCH,
        Capability.EDIT,
        Capability.VERIFY,
        Capability.VCS,
        Capability.SHELL,
    } <= covered
