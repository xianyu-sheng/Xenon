"""Canonical thinking-mode catalog.

This module is intentionally neutral: it imports neither the legacy engine
registry nor the REPL.  ``/mode``, the setup wizard and the runtime router can
therefore share one list of names and descriptions without loading the engine
skeleton.

``kernel_strategy`` is the routing contract, not a promise that every mode is
already migrated:

* ``act``  — tool turns use the kernel's ActFirst strategy (``react`` and the
  default ``direct`` mode; direct chat itself is still a REPL branch);
* ``plan`` — tool turns use the kernel's LLM PlanFirst strategy
  (``plan-execute``);
* ``None`` — no kernel parity yet; the mode stays on the frozen legacy path and
  must not silently fall through to ActFirst.

Adding a kernel strategy is therefore an explicit decision, and adding a new
legacy mode cannot accidentally inherit another mode's routing.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MODE = "direct"


@dataclass(frozen=True, slots=True)
class ModeSpec:
    """A user-selectable thinking mode.

    Attributes:
        name: Stable identifier used by ``/mode`` and session config.
        description: One-line explanation shown by ``/mode`` and setup.
        workflow_template: Optional legacy workflow YAML path.
        kernel_strategy: ``act`` / ``plan`` when the kernel has parity; ``None``
            means the mode remains on the frozen legacy path.
    """

    name: str
    description: str = ""
    workflow_template: str = ""
    kernel_strategy: str | None = None

    @property
    def legacy_only(self) -> bool:
        """Whether the mode must stay on the legacy execution path."""
        return self.kernel_strategy is None


# Order is the user-visible ``/mode`` order.  Keep legacy-only modes in the
# catalog while they are still selectable, but mark them explicitly.
MODE_CATALOG: dict[str, ModeSpec] = {
    "direct": ModeSpec(
        name="direct",
        description="直接对话，不使用特殊引擎（默认模式）",
        # Direct chat is still a REPL branch.  A direct-default tool turn is
        # auto-upgraded to ActFirst by the kernel path, matching the existing
        # "kernel default for tool turns" behaviour.
        kernel_strategy="act",
    ),
    "react": ModeSpec(
        name="react",
        description="思考-行动-观察循环，适合需要工具的探索性任务",
        kernel_strategy="act",
    ),
    "plan-execute": ModeSpec(
        name="plan-execute",
        description="先用强模型规划，再逐步执行，适合复杂任务",
        kernel_strategy="plan",
    ),
    "reflection": ModeSpec(
        name="reflection",
        description="执行后自我审查并修正，适合高质量代码生成",
    ),
    "plan-react": ModeSpec(
        name="plan-react",
        description="全局规划 + 每步 ReAct 执行，适合复杂多步骤任务",
    ),
    "plan-reflection": ModeSpec(
        name="plan-reflection",
        description="规划执行 + 反思修正，适合需要高质量输出的任务",
    ),
    "react-reflection": ModeSpec(
        name="react-reflection",
        description="ReAct 探索 + 反思审查，适合需要工具且要求高质量的任务",
    ),
}


def get_mode(name: str | None) -> ModeSpec | None:
    """Resolve a mode name; blank/None means the default mode.

    Unknown names return ``None`` rather than being coerced to ``direct``.
    Callers that route execution must fail closed on ``None``.
    """

    key = (name or DEFAULT_MODE).strip().lower()
    return MODE_CATALOG.get(key)


def kernel_strategy_for(name: str | None) -> str | None:
    """Return the kernel strategy for a mode, or ``None`` if legacy-only."""

    spec = get_mode(name)
    return spec.kernel_strategy if spec is not None else None


__all__ = [
    "DEFAULT_MODE",
    "MODE_CATALOG",
    "ModeSpec",
    "get_mode",
    "kernel_strategy_for",
]
