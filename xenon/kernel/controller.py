"""Deterministic verification controller (#199).

V0 reconciles one receipt, V1 closes a step, V2 audits a claim.  The model never
decides whether a claim is true: it proposes claims, and this module binds them
to receipt facts or returns unresolved/ambiguous/abstained.

A claim with no fact is not a failure of the tool; it is an unproven claim.  It
opens debt and blocks delivery until a fact exists or the user accepts the gap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from xenon.kernel.contracts import (
    Claim,
    ClaimSource,
    ControlDecision,
    ControlDecisionKind,
    Debt,
    FsWrite,
    Obligation,
    PropositionKind,
    ReceiptStatus,
    Resolution,
    ResolutionStatus,
    StepContract,
    TestRun,
    VcsCommit,
    VcsPush,
    Verdict,
    VerdictStatus,
    VerificationStage,
)
from xenon.ledger.events import (
    EventKind,
    effect_to_payload,
    fact_id,
    receipt_from_payload,
)


@dataclass(frozen=True, slots=True)
class Fact:
    fact_id: str
    effect: Any
    seq: int
    receipt_id: str


def index_facts(ledger: Any, up_to_seq: int | None = None) -> tuple[Fact, ...]:
    """Flatten receipt effects into facts in ledger order."""

    facts: list[Fact] = []
    for event in ledger.read(EventKind.TOOL_RECEIPT, up_to_seq=up_to_seq):
        payload = event.payload or {}
        receipt_payload = payload.get("receipt")
        if not isinstance(receipt_payload, dict):
            continue
        receipt = receipt_from_payload(receipt_payload)
        if receipt.status is not ReceiptStatus.OK:
            continue
        stored_ids = list(payload.get("fact_ids") or ())
        for index, effect in enumerate(receipt.effects):
            identifier = (
                stored_ids[index]
                if index < len(stored_ids)
                else fact_id(effect_to_payload(effect))
            )
            facts.append(
                Fact(
                    fact_id=identifier,
                    effect=effect,
                    seq=event.seq,
                    receipt_id=receipt.receipt_id,
                )
            )
    return tuple(facts)


def _normalized(value: str) -> str:
    return value.replace("\\", "/").strip().lower().lstrip("./").rstrip("/")


def _path_match(paths: Iterable[str], target: str) -> bool:
    if not target:
        return bool(tuple(paths))
    wanted = _normalized(target)
    for path in paths:
        actual = _normalized(path)
        if (
            actual == wanted
            or actual.endswith("/" + wanted)
            or wanted.endswith("/" + actual)
            or actual.startswith(wanted + "::")
            or wanted.startswith(actual + "::")
        ):
            return True
    return False


def _test_matches(effect: TestRun, target: str) -> bool:
    if not target:
        return True
    if _path_match((effect.target,), target):
        return True
    return _normalized(target) in _normalized(effect.command)


def _match_key(fact: Fact, claim: Claim) -> str:
    """The concrete entity a fact matched, used to reject cross-target guesses."""

    effect = fact.effect
    paths: Iterable[str] = ()
    if isinstance(effect, FsWrite):
        paths = effect.paths
        if claim.kind is PropositionKind.FILE_CREATED:
            paths = effect.created_paths
    for path in paths:
        if _path_match((path,), claim.target):
            return _normalized(path)
    if isinstance(effect, TestRun):
        if _normalized(effect.target) == _normalized(claim.target):
            return _normalized(effect.target)
        if effect.command:
            return _normalized(effect.command)
        return _normalized(effect.target)
    if isinstance(effect, VcsCommit):
        return effect.sha
    if isinstance(effect, VcsPush):
        return f"{effect.remote}:{effect.branch}"
    return fact.fact_id


def _unique_facts(matches: Sequence[Fact]) -> tuple[Fact, ...]:
    seen: set[str] = set()
    result: list[Fact] = []
    for fact in matches:
        if fact.fact_id in seen:
            continue
        seen.add(fact.fact_id)
        result.append(fact)
    return tuple(result)


class ClaimResolver:
    """Bind a claim to facts; zero/multi matches are explicit, never guessed."""

    def resolve(
        self,
        claim: Claim,
        ledger: Any,
        up_to_seq: int | None = None,
    ) -> Resolution:
        facts = index_facts(ledger, up_to_seq=up_to_seq)
        if claim.since_seq:
            facts = tuple(item for item in facts if item.seq > claim.since_seq)
        return self._resolve(claim, facts)

    def _resolve(self, claim: Claim, facts: Sequence[Fact]) -> Resolution:
        kind = claim.kind
        if kind is PropositionKind.FILE_CHANGED:
            matches = _unique_facts(
                [
                    fact
                    for fact in facts
                    if isinstance(fact.effect, FsWrite)
                    and fact.effect.changed
                    and _path_match(fact.effect.paths, claim.target)
                ]
            )
            return self._latest(claim, matches, "no changed file fact")

        if kind is PropositionKind.FILE_CREATED:
            matches = _unique_facts(
                [
                    fact
                    for fact in facts
                    if isinstance(fact.effect, FsWrite)
                    and _path_match(fact.effect.created_paths, claim.target)
                ]
            )
            return self._latest(claim, matches, "no created file fact")

        if kind is PropositionKind.TEST_PASSED:
            minimum = int(claim.expect.get("min_passed") or 1)
            matches = _unique_facts(
                [
                    fact
                    for fact in facts
                    if isinstance(fact.effect, TestRun)
                    and _test_matches(fact.effect, claim.target)
                    and fact.effect.tests_run > 0
                    and fact.effect.passed >= minimum
                    and fact.effect.failed == 0
                    and fact.effect.errors == 0
                    and fact.effect.exit_code == 0
                ]
            )
            return self._latest(claim, matches, "no passing test fact for target")

        if kind is PropositionKind.COMMIT_CREATED:
            matches = _unique_facts(
                [
                    fact
                    for fact in facts
                    if isinstance(fact.effect, VcsCommit)
                    and fact.effect.sha
                    and (
                        not claim.target
                        or fact.effect.sha.startswith(claim.target)
                    )
                ]
            )
            return self._latest(claim, matches, "no commit fact")

        if kind is PropositionKind.PUSHED:
            matches = _unique_facts(
                [
                    fact
                    for fact in facts
                    if isinstance(fact.effect, VcsPush)
                    and (
                        not claim.target
                        or claim.target in {fact.effect.remote, fact.effect.branch}
                    )
                ]
            )
            return self._latest(claim, matches, "no push fact")

        if kind is PropositionKind.NO_STRAY_CHANGES:
            return Resolution(
                claim_id=claim.id,
                status=ResolutionStatus.ABSTAINED,
                reason="no deterministic worktree-clean fact yet",
            )

        if kind is PropositionKind.TOOL_SUCCEEDED:
            return Resolution(
                claim_id=claim.id,
                status=ResolutionStatus.ABSTAINED,
                reason="receipt status is not indexed as a claim fact",
            )

        if kind in {
            PropositionKind.REQUIREMENT_MET,
            PropositionKind.ANSWER_DELIVERED,
        }:
            return Resolution(
                claim_id=claim.id,
                status=ResolutionStatus.ABSTAINED,
                reason="semantic claim needs a coverage binder or user acceptance",
            )

        return Resolution(
            claim_id=claim.id,
            status=ResolutionStatus.ABSTAINED,
            reason=f"unsupported claim kind: {kind.value}",
        )

    @staticmethod
    def _latest(
        claim: Claim, matches: Sequence[Fact], missing: str
    ) -> Resolution:
        if not matches:
            return Resolution(
                claim_id=claim.id,
                status=ResolutionStatus.UNRESOLVED,
                reason=missing,
            )
        if len(matches) == 1:
            return Resolution(
                claim_id=claim.id,
                status=ResolutionStatus.BOUND,
                fact_ids=(matches[0].fact_id,),
            )
        if not claim.target:
            return Resolution(
                claim_id=claim.id,
                status=ResolutionStatus.AMBIGUOUS,
                reason="multiple facts and no target",
                candidates=tuple(item.fact_id for item in matches),
            )
        wanted = _normalized(claim.target)
        exact = [
            item for item in matches if _match_key(item, claim) == wanted
        ]
        keys = {_match_key(item, claim) for item in matches}
        if not exact and len(keys) > 1:
            return Resolution(
                claim_id=claim.id,
                status=ResolutionStatus.AMBIGUOUS,
                reason="multiple distinct entities match the target",
                candidates=tuple(item.fact_id for item in matches),
            )
        pool = exact or list(matches)
        latest = max(pool, key=lambda item: item.seq)
        return Resolution(
            claim_id=claim.id,
            status=ResolutionStatus.BOUND,
            fact_ids=(latest.fact_id,),
        )


class VerificationController:
    """The single verification authority for V0/V1/V2."""

    def __init__(self, resolver: ClaimResolver | None = None) -> None:
        self._resolver = resolver or ClaimResolver()

    def reconcile(
        self, step: StepContract | None, receipt: Any
    ) -> Verdict:
        obligation_id = (
            step.expects[0].id
            if step is not None and step.expects
            else receipt.receipt_id
        )
        if receipt.status is not ReceiptStatus.OK:
            return Verdict(
                obligation_id=obligation_id,
                stage=VerificationStage.V0,
                status=VerdictStatus.VIOLATED,
                reason=f"receipt status: {receipt.status.value}",
            )
        unexpected = set(receipt.observed_effects) - set(
            receipt.declared_effects
        )
        if unexpected:
            return Verdict(
                obligation_id=obligation_id,
                stage=VerificationStage.V0,
                status=VerdictStatus.UNVERIFIABLE,
                reason=(
                    "observed effects not declared by the tool: "
                    + ", ".join(sorted(item.value for item in unexpected))
                ),
            )
        for effect in receipt.effects:
            if isinstance(effect, TestRun):
                if effect.exit_code != 0:
                    return Verdict(
                        obligation_id=obligation_id,
                        stage=VerificationStage.V0,
                        status=VerdictStatus.VIOLATED,
                        reason=f"test exit code {effect.exit_code}",
                    )
                if effect.tests_run == 0:
                    return Verdict(
                        obligation_id=obligation_id,
                        stage=VerificationStage.V0,
                        status=VerdictStatus.UNVERIFIABLE,
                        reason="test command ran zero tests",
                    )
            if isinstance(effect, VcsCommit) and not effect.sha:
                return Verdict(
                    obligation_id=obligation_id,
                    stage=VerificationStage.V0,
                    status=VerdictStatus.VIOLATED,
                    reason="commit has no sha",
                )
        return Verdict(
            obligation_id=obligation_id,
            stage=VerificationStage.V0,
            status=VerdictStatus.SATISFIED,
            fact_ids=(),
        )

    def close_step(
        self, step: StepContract, ledger: Any
    ) -> tuple[Verdict, ...]:
        verdicts: list[Verdict] = []
        for obligation in step.expects:
            verdicts.append(self._obligation_verdict(obligation, ledger))
        return tuple(verdicts)

    def resolve(self, claim: Claim, ledger: Any) -> Resolution:
        """Resolve one claim; callers use this to report resolutions honestly."""

        return self._resolver.resolve(claim, ledger)

    def audit(
        self, claim: Claim | None, ledger: Any, debt: Sequence[Debt]
    ) -> Verdict:
        if claim is None:
            if debt:
                return Verdict(
                    obligation_id="task",
                    stage=VerificationStage.V2,
                    status=VerdictStatus.UNVERIFIABLE,
                    reason="open debt",
                )
            return Verdict(
                obligation_id="task",
                stage=VerificationStage.V2,
                status=VerdictStatus.SATISFIED,
            )
        resolution = self.resolve(claim, ledger)
        return self._resolution_verdict(
            claim.id, resolution, VerificationStage.V2
        )

    def decide(
        self, verdicts: Sequence[Verdict], debt: Sequence[Debt]
    ) -> ControlDecision:
        violated = [
            item for item in verdicts if item.status is VerdictStatus.VIOLATED
        ]
        if violated:
            return ControlDecision(
                kind=ControlDecisionKind.BLOCK,
                reason="; ".join(item.reason or item.obligation_id for item in violated),
                obligation_ids=tuple(item.obligation_id for item in violated),
            )
        unverifiable = [
            item
            for item in verdicts
            if item.status is VerdictStatus.UNVERIFIABLE
        ]
        if unverifiable:
            return ControlDecision(
                kind=ControlDecisionKind.ABSTAIN,
                reason="; ".join(
                    item.reason or item.obligation_id for item in unverifiable
                ),
                obligation_ids=tuple(
                    item.obligation_id for item in unverifiable
                ),
            )
        if debt:
            return ControlDecision(
                kind=ControlDecisionKind.BLOCK,
                reason="open debt",
                obligation_ids=tuple(item.obligation.id for item in debt),
            )
        return ControlDecision(kind=ControlDecisionKind.DELIVER)

    def _obligation_verdict(
        self, obligation: Obligation, ledger: Any
    ) -> Verdict:
        claim = Claim(
            id=obligation.id,
            kind=obligation.kind,
            target=obligation.target,
            expect=dict(obligation.expect),
            source=ClaimSource.USER,
            since_seq=obligation.since_seq,
        )
        resolution = self._resolver.resolve(claim, ledger)
        return self._resolution_verdict(
            obligation.id, resolution, VerificationStage.V1
        )

    @staticmethod
    def _resolution_verdict(
        obligation_id: str,
        resolution: Resolution,
        stage: VerificationStage,
    ) -> Verdict:
        if resolution.status is ResolutionStatus.BOUND:
            return Verdict(
                obligation_id=obligation_id,
                stage=stage,
                status=VerdictStatus.SATISFIED,
                fact_ids=resolution.fact_ids,
            )
        if resolution.status is ResolutionStatus.UNRESOLVED:
            return Verdict(
                obligation_id=obligation_id,
                stage=stage,
                status=VerdictStatus.VIOLATED,
                reason=resolution.reason or "no matching fact",
            )
        return Verdict(
            obligation_id=obligation_id,
            stage=stage,
            status=VerdictStatus.UNVERIFIABLE,
            reason=resolution.reason or resolution.status.value,
            fact_ids=resolution.fact_ids,
        )
