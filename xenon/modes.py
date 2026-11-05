"""Canonical thinking-mode catalog.

This module is intentionally neutral: it imports neither the legacy engine
registry nor the REPL.  ``/mode``, the setup wizard and the runtime router share
one list of names and descriptions without loading the engine skeleton.

A mode no longer selects a strategy class.  There is exactly one kernel loop
(``LoopStrategy``) with three phases — plan, react, reflect — and a mode only
declares which phases run and how much they may spend.  ``loop`` is that
declaration, so adding a mode cannot fork the execution model again.
"""

from __future__ import annotations

from dataclasses import dataclass

from xenon.kernel.strategies import DEFAULT_PROFILE, LoopProfile

DEFAULT_MODE = "direct"

# Shared shapes, named once so modes cannot drift apart silently.
# ``direct`` is the default coding loop: plan → execute one step at a time →
# check the step's facts → re-plan from the gaps (up to three rounds).  A user
# no longer picks a paradigm; the other names stay as internal presets.
_DEFAULT_LOOP = DEFAULT_PROFILE
_PLAN_THEN_EXECUTE = LoopProfile(plan=True, step_scope=True, follow_plan=True)
_PLAN_THEN_REACT = LoopProfile(plan=True, step_scope=True, step_calls=6)
_REFLECT_ONCE = LoopProfile(reflect_rounds=1, reflect_calls=1)
_REFLECT_MULTI = LoopProfile(reflect_rounds=3, reflect_calls=2)


@dataclass(frozen=True, slots=True)
class ModeSpec:
    """A user-selectable thinking mode.

    Attributes:
        name: Stable identifier used by ``/mode`` and session config.
        description: One-line explanation shown by ``/mode`` and setup.
        workflow_template: Optional legacy workflow YAML path.
        loop: Which phases of the kernel loop this mode runs.  ``None`` means
            the mode is not selectable on the kernel (fail closed).
    """

    name: str
    description: str = ""
    workflow_template: str = ""
    loop: LoopProfile | None = None
    # Set when this name is kept only for compatibility and resolves to the
    # same loop profile as another mode.  ``react`` is the case: the seven
    # engines are gone, and ``react`` now declares exactly what ``direct``
    # declares.  Keeping the name keeps `--mode react` working; advertising it
    # as a separate paradigm would claim a difference that no longer exists.
    alias_of: str = ""

    @property
    def kernel_only(self) -> bool:
        """Whether the mode must run on the kernel loop."""
        return self.loop is not None


MODE_CATALOG: dict[str, ModeSpec] = {
    "direct": ModeSpec(
        name="direct",
        description="默认单循环：先计划，再逐步执行并按事实校验",
        loop=_DEFAULT_LOOP,
    ),
    "react": ModeSpec(
        name="react",
        description="（别名）与 direct 同一套单循环",
        loop=_DEFAULT_LOOP,
        alias_of="direct",
    ),
    "plan-execute": ModeSpec(
        name="plan-execute",
        description="规划阶段 + 按计划逐步执行（follow_plan）",
        loop=_PLAN_THEN_EXECUTE,
    ),
    "reflection": ModeSpec(
        name="reflection",
        description="执行 + 一轮反思审查（reflect_rounds=1）",
        loop=_REFLECT_ONCE,
    ),
    "plan-react": ModeSpec(
        name="plan-react",
        description="全局规划 + 每步限定调用次数的执行（step_calls=6）",
        loop=_PLAN_THEN_REACT,
    ),
    "plan-reflection": ModeSpec(
        name="plan-reflection",
        description="规划 + 计划审查 + 每步执行 + 步骤反思",
        loop=LoopProfile(
            plan=True,
            plan_review=True,
            step_scope=True,
            step_calls=6,
            step_reflect=True,
            reflect_rounds=1,
            reflect_calls=1,
        ),
    ),
    "react-reflection": ModeSpec(
        name="react-reflection",
        description="执行 + 三轮反思审查（reflect_rounds=3）",
        loop=_REFLECT_MULTI,
    ),
}


def distinct_modes() -> tuple[ModeSpec, ...]:
    """One entry per *distinct* loop profile, for menus and pickers.

    Aliases stay resolvable but are not offered as if they were alternatives:
    offering two rows that do exactly the same thing is how the old seven-engine
    catalog lied about itself.
    """

    canonical: dict[str, ModeSpec] = {}
    for spec in MODE_CATALOG.values():
        if spec.alias_of:
            continue
        canonical.setdefault(repr(spec.loop), spec)
    return tuple(canonical.values())


def aliases_of(name: str) -> tuple[str, ...]:
    """Names that resolve to the same mode, excluding the mode itself."""

    return tuple(
        alias for alias, spec in MODE_CATALOG.items() if spec.alias_of == name
    )


def get_mode(name: str | None) -> ModeSpec | None:
    """Resolve a mode name; blank/None means the default mode.

    Unknown names return ``None`` rather than being coerced to ``direct``.
    Callers that route execution must fail closed on ``None``.
    """

    key = (name or DEFAULT_MODE).strip().lower()
    return MODE_CATALOG.get(key)


def loop_profile_for(name: str | None) -> LoopProfile | None:
    """Return the kernel loop profile for a mode, or ``None`` if unusable."""

    spec = get_mode(name)
    return spec.loop if spec is not None else None


__all__ = [
    "DEFAULT_MODE",
    "MODE_CATALOG",
    "aliases_of",
    "distinct_modes",
    "ModeSpec",
    "get_mode",
    "loop_profile_for",
]
