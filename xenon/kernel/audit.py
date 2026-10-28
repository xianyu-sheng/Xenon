"""Deterministic V2 audit primitives (#200).

The LLM may propose coverage, but it never decides a verdict.  This module
binds an obligation to ledger facts; zero matches are unresolved, multiple
matches are ambiguous, and obligations without a deterministic check abstain.
The report is data: the runtime decides whether it becomes debt.
"""

from __future__ import annotations

from typing import Any, Sequence

from xenon.kernel.contracts import (
    AuditProposal,
    AuditReport,
    Claim,
    ClaimSource,
    CoverageClaim,
    CoverageRow,
    FsWrite,
    GapProposal,
    Obligation,
    PropositionKind,
    ResolutionStatus,
    TestRun,
    VcsPush,
    Verdict,
    VerdictStatus,
    VerificationStage,
)
from xenon.kernel.controller import (
    ClaimResolver,
    Fact,
    _path_match,
    index_facts,
)


class CoverageBinder:
    """Bind obligations to facts, with explicit negative-fact handling."""

    def __init__(self, resolver: ClaimResolver | None = None) -> None:
        self._resolver = resolver or ClaimResolver()

    def bind(
        self,
        obligation: Obligation,
        ledger: Any,
        *,
        proposal: AuditProposal | None = None,
        facts: Sequence[Fact] | None = None,
    ) -> CoverageRow:
        facts = tuple(facts) if facts is not None else index_facts(ledger)
        if obligation.kind is PropositionKind.PUSHED:
            return self._bind_push(obligation, facts)

        claim = Claim(
            id=obligation.id,
            kind=obligation.kind,
            target=obligation.target,
            expect=dict(obligation.expect),
            source=ClaimSource.USER,
            since_seq=obligation.since_seq,
        )
        resolution = self._resolver.resolve(claim, ledger)
        if resolution.status is not ResolutionStatus.ABSTAINED:
            return CoverageRow(
                obligation_id=obligation.id,
                kind=obligation.kind,
                target=obligation.target,
                status=resolution.status,
                fact_ids=resolution.fact_ids,
                reason=resolution.reason,
                next_action=_next_action(obligation),
            )

        coverage = _coverage_for(proposal, obligation.id)
        if coverage is not None and obligation.kind in {
            PropositionKind.REQUIREMENT_MET,
            PropositionKind.ANSWER_DELIVERED,
        }:
            # Proposals may fill semantic gaps, but they must never override
            # a deterministic kind with a guessed artifact/test pair.
            bound = self._bind_coverage(obligation, coverage, facts)
            if bound is not None:
                return bound
        return CoverageRow(
            obligation_id=obligation.id,
            kind=obligation.kind,
            target=obligation.target,
            status=resolution.status,
            fact_ids=resolution.fact_ids,
            reason=resolution.reason,
            next_action=_next_action(obligation),
        )

    def _bind_push(
        self, obligation: Obligation, facts: Sequence[Fact]
    ) -> CoverageRow:
        pushes = [
            fact
            for fact in facts
            if isinstance(fact.effect, VcsPush)
            and (
                not obligation.target
                or obligation.target
                in {fact.effect.remote, fact.effect.branch}
            )
        ]
        absent = bool(obligation.expect.get("absent"))
        if absent:
            if not pushes:
                return CoverageRow(
                    obligation_id=obligation.id,
                    kind=obligation.kind,
                    target=obligation.target,
                    status=ResolutionStatus.BOUND,
                    reason="no push receipt (negative fact)",
                )
            return CoverageRow(
                obligation_id=obligation.id,
                kind=obligation.kind,
                target=obligation.target,
                status=ResolutionStatus.UNRESOLVED,
                fact_ids=tuple(fact.fact_id for fact in pushes),
                reason="push receipt exists but absence was required",
                next_action="remove the push or explicitly accept it",
            )
        if pushes:
            return CoverageRow(
                obligation_id=obligation.id,
                kind=obligation.kind,
                target=obligation.target,
                status=ResolutionStatus.BOUND,
                fact_ids=tuple(fact.fact_id for fact in pushes),
            )
        return CoverageRow(
            obligation_id=obligation.id,
            kind=obligation.kind,
            target=obligation.target,
            status=ResolutionStatus.UNRESOLVED,
            reason="no push receipt",
            next_action="produce a push receipt or remove the obligation",
        )

    def _bind_coverage(
        self,
        obligation: Obligation,
        coverage: CoverageClaim,
        facts: Sequence[Fact],
    ) -> CoverageRow | None:
        artifact_ids: list[str] = []
        for path in coverage.artifact_paths:
            matches = [
                fact
                for fact in facts
                if (
                    isinstance(fact.effect, FsWrite)
                    and fact.effect.changed
                    and _path_match(fact.effect.paths, path)
                )
            ]
            if not matches:
                return None
            artifact_ids.extend(fact.fact_id for fact in matches)
        test_ids: list[str] = []
        for target in coverage.test_targets:
            matches = [
                fact
                for fact in facts
                if (
                    isinstance(fact.effect, TestRun)
                    and _path_match((fact.effect.target,), target)
                    and fact.effect.tests_run > 0
                    and fact.effect.failed == 0
                    and fact.effect.errors == 0
                    and fact.effect.exit_code == 0
                )
            ]
            if not matches:
                return None
            test_ids.extend(fact.fact_id for fact in matches)
        if not artifact_ids and not test_ids:
            return None
        return CoverageRow(
            obligation_id=obligation.id,
            kind=obligation.kind,
            target=obligation.target,
            status=ResolutionStatus.BOUND,
            fact_ids=tuple(dict.fromkeys([*artifact_ids, *test_ids])),
            reason="proposal matched deterministic facts",
            source="proposal",
        )


