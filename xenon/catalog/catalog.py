"""Capability-aware tool catalog.

Profiles assemble a catalog; they do not change engine behavior.  Binding is
deterministic: a single match or a unique preference wins, otherwise the result
is explicitly ``ambiguous``.
"""

from __future__ import annotations

from typing import Iterable

from xenon.catalog.specs import ALL_SPECS, CORE_SPECS, LEGACY_SPECS, MCP_SPECS
from xenon.kernel.contracts import (
    BindingStatus,
    ToolBinding,
    ToolCall,
    ToolIntent,
    ToolSpec,
)


class ToolCatalog:
    def __init__(
        self,
        specs: Iterable[ToolSpec],
        profile: str = "core",
    ) -> None:
        self._all = tuple(specs)
        self._profile = profile

    def specs(self, profile: str | None = None) -> tuple[ToolSpec, ...]:
        selected = profile or self._profile
        if selected in {"all", "*"}:
            return self._all
        return tuple(item for item in self._all if item.pack == selected)

    def bind(self, intent: ToolIntent) -> ToolBinding:
        candidates = [
            item
            for item in self.specs()
            if intent.capability in item.capabilities
        ]
        if intent.prefer:
            preferred = [
                item for item in candidates if item.name == intent.prefer
            ]
            if preferred:
                candidates = preferred
        if not candidates:
            return ToolBinding(
                status=BindingStatus.UNRESOLVED,
                reason=f"no tool exposes capability {intent.capability.value}",
            )
        if len(candidates) == 1:
            return ToolBinding(
                status=BindingStatus.BOUND,
                call=ToolCall(
                    spec_name=candidates[0].name,
                    params=dict(intent.params),
                    capability=intent.capability,
                ),
            )

        def score(spec: ToolSpec) -> tuple[int, str]:
            return (
                int(spec.preferences.get(intent.capability.value, 0)),
                spec.name,
            )

        ranked = sorted(candidates, key=score, reverse=True)
        top = ranked[0]
        second = ranked[1]
        if score(top)[0] > score(second)[0]:
            return ToolBinding(
                status=BindingStatus.BOUND,
                call=ToolCall(
                    spec_name=top.name,
                    params=dict(intent.params),
                    capability=intent.capability,
                ),
            )
        return ToolBinding(
            status=BindingStatus.AMBIGUOUS,
            candidates=tuple(item.name for item in ranked),
            reason=(
                f"multiple tools expose {intent.capability.value} "
                "with the same preference"
            ),
        )


def build_catalog(profile: str = "core") -> ToolCatalog:
    if profile in {"all", "*"}:
        return ToolCatalog(ALL_SPECS, "all")
    if profile == "core":
        return ToolCatalog(CORE_SPECS, "core")
    if profile == "legacy":
        return ToolCatalog(LEGACY_SPECS, "legacy")
    if profile == "mcp":
        return ToolCatalog(MCP_SPECS, "mcp")
    raise ValueError(f"unknown tool profile: {profile!r}")
