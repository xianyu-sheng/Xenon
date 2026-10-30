"""Plan-file parsing for the headless harness (#203).

The plan format is deliberately capability-based: a plan never names a tool.
Steps carry ``kind`` + ``intent`` + ``expects``; the catalog binds the intent to
a concrete tool at execution time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from xenon.kernel.contracts import (
    Capability,
    Obligation,
    ObligationSource,
    PlanProposal,
    PropositionKind,
    StepContract,
    StepKind,
    ToolIntent,
)


class PlanError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class HarnessPlan:
    objective: str
    proposal: PlanProposal


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PlanError(f"{where} must be an object")
    return value


def _list(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise PlanError(f"{where} must be a list")
    return value


def _capability(value: Any) -> Capability:
    try:
        return Capability(str(value))
    except ValueError as exc:
        raise PlanError(f"unknown capability: {value!r}") from exc


_STEP_KIND_ALIASES = {
    "write": StepKind.EDIT,
    "create": StepKind.EDIT,
    "modify": StepKind.EDIT,
    "read": StepKind.INSPECT,
    "test": StepKind.VERIFY,
    "run": StepKind.EXECUTE,
}


def _step_kind(value: Any) -> StepKind:
    raw = str(value)
    alias = _STEP_KIND_ALIASES.get(raw.lower())
    if alias is not None:
        return alias
    try:
        return StepKind(raw)
    except ValueError as exc:
        raise PlanError(f"unknown step kind: {value!r}") from exc


_PROPOSITION_ALIASES = {
    "file_exists": PropositionKind.FILE_CREATED,
    "file_written": PropositionKind.FILE_CHANGED,
    "file_content_matches": PropositionKind.FILE_CHANGED,
    "content_matches": PropositionKind.FILE_CHANGED,
    "tests_passed": PropositionKind.TEST_PASSED,
    "test_run": PropositionKind.TEST_PASSED,
}


def _proposition(value: Any) -> PropositionKind:
    raw = str(value)
    alias = _PROPOSITION_ALIASES.get(raw.lower())
    if alias is not None:
        return alias
    try:
        return PropositionKind(raw)
    except ValueError as exc:
        raise PlanError(f"unknown expectation kind: {value!r}") from exc


def _expectation(value: Any, where: str) -> Obligation:
    data = _mapping(value, where)
    identifier = str(data.get("id") or "")
    if not identifier:
        raise PlanError(f"{where}.id is required")
    target = str(data.get("target") or "")
    expect = data.get("expect") or {}
    if not isinstance(expect, Mapping):
        raise PlanError(f"{where}.expect must be an object")
    return Obligation(
        id=identifier,
        kind=_proposition(data.get("kind")),
        target=target,
        expect=dict(expect),
        source=ObligationSource.PLAN,
    )


def _step(value: Any, index: int) -> StepContract:
    data = _mapping(value, f"steps[{index}]")
    identifier = data.get("id")
    if not isinstance(identifier, int):
        raise PlanError(f"steps[{index}].id must be an integer")
    targets = tuple(str(item) for item in _list(data.get("targets") or [], f"steps[{index}].targets"))
    capability = data.get("capability")
    intent = None
    if capability is not None:
        params = data.get("params") or {}
        if not isinstance(params, Mapping):
            raise PlanError(f"steps[{index}].params must be an object")
        intent = ToolIntent(
            capability=_capability(capability),
            params=dict(params),
            targets=targets,
            prefer=data.get("prefer"),
        )
    expects = tuple(
        _expectation(item, f"steps[{index}].expects[{position}]")
        for position, item in enumerate(
            _list(data.get("expects") or [], f"steps[{index}].expects")
        )
    )
    return StepContract(
        id=identifier,
        kind=_step_kind(data.get("kind")),
        intent=intent,
        targets=targets,
        expects=expects,
        depends_on=tuple(
            int(item)
            for item in _list(
                data.get("depends_on") or [],
                f"steps[{index}].depends_on",
            )
        ),
        prefer=data.get("prefer"),
    )


def parse_plan(value: Mapping[str, Any]) -> HarnessPlan:
    data = _mapping(value, "plan")
    objective = str(data.get("objective") or "").strip()
    if not objective:
        raise PlanError("plan.objective is required")
    steps = tuple(
        _step(item, index)
        for index, item in enumerate(_list(data.get("steps") or [], "steps"))
    )
    if not steps:
        raise PlanError("plan.steps must not be empty")
    analysis = str(data.get("analysis") or "")
    return HarnessPlan(
        objective=objective,
        proposal=PlanProposal(steps=steps, analysis=analysis),
    )


__all__ = ["HarnessPlan", "PlanError", "parse_plan"]
