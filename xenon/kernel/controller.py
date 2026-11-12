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
    Capability,
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
    WorkspaceProbe,
)
from xenon.ledger.events import (
    EventKind,
    effect_from_payload,
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
    # A workspace probe is an observation, not a tool call: it gets its own
    # event so that "plan-only runs execute no tools" stays literally true and
    # tool counts are not inflated by host bookkeeping.
    for event in ledger.read(EventKind.WORKSPACE_PROBED, up_to_seq=up_to_seq):
        payload = dict(event.payload or {})
        if payload.get("type") != "workspace_probe":
            continue
        facts.append(
            Fact(
                fact_id=f"fact-{event.seq}-workspace",
                effect=effect_from_payload(payload),
                seq=event.seq,
                receipt_id="workspace_probe",
            )
        )

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
    if not effect.target:
        # The recorded run was the whole suite: it covers any specific target.
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
        if claim.since_seq and claim.kind is not PropositionKind.NO_STRAY_CHANGES:
            facts = tuple(item for item in facts if item.seq > claim.since_seq)
        # ``no_stray_changes`` is a *state* predicate: what matters is the
        # newest observation of the worktree, not only observations made after
        # the obligation was declared (the probe usually predates the plan).
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
            return self._resolve_workspace_clean(claim, facts)

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

    def _resolve_workspace_clean(
        self, claim: Claim, facts: Sequence[Fact]
    ) -> Resolution:
        """Bind "no stray changes" to the host's worktree probe.

        The probe is taken by the host (the kernel never shells out) and
        recorded as a receipt effect, so this stays a ledger fact: the same
        ledger always answers the same way.  No probe in the ledger means the
        honest answer is still "cannot tell".
        """

        probes = [
            fact for fact in facts if isinstance(fact.effect, WorkspaceProbe)
        ]
        if not probes:
            return Resolution(
                claim_id=claim.id,
                status=ResolutionStatus.ABSTAINED,
                reason="no workspace probe in the ledger yet",
            )
        probe = probes[-1].effect
        verified = {_normalized(item) for item in probe.verified_paths}
        stray = [
            path for path in probe.dirty_paths if _normalized(path) not in verified
        ]
        if stray:
            return Resolution(
                claim_id=claim.id,
                status=ResolutionStatus.UNRESOLVED,
                reason=(
                    "worktree contains unverified paths: "
                    + ", ".join(sorted(stray))
                ),
                fact_ids=(probes[-1].fact_id,),
            )
        return Resolution(
            claim_id=claim.id,
            status=ResolutionStatus.BOUND,
            fact_ids=(probes[-1].fact_id,),
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


def _implied_obligations(
    step: StepContract, *, since_seq: int | None = None
) -> tuple[Obligation, ...]:
    """What a step's own action promises, when the plan did not say (#419).

    Only ``edit`` is derived here.  It is the one action whose promise is
    unambiguous — "this step changes a file" — and the one that real plans
    never declared, which is why V1 never ran.  Reads are free (a read step
    that produced no new fact is a repeat, not a lie), verify steps are judged
    by the finish claim, and shell/remote steps by their receipt.

    Names of tools and parameters stay out of the kernel (K003): the decision
    is made from the capability alone.
    """

    intent = getattr(step, "intent", None)
    if intent is None:
        return ()
    if getattr(intent, "capability", None) is not Capability.EDIT:
        return ()
    targets = tuple(getattr(intent, "targets", ()) or ())
    return (
        Obligation(
            id=f"step-{step.id}-file-changed",
            kind=PropositionKind.FILE_CHANGED,
            target=targets[0] if targets else "",
            since_seq=int(since_seq or 0),
        ),
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
        self, step: StepContract, ledger: Any, *, since_seq: int | None = None
    ) -> tuple[Verdict, ...]:
        """V1: did this step produce what its own action implies?

        ``step.expects`` is the explicit form, but a model-authored plan almost
        never fills it (a real 6-receipt task declared ``expects: []`` on every
        step), which left V1 as code that never ran.  So when the plan is
        silent, the *action the step was executed with* supplies the
        expectation: an edit that writes nothing is a step that did not happen.

        This is a matching check, not a judgement about correctness: it asks
        "is there a fact for what you just did", never "was it the right thing".
        """

        verdicts: list[Verdict] = []
        for obligation in step.expects:
            verdicts.append(self._obligation_verdict(obligation, ledger))
        if verdicts:
            return tuple(verdicts)
        derived = _implied_obligations(step, since_seq=since_seq)
        for obligation in derived:
            verdicts.append(self._obligation_verdict(obligation, ledger))
        return tuple(verdicts)

    def resolve(self, claim: Claim, ledger: Any) -> Resolution:
        """Resolve one claim; callers use this to report resolutions honestly."""

        return self._resolver.resolve(claim, ledger)

    def resolve_many(
        self, claims: Sequence[Claim], ledger: Any
    ) -> tuple[Resolution, ...]:
        """Resolve several claims against **one** index of the ledger.

        ``resolve`` re-indexes the ledger per call, so resolving N claims that
        way scans the ledger N times.  Coverage asks about every obligation
        after every receipt, and obligations are uncapped (a 30-step plan has
        30+ obligations), so that is the difference between one pass and
        thirty.

        The judgement is unchanged: each claim reaches ``ClaimResolver._resolve``
        with exactly the facts ``resolve`` would have given it — the full list,
        or the `since_seq` slice, per claim.  Only the indexing is shared.
        """

        if not claims:
            return ()
        facts = index_facts(ledger)
        resolutions: list[Resolution] = []
        for claim in claims:
            window = facts
            # Same rule as `ClaimResolver.resolve`: a state predicate like
            # ``no_stray_changes`` describes now, not "since the obligation was
            # declared", so it is judged against every observation.
            if claim.since_seq and claim.kind is not PropositionKind.NO_STRAY_CHANGES:
                window = tuple(item for item in facts if item.seq > claim.since_seq)
            resolutions.append(self._resolver._resolve(claim, window))
        return tuple(resolutions)


    def audit(
        self, claim: Claim | None, ledger: Any, debt: Sequence[Debt]
    ) -> Verdict:
        if claim is None:
            # No claim means nothing was asserted, so there is nothing to
            # confirm.  This used to return SATISFIED, which made a task that
            # could not express a single verifiable obligation report itself as
            # verified: an unknown objective ("exec"), a prose-only ask, or an
            # intake that derived zero obligations.  Satisfaction with no
            # proposition is not evidence — an empty set trivially satisfies
            # "all are satisfied".
            return Verdict(
                obligation_id="task",
                stage=VerificationStage.V2,
                status=VerdictStatus.UNVERIFIABLE,
                reason="open debt" if debt else "no verifiable claim",
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
        # A gap the operator accepted stops blocking delivery, but it never
        # stops being a gap: the evidence status stays PARTIAL.
        accepted = {
            item.obligation.id
            for item in debt
            if item.acceptable and item.user_accepted
        }
        unverifiable = [
            item
            for item in verdicts
            if item.status is VerdictStatus.UNVERIFIABLE
            and item.obligation_id not in accepted
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
            blocking = [
                item
                for item in debt
                if not (item.acceptable and item.user_accepted)
            ]
            if blocking:
                return ControlDecision(
                    kind=ControlDecisionKind.BLOCK,
                    reason="open debt",
                    obligation_ids=tuple(
                        item.obligation.id for item in blocking
                    ),
                )
            # Every remaining gap is one the operator explicitly accepted and
            # that carries no evidence of failure.  Delivering is allowed; the
            # evidence status stays PARTIAL because nothing was verified.
            return ControlDecision(
                kind=ControlDecisionKind.DELIVER,
                reason=(
                    f"user accepted {len(debt)} unverified gap(s): "
                    + ", ".join(item.obligation.id for item in debt)
                ),
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
