"""Capability-aware tool catalog.

Profiles assemble a catalog; they do not change engine behavior.  Binding is
deterministic: a single match or a unique preference wins, otherwise the result
is explicitly ``ambiguous``.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping, Sequence

from xenon.catalog.specs import ALL_SPECS, CORE_SPECS, LEGACY_SPECS, MCP_SPECS
from xenon.kernel.contracts import (
    BindingStatus,
    Capability,
    Risk,
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
        self._strict_remote = any(
            spec.pack == "mcp" and spec.name != "mcp_call"
            for spec in self._all
        )

    def specs(self, profile: str | None = None) -> tuple[ToolSpec, ...]:
        selected = profile or self._profile
        if selected in {"all", "*"}:
            return self._all
        if selected in {"core+mcp", "core_mcp"}:
            packs = {"core", "mcp"}
        else:
            packs = {selected}
        # Registering a plugin is an explicit user act, so it stays visible in
        # every profile: a tool the model cannot see is a tool that does not
        # exist.
        packs.add("plugin")
        return tuple(item for item in self._all if item.pack in packs)

    def bind(self, intent: ToolIntent) -> ToolBinding:
        candidates = [
            item
            for item in self.specs()
            if intent.capability in item.capabilities
        ]
        if intent.capability.value == "remote":
            tool_name = str(intent.params.get("tool_name") or "").strip()
            if not tool_name:
                return ToolBinding(
                    status=BindingStatus.UNRESOLVED,
                    reason="remote capability requires params.tool_name",
                )
            exact = [
                item
                for item in candidates
                if item.name == tool_name and item.name != "mcp_call"
            ]
            if exact:
                candidates = exact
            elif self._strict_remote:
                return ToolBinding(
                    status=BindingStatus.UNRESOLVED,
                    reason=(
                        "unknown MCP tool; expected one of the discovered "
                        "server:tool specs"
                    ),
                )
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
            return self._bound_or_targetless(candidates[0], intent)

        def score(spec: ToolSpec) -> tuple[int, str]:
            return (
                int(spec.preferences.get(intent.capability.value, 0)),
                spec.name,
            )

        ranked = sorted(candidates, key=score, reverse=True)
        top = ranked[0]
        second = ranked[1]
        if score(top)[0] > score(second)[0]:
            return self._bound_or_targetless(top, intent)
        return ToolBinding(
            status=BindingStatus.AMBIGUOUS,
            candidates=tuple(item.name for item in ranked),
            reason=(
                f"multiple tools expose {intent.capability.value} "
                "with the same preference"
            ),
        )

    def discover(
        self,
        query: str,
        *,
        capability: str = "",
        limit: int = 5,
    ) -> tuple[tuple[Mapping[str, object], ...], bool, bool]:
        """Find tools matching an intent, with full params, without executing.

        Deterministic structural scoring, not keyword enumeration: an exact
        tool name wins; otherwise specs rank by how many query words appear
        in the tool's name, description, parameter keys and capabilities.
        Returns ``(matches, truncated, fallback)``: ``fallback`` is true when
        nothing actually matched and the listing is a name-ordered guess, so
        the caller can tell the model instead of letting it mistake an
        unrelated tool for a hit.
        """

        text = str(query or "").strip()
        raw_capability = str(capability or "").strip()
        bound = max(1, min(int(limit or 5), 10))

        pool = [item for item in self.specs() if item.name != "find_tools"]
        if raw_capability:
            wanted = self._capability(raw_capability)
            # A non-empty but unparseable filter is an explicit ask for a
            # capability that does not exist — fail closed, not "ignore filter".
            if wanted is None:
                return (), False, False
            pool = [item for item in pool if wanted in item.capabilities]
        if not pool:
            return (), False, False

        # An exact (case-insensitive) name is the strongest possible signal.
        lowered = text.casefold()
        exact = [item for item in pool if item.name.casefold() == lowered]
        if exact:
            projected = [project_spec(item) for item in exact[:bound]]
            return tuple(projected), len(exact) > bound, False

        query_words = _words(text)
        scored = sorted(
            (
                (_overlap(query_words, _spec_words(item)), item.name, item)
                for item in pool
            ),
            key=lambda triple: (triple[0], triple[1]),
            reverse=True,
        )
        positive = [triple for triple in scored if triple[0] > 0]
        fallback = not positive
        chosen = positive or scored  # never return empty when tools exist
        matches = [project_spec(triple[2]) for triple in chosen[:bound]]
        return tuple(matches), len(chosen) > bound, fallback

    @staticmethod
    def _capability(value: str) -> Capability | None:
        if not value:
            return None
        try:
            return Capability(str(value))
        except ValueError:
            return None

    def knows_capability(self, value: str) -> bool:
        """Is this a capability name the catalog understands?

        The caller needs it to explain an empty result: an unknown filter and
        a filter that legitimately matches nothing both return zero matches,
        and the model cannot tell them apart from the receipt alone.
        """

        return self._capability(str(value or "")) is not None

    def _bound_or_targetless(
        self, spec: ToolSpec, intent: ToolIntent
    ) -> ToolBinding:
        """Bind, unless the tool needs a named target and got none (#405).

        A dogfood run asked for ``read`` with only a note ("summarise steps
        1-8") and no path.  Executing it produced ``read_file 需要 file_path``
        four times, the loop detector killed the turn, and eight steps of
        collected evidence were thrown away.  The call was malformed from the
        start: the model wanted to *answer*, not to read.

        Refusing at binding turns that into one teaching reason — and the
        reason names the parameters the spec declares, so the model can either
        fix the call or submit ``finish``.
        """

        if spec.requires_target and not self._named_target(spec, intent):
            required = ", ".join(spec.params_schema) or "a target"
            return ToolBinding(
                status=BindingStatus.UNRESOLVED,
                reason=(
                    f"{intent.capability.value} acts on something that must be "
                    f"named; this proposal named nothing (expected: {required}). "
                    "If you already collected what you need, submit `finish` "
                    "with the answer instead of calling another tool."
                ),
            )
        return ToolBinding(
            status=BindingStatus.BOUND,
            call=ToolCall(
                spec_name=spec.name,
                params=dict(intent.params),
                capability=intent.capability,
            ),
        )

    @staticmethod
    def _named_target(spec: ToolSpec, intent: ToolIntent) -> bool:
        """Did the proposal name anything the spec can act on?"""

        if intent.targets:
            return True
        for key in spec.params_schema:
            value = intent.params.get(key)
            if isinstance(value, str) and value.strip():
                return True
            if isinstance(value, (list, tuple)) and value:
                return True
        return False


_WORD = re.compile(r"[a-z0-9]+")


def _words(text: str) -> frozenset[str]:
    """Lowercase structural tokens; works for any language's ASCII identifiers."""

    return frozenset(_WORD.findall(text.casefold()))


def _spec_words(spec: ToolSpec) -> frozenset[str]:
    """Every structural word a tool advertises, for intent overlap."""

    blob = " ".join(
        (
            spec.name,
            spec.description or "",
            " ".join(spec.params_schema),
            " ".join(item.value for item in spec.capabilities),
        )
    )
    return _words(blob)


def _overlap(query_words: frozenset[str], spec_words: frozenset[str]) -> int:
    return len(query_words & spec_words)


def project_spec(spec: ToolSpec) -> dict[str, object]:
    """Project one full tool as catalog facts the model may bind against."""

    return {
        "name": spec.name,
        "description": spec.description or "",
        "capabilities": sorted(item.value for item in spec.capabilities),
        "parameters": dict(spec.params_schema),
        "risk": spec.risk.value,
    }


def plugin_specs() -> tuple[ToolSpec, ...]:
    """Turn user-registered plugin tools into catalog specs.

    The legacy engine merged ``plugin_schemas()`` into its own tool dict, which
    is how a registered plugin became visible to the model.  That merge lived
    in the engine, so removing the engine silently hid every plugin.  Doing it
    here keeps the promise: registering a tool makes it callable.
    """

    from xenon.nodes.tool_registry import BUILTIN_TOOL_REGISTRY

    specs: list[ToolSpec] = []
    for name, schema in BUILTIN_TOOL_REGISTRY.plugin_schemas().items():
        specs.append(
            ToolSpec(
                name=name,
                pack="plugin",
                # A plugin's capability is unknown, so it is not advertised as
                # one of the closed set.  The catalog still binds it by name.
                capabilities=frozenset(),
                effects=frozenset(),
                risk=Risk.WRITE,
                params_schema=dict(schema.get("params") or {}),
                description=str(schema.get("description") or ""),
            )
        )
    return tuple(specs)


def build_catalog(
    profile: str = "core",
    *,
    extra_specs: Sequence[ToolSpec] = (),
    include_plugins: bool = True,
) -> ToolCatalog:
    extra = tuple(extra_specs)
    if include_plugins:
        extra = extra + plugin_specs()

    def combined(base: Sequence[ToolSpec]) -> tuple[ToolSpec, ...]:
        merged = {spec.name: spec for spec in base}
        merged.update({spec.name: spec for spec in extra})
        return tuple(merged.values())

    if profile in {"all", "*"}:
        return ToolCatalog(combined(ALL_SPECS), "all")
    if profile == "core":
        return ToolCatalog(combined(CORE_SPECS), "core")
    if profile in {"core+mcp", "core_mcp"}:
        return ToolCatalog(combined(CORE_SPECS + MCP_SPECS), "core+mcp")
    if profile == "legacy":
        return ToolCatalog(combined(LEGACY_SPECS), "legacy")
    if profile == "mcp":
        return ToolCatalog(combined(MCP_SPECS), "mcp")
    raise ValueError(f"unknown tool profile: {profile!r}")
