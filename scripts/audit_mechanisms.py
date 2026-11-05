"""Audit the mechanisms this project claims, against a real ledger (#421).

The rule that produced this file: *do not accept "done" from code reading or
from a green test run — look for the event the mechanism is supposed to write.*
A mechanism that cannot be observed in a ledger either did not run, or does not
record itself; both are findings.

Usage:
    python scripts/audit_mechanisms.py <ledger.jsonl> [<ledger.jsonl> ...]

Exit code is 1 when any mechanism is **missing** (not merely "not exercised":
a task that never needed approvals is not evidence of a broken approval path).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

Event = Mapping[str, object]


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    #: Events this mechanism is responsible for writing.
    expects: tuple[str, ...]
    predicate: Callable[..., tuple[str, str]]
    #: Predicates decide `n/a` themselves, so a mechanism that *should* have
    #: run and did not is always reported as a finding.


def _kinds(events: Iterable[Event]) -> set[str]:
    return {str(item.get("kind") or "") for item in events}


def _payloads(events: Iterable[Event], kind: str) -> list[Mapping[str, object]]:
    return [
        dict(item.get("payload") or {})
        for item in events
        if str(item.get("kind") or "") == kind
    ]


def _verdict_stages(events: Sequence[Event]) -> set[str]:
    stages: set[str] = set()
    for item in events:
        if str(item.get("kind") or "") != "step_verdict":
            continue
        stage = str((item.get("payload") or {}).get("stage") or "")
        if stage:
            stages.add(stage)
    for item in events:
        if str(item.get("kind") or "") != "audit_reported":
            continue
        for verdict in (item.get("payload") or {}).get("verdicts") or ():
            stage = str((verdict or {}).get("stage") or "")
            if stage:
                stages.add(stage)
    return stages


def _check_v0(events: Sequence[Event]) -> tuple[str, str]:
    count = sum(
        1
        for e in events
        if str(e.get("kind")) == "step_verdict"
        and str((e.get("payload") or {}).get("stage")) == "v0"
    )
    receipts = len(_payloads(events, "tool_receipt"))
    if not receipts:
        return ("没有执行任何工具（未触发）", "n/a")
    if not count:
        return (f"{receipts} 次调用但 0 条 v0 verdict", "miss")
    return (f"{count} 条 v0 verdict", "ok")


def _check_levels(events: Sequence[Event]) -> str:
    stages = _verdict_stages(events)
    if "audit_reported" not in _kinds(events):
        # The task level only runs when the run reaches delivery; a run that
        # blocked earlier is not evidence that V2 is broken.
        return ("未到任务级（本回合未交付）", "n/a")
    missing = {"v0", "v1", "v2"} - stages
    if not missing:
        return ("三级齐备: " + ", ".join(sorted(stages)), "ok")
    return ("缺: " + ", ".join(sorted(missing)), "miss")


def _check_claims(events: Sequence[Event]) -> str:
    proposed = len(_payloads(events, "claim_proposed"))
    resolved = _payloads(events, "claim_resolved")
    bound = sum(1 for item in resolved if str(item.get("status")) == "bound")
    if not proposed:
        return ("本次任务没有 claim（未触发）", "n/a")
    return (f"{proposed} 提出 / {bound} 绑定到事实", "ok")


def _check_delivery(events: Sequence[Event]) -> str:
    finals = _payloads(events, "finalize_started")
    if not finals:
        return ("没有 finalize（任务未到交付阶段）", "n/a")
    last = finals[-1]
    return (
        f"verified_paths={list(last.get('verified_paths') or ())} "
        f"unverified={list(last.get('unverified_paths') or ())} "
        f"commit={last.get('commit')}",
        "ok",
    )


def _check_loops(events: Sequence[Event]) -> str:
    loops = _payloads(events, "loop_detected")
    forced = [
        item
        for item in _payloads(events, "control_decision")
        if item.get("decision") == "force_finish"
    ]
    if not loops:
        return ("本次没有循环（未触发）", "n/a")
    return (f"{len(loops)} 次检测 / {len(forced)} 次收尾", "ok")


def _check_teaching(events: Sequence[Event]) -> str:
    """"Failure-as-teaching" must be observable, not just an internal note."""

    taught = [
        item
        for item in _payloads(events, "control_decision")
        if item.get("decision") in {"teach_next_step", "advice"}
    ]
    failures = [
        item
        for item in _payloads(events, "tool_receipt")
        if str((item.get("receipt") or {}).get("status")) not in {"ok", ""}
    ]
    if not failures:
        return ("本次没有失败调用（未触发）", "n/a")
    if not taught:
        return (f"{len(failures)} 次失败，但账本里 0 条教学提示", "miss")
    return (f"{len(failures)} 次失败 / {len(taught)} 条已记账的教学提示", "ok")


def _check_lessons(events: Sequence[Event]) -> str:
    lessons = _payloads(events, "lesson_proposed")
    if not lessons:
        return ("没有 lesson 提案（未触发）", "n/a")
    return (f"{len(lessons)} 条 lesson 提案", "ok")


def _check_approvals(events: Sequence[Event]) -> str:
    approvals = _payloads(events, "user_approval")
    if not approvals:
        return ("本次没有审批（未触发）", "n/a")
    return (f"{len(approvals)} 次审批，最后 approved={approvals[-1].get('approved')}", "ok")


def _check_usage(events: Sequence[Event]) -> str:
    usage = _payloads(events, "model_usage")
    if not usage:
        return ("没有 model_usage（账本无法解释成本）", "miss")
    last = usage[-1]
    return (
        f"{len(usage)} 次调用, 末次 prompt={last.get('prompt_tokens')} "
        f"estimated={last.get('estimated')}",
        "ok",
    )


def _check_chain(events: Sequence[Event], path: Path) -> tuple[str, str]:
    """A ledger that cannot prove its own integrity is not evidence."""

    try:
        # Known ordering quirk: the kernel contracts must be imported before
        # the ledger store, or `xenon.ledger.events` is left half-initialised.
        import xenon.kernel.contracts  # noqa: F401
        from xenon.ledger.store import Ledger

        report = Ledger(path, path.stem).verify()
    except Exception as exc:  # noqa: BLE001 — an unreadable chain is a finding
        return (f"无法核验: {type(exc).__name__}: {exc}", "miss")

    detail = (
        f"valid={report.valid} events={report.events} "
        f"tail={'truncated' if report.truncated_tail else 'intact'} "
        f"keyed={report.keyed}"
    )
    return (detail, "ok" if report.valid else "miss")


def _check_compaction(events: Sequence[Event]) -> tuple[str, str]:
    compacted = _payloads(events, "context_compacted")
    if not compacted:
        return ("本次未压缩（未触发）", "n/a")
    kept = sum(len(item.get("kept") or ()) for item in compacted)
    return (f"{len(compacted)} 次压缩, 保留 {kept} 条决策", "ok")


def _check_children(events: Sequence[Event]) -> tuple[str, str]:
    started = _payloads(events, "task_started")
    if not started:
        return ("没有 task_started", "miss")
    child = [item for item in started if item.get("parent_task_id")]
    if not child:
        return ("本次没有子任务（未触发）", "n/a")
    with_batch = sum(1 for item in child if item.get("batch_id"))
    return (f"{len(child)} 个子任务, {with_batch} 个带 batch_id", "ok")


def _check_prefix_cache(events: Sequence[Event]) -> tuple[str, str]:
    usage = _payloads(events, "model_usage")
    if not usage:
        return ("没有用量记录（未触发）", "n/a")
    reported = [item for item in usage if item.get("cache_hit_tokens")]
    if not reported:
        return ("提供方未报告缓存命中（未触发）", "n/a")
    hits = sum(int(item.get("cache_hit_tokens") or 0) for item in reported)
    prompts = sum(int(item.get("prompt_tokens") or 0) for item in reported) or 1
    return (f"{len(reported)} 次命中, 约 {hits * 100 // prompts}% 前缀复用", "ok")


CHECKS: tuple[Check, ...] = (
    Check("V0 动作级校验", ("step_verdict",), _check_v0),
    Check("三级齐备 V0/V1/V2", ("step_verdict",), _check_levels),
    Check("claim → fact 绑定", ("claim_proposed", "claim_resolved"), _check_claims),
    Check("交付（finalizer）", ("finalize_started", "delivered"), _check_delivery),
    Check("循环检测 + 收尾", ("loop_detected",), _check_loops),
    Check("失败即教学（可观测）", ("control_decision",), _check_teaching),
    Check("自我进化（lesson）", ("lesson_proposed",), _check_lessons),
    Check("审批边界", ("user_approval",), _check_approvals),
    Check("用量记账", ("model_usage",), _check_usage),
    Check("账本链完整性", ("ledger",), _check_chain),
    Check("上下文压缩", ("context_compacted",), _check_compaction),
    Check("子任务隔离", ("task_started",), _check_children),
    Check("前缀缓存复用", ("model_usage",), _check_prefix_cache),
)


def audit(path: Path) -> list[tuple[str, str, str]]:
    events = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rows: list[tuple[str, str, str]] = []
    for check in CHECKS:
        if check.name == "账本链完整性":
            detail, state = check.predicate(events, path)
        else:
            detail, state = check.predicate(events)
        rows.append((check.name, detail, state))
    return rows


def main(argv: list[str]) -> int:
    paths = [Path(item) for item in argv[1:]]
    if not paths:
        print(__doc__)
        return 2
    failed = 0
    for path in paths:
        print(chr(10) + "=== " + path.name + " ===")
        for name, detail, state in audit(path):
            mark = {"ok": "OK  ", "miss": "MISS", "n/a": "n/a "}[state]
            if state == "miss":
                failed += 1
            print(f"  {mark} {name:<24} {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))