def _coverage_for(
    proposal: AuditProposal | None, obligation_id: str
) -> CoverageClaim | None:
    if proposal is None:
        return None
    for claim in proposal.coverage:
        if claim.obligation_id == obligation_id:
            return claim
    return None


def _next_action(obligation: Obligation) -> str:
    if obligation.kind is PropositionKind.FILE_CHANGED:
        return "write the target file and record a write receipt"
    if obligation.kind is PropositionKind.FILE_CREATED:
        return "create the target file and record a create receipt"
    if obligation.kind is PropositionKind.TEST_PASSED:
        return "run the target test and record a passing test receipt"
    if obligation.kind is PropositionKind.COMMIT_CREATED:
        return "create a delivery commit through the finalizer"
    if obligation.kind is PropositionKind.PUSHED:
        return "produce or remove the push receipt"
    return "provide deterministic evidence or ask the user to accept the gap"


class AuditCoordinator:
    """Build a V2 coverage matrix; never close debt itself."""

    def __init__(self, binder: CoverageBinder | None = None) -> None:
        self._binder = binder or CoverageBinder()

    def audit(
        self,
        obligations: Sequence[Obligation],
        ledger: Any,
        proposal: AuditProposal | None = None,
        *,
        defer_commit: bool = False,
    ) -> AuditReport:
        facts = index_facts(ledger)
        rows: list[CoverageRow] = []
        gaps: list[GapProposal] = list(proposal.gaps if proposal else ())
        seen: set[str] = set()
        for obligation in obligations:
            if obligation.id in seen:
                continue
            seen.add(obligation.id)
            if defer_commit and obligation.kind is PropositionKind.COMMIT_CREATED:
                continue
            row = self._binder.bind(
                obligation, ledger, proposal=proposal, facts=facts
            )
            rows.append(row)
            if row.status is not ResolutionStatus.BOUND:
                gaps.append(
                    GapProposal(
                        obligation_id=row.obligation_id,
                        reason=row.reason or row.status.value,
                        next_action=row.next_action,
                    )
                )
        verdicts = tuple(_verdict(row) for row in rows)
        return AuditReport(
            coverage=tuple(rows),
            gaps=tuple(gaps),
            verdicts=verdicts,
            proposal=proposal,
        )


def _verdict(row: CoverageRow) -> Verdict:
    if row.status is ResolutionStatus.BOUND:
        return Verdict(
            obligation_id=row.obligation_id,
            stage=VerificationStage.V2,
            status=VerdictStatus.SATISFIED,
            fact_ids=row.fact_ids,
        )
    if row.status is ResolutionStatus.UNRESOLVED:
        return Verdict(
            obligation_id=row.obligation_id,
            stage=VerificationStage.V2,
            status=VerdictStatus.VIOLATED,
            reason=row.reason or "unresolved coverage",
            fact_ids=row.fact_ids,
        )
    return Verdict(
        obligation_id=row.obligation_id,
        stage=VerificationStage.V2,
        status=VerdictStatus.UNVERIFIABLE,
        reason=row.reason or row.status.value,
        fact_ids=row.fact_ids,
    )


__all__ = ["AuditCoordinator", "CoverageBinder"]
