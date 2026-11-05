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
        # The structured adapters win their capability so a test run or a git
        # read records a real fact instead of an opaque shell receipt.
        Capability.VERIFY: "test",
        Capability.VCS: "git",
        Capability.SHELL: "bash",
    }
    # A target-requiring capability needs a named target to bind at all (#405);
    # this test is about the capability→tool mapping, so name one.
    _TARGET = {"file_path": "a.py", "files": ["a.py"], "command": "true",
               "pattern": "x", "test_paths": ["tests"]}

    for capability, name in expected.items():
        binding = catalog.bind(
            ToolIntent(
                capability=capability,
                params={k: v for k, v in _TARGET.items()},
                targets=("a.py",),
            )
        )
        assert binding.status is BindingStatus.BOUND
        assert binding.call is not None
        assert binding.call.spec_name == name
        assert binding.call.capability is capability


def test_core_catalog_can_reach_the_result_reader_by_preference() -> None:
    """A truncated result is read by result_id, not by re-running the tool."""

    catalog = build_catalog("core")
    general = catalog.bind(
        ToolIntent(capability=Capability.READ, params={"file_path": "a.py"})
    )
    assert general.call is not None and general.call.spec_name == "read"

    expanded = catalog.bind(
        ToolIntent(
            capability=Capability.READ,
            params={"result_id": "r-1"},
            prefer="tool_result_read",
        )
    )
    assert expanded.status is BindingStatus.BOUND
    assert expanded.call is not None
    assert expanded.call.spec_name == "tool_result_read"


def test_core_catalog_has_no_remote_tool() -> None:
    binding = build_catalog("core").bind(
        ToolIntent(capability=Capability.REMOTE)
    )
    assert binding.status is BindingStatus.UNRESOLVED


def test_prefer_is_a_non_binding_hint() -> None:
    catalog = build_catalog("core")
    binding = catalog.bind(
        ToolIntent(
            capability=Capability.EDIT,
            params={"files": ["a.py"]},
            targets=("a.py",),
            prefer="apply_patch",
        )
    )
    assert binding.call is not None
    assert binding.call.spec_name == "apply_patch"
    # An unavailable preference falls back to the capability binding.
    fallback = catalog.bind(
        ToolIntent(
            capability=Capability.EDIT,
            params={"files": ["a.py"]},
            targets=("a.py",),
            prefer="no-such-tool",
        )
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


def test_a_targetless_read_is_refused_before_it_executes() -> None:
    """#405: dogfood `repl-125425ecc94f` sent ``read`` with only a note.

    It executed four times, each failing with "read_file 需要 file_path", the
    loop detector then killed the turn and eight steps of evidence went with
    it.  The proposal was malformed: it wanted to answer, not to read.
    """

    catalog = build_catalog("core")
    binding = catalog.bind(
        ToolIntent(
            capability=Capability.READ,
            params={"note": "汇总步骤1-8的证据并给出建议"},
            targets=(),
        )
    )

    assert binding.status is BindingStatus.UNRESOLVED
    assert binding.call is None, "a malformed call must never reach a tool"
    assert "must be named" in binding.reason
    assert "file_path" in binding.reason, "name the parameter the spec declares"
    assert "finish" in binding.reason, "offer the action that fits the intent"


def test_a_named_read_still_binds() -> None:
    catalog = build_catalog("core")
    binding = catalog.bind(
        ToolIntent(
            capability=Capability.READ,
            params={"file_path": "package.json"},
            targets=("package.json",),
        )
    )

    assert binding.status is BindingStatus.BOUND
    assert binding.call is not None
