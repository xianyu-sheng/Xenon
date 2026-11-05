#!/usr/bin/env python
"""Delivery report: the artifact a reviewer reads.

The report is the artifact a reviewer reads: what the agent claimed, what the
receipts actually prove, and where those two disagree.  It is deliberately
read-only and offline — the same ledger copied to another machine produces the
same report.

Callable from the CLI (``xenon report``) and from ``scripts/delivery_report.py``.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from xenon.ledger.store import Ledger
from xenon.replay import resolve_ledger

SCHEMA = "xenon.delivery_report/v0"

_SECRET_PATTERNS = (
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
)


def _payload(event: Any) -> Mapping[str, Any]:
    value = getattr(event, "payload", {})
    return value if isinstance(value, Mapping) else {}


def _kind(event: Any) -> str:
    kind = getattr(event, "kind", "")
    return getattr(kind, "value", str(kind))


def redact(value: Any, *, home: Path | None = None) -> Any:
    """Remove the local home path and obvious credentials from report text."""

    if isinstance(value, Mapping):
        return {str(k): redact(v, home=home) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, home=home) for item in value]
    if not isinstance(value, str):
        return value
    text = value
    home = home or Path.home()
    try:
        text = text.replace(str(home), "~")
        text = text.replace(str(home).replace("\\", "/"), "~")
    except (OSError, ValueError):
        pass
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def _first(events: Sequence[Any], kind: str) -> Mapping[str, Any]:
    for event in events:
        if _kind(event) == kind:
            return _payload(event)
    return {}


def _last(events: Sequence[Any], kind: str) -> Mapping[str, Any]:
    for event in reversed(events):
        if _kind(event) == kind:
            return _payload(event)
    return {}


def _all(events: Sequence[Any], kind: str) -> list[Mapping[str, Any]]:
    return [_payload(event) for event in events if _kind(event) == kind]


def _model_from(constraints: Iterable[Any]) -> str:
    for item in constraints:
        text = str(item)
        if text.startswith("model:"):
            return text.partition(":")[2]
    return ""


def _phase(events: Sequence[Any]) -> str:
    for event in reversed(events):
        if _kind(event) != "state_transition":
            continue
        target = _payload(event).get("to")
        if target:
            return str(target)
    return ""


def _test_runs(receipts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for item in receipts:
        receipt = item.get("receipt") or {}
        for effect in receipt.get("effects") or []:
            if not isinstance(effect, Mapping):
                continue
            if effect.get("type") != "test_run":
                continue
            runs.append(
                {
                    "command": str(effect.get("command") or ""),
                    "target": str(effect.get("target") or ""),
                    "passed": int(effect.get("passed") or 0),
                    "failed": int(effect.get("failed") or 0),
                    "errors": int(effect.get("errors") or 0),
                    "skipped": int(effect.get("skipped") or 0),
                    "exit_code": effect.get("exit_code"),
                }
            )
    return runs


def _effect_paths(receipts: Sequence[Mapping[str, Any]]) -> list[str]:
    paths: set[str] = set()
    for item in receipts:
        receipt = item.get("receipt") or {}
        for effect in receipt.get("effects") or []:
            if not isinstance(effect, Mapping):
                continue
            if effect.get("type") in {"fs_write", "fs_delete"} and effect.get(
                "path"
            ):
                paths.add(str(effect["path"]))
    return sorted(paths)


def build_report(ledger_path: Path, *, result_path: Path | None = None) -> dict[str, Any]:
    ledger = Ledger.open(ledger_path)
    events = ledger.read()
    integrity = ledger.verify()

    started = _first(events, "task_started")
    intake = _first(events, "intake_recorded")
    delivered = _last(events, "delivered")
    finalized = _last(events, "finalized")
    finalize_started = _last(events, "finalize_started")

    result: Mapping[str, Any] = {}
    if result_path is None:
        sibling = ledger_path.with_suffix(".result.json")
        if sibling.is_file():
            result_path = sibling
    if result_path is not None and result_path.is_file():
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            result = {}

    receipts = _all(events, "tool_receipt")
    claims = _all(events, "claim_proposed")
    resolutions = {str(item.get("claim_id") or ""): item for item in _all(
        events, "claim_resolved"
    )}
    resolved_claims: list[dict[str, Any]] = []
    contradictions: list[dict[str, Any]] = []
    for claim in claims:
        claim_id = str(claim.get("claim_id") or claim.get("id") or "")
        resolution = resolutions.get(claim_id, {})
        status = str(resolution.get("status") or "unresolved")
        row = {
            "id": claim_id,
            "kind": str(claim.get("kind") or ""),
            "target": str(claim.get("target") or ""),
            "status": status,
            "reason": str(resolution.get("reason") or ""),
            "fact_ids": list(resolution.get("fact_ids") or ()),
        }
        resolved_claims.append(row)
        if status in {"unresolved", "ambiguous", "abstained"}:
            contradictions.append(
                {
                    "kind": "claim_not_bound",
                    "claim": row,
                    "detail": (
                        "the claim has no matching receipt fact"
                        if status == "unresolved"
                        else f"claim resolution is {status}"
                    ),
                }
            )

    # A violated verdict is *unfinished work*, not a contradiction: the agent
    # did not claim it and the receipts never said it happened.  Contradiction
    # is reserved for a claim that facts refuse, or content that changed after
    # verification.  Mixing the two made every blocked run read as deceptive.
    verdicts = _all(events, "step_verdict")

    for mismatch in list(finalize_started.get("hash_mismatches") or ()):
        contradictions.append(
            {
                "kind": "hash_mismatch",
                "claim": {"id": str(mismatch), "kind": "artifact", "target": ""},
                "detail": "artifact content changed after verification",
            }
        )

    gaps = [
        {
            "id": str(item.get("obligation_id") or ""),
            "stage": str(item.get("stage") or ""),
            "kind": str(item.get("kind") or ""),
            "target": str(item.get("target") or ""),
            "reason": str(item.get("reason") or ""),
            "attempts": int(item.get("attempts") or 0),
        }
        for item in _all(events, "debt_opened")
    ]

    by_tool: dict[str, int] = {}
    failures = 0
    for item in receipts:
        receipt = item.get("receipt") or {}
        tool = str(receipt.get("tool") or "")
        by_tool[tool] = by_tool.get(tool, 0) + 1
        if str(receipt.get("status") or "") != "ok":
            failures += 1

    evidence = (
        str(result.get("evidence_status") or "")
        or str(finalized.get("evidence_status") or "")
        or "unverified"
    )
    commit = str(
        result.get("finalize_sha")
        or finalized.get("commit")
        or finalize_started.get("commit")
        or ""
    )

    report = {
        "schema": SCHEMA,
        "task": {
            "id": str(started.get("task_id") or ledger.task_id or ledger_path.stem),
            "objective": str(started.get("objective") or ""),
            "user_objective": str(
                started.get("user_objective") or started.get("objective") or ""
            ),
            "workspace": str(started.get("workspace") or ""),
            "model": _model_from(started.get("constraints") or ()),
            "mode": str(started.get("mode") or ""),
            "constraints": list(started.get("constraints") or ()),
            "obligations": list(intake.get("obligations") or ()),
        },
        "status": {
            "phase": str(result.get("phase") or _phase(events) or ""),
            "accepted": bool(result.get("accepted", bool(delivered))),
            "evidence_status": evidence,
            # The harness status describes *receipts*, not the answer.  A run
            # can have verified tool receipts and still make no checkable claim
            # (a research summary is the common case).  Team readers need both.
            "answer_verified": bool(claims)
            and evidence == "verified"
            and not contradictions,
            "delivered": bool(delivered),
            "commit": commit,
        },
        "integrity": {
            "valid": bool(getattr(integrity, "valid", False)),
            "reason": str(getattr(integrity, "reason", "") or ""),
            "events": int(getattr(integrity, "events", len(events)) or 0),
            "last_seq": int(getattr(integrity, "last_seq", 0) or 0),
            "last_hash": str(getattr(integrity, "last_hash", "") or ""),
        },
        "claims": resolved_claims,
        "verdicts": [
            {
                "stage": str(item.get("stage") or ""),
                "status": str(item.get("status") or ""),
                "obligation_id": str(item.get("obligation_id") or ""),
                "reason": str(item.get("reason") or ""),
                "from_proposal": bool(item.get("from_proposal", False)),
            }
            for item in verdicts
        ],
        "gaps": gaps,
        "delivery": {
            "verified_paths": list(finalize_started.get("verified_paths") or ()),
            "unverified_paths": list(finalize_started.get("unverified_paths") or ()),
            "receipt_paths": _effect_paths(receipts),
            "hash_mismatches": list(finalize_started.get("hash_mismatches") or ()),
        },
        "tools": {
            "calls": len(receipts),
            "failures": failures,
            "by_tool": by_tool,
            "tests": _test_runs(receipts),
        },
        "contradictions": contradictions,
        "answer": str(delivered.get("answer") or result.get("answer") or ""),
        "replay": {
            "ledger": str(ledger_path),
            "command": f"xenon replay {ledger.task_id or ledger_path.stem}",
        },
        "limits": {
            "note": (
                "Facts come from receipts, git state and test exit codes. "
                "Natural-language claims are model proposals; a claim marked "
                "unresolved is not evidence of lying, only of no matching fact."
            )
        },
    }
    return redact(report)


def _truncate(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n… ({len(text) - limit} chars omitted)"


def render_comment(report: Mapping[str, Any]) -> str:
    """A compact form for a PR comment: verdict first, noise last.

    A reviewer decides in the first three lines; the full report belongs in an
    artifact or a collapsed block, not in the comment body.
    """

    task = report.get("task", {})
    status = report.get("status", {})
    integrity = report.get("integrity", {})
    tools = report.get("tools", {})
    delivery = report.get("delivery", {})
    contradictions = list(report.get("contradictions") or ())
    gaps = list(report.get("gaps") or ())
    claims = list(report.get("claims") or ())
    unbound = [item for item in claims if item.get("status") != "bound"]
    tests = list(tools.get("tests") or ())

    if contradictions:
        verdict = "❌ 交付与事实冲突"
    elif status.get("answer_verified"):
        verdict = "✅ 交付与事实一致"
    elif status.get("delivered"):
        verdict = "⚠️ 已交付，但结论未经验证"
    else:
        verdict = "⚠️ 未交付（缺口已披露）"

    lines = [
        f"### {verdict}",
        "",
        f"`{task.get('id', '')}` · 模型 `{task.get('model', '') or 'unknown'}` · "
        f"工具调用 {tools.get('calls', 0)} 次"
        f"（失败 {tools.get('failures', 0)}）· "
        f"commit `{(status.get('commit') or 'none')[:12]}`",
        "",
    ]

    if claims:
        lines.append(
            f"**声明 {len(claims)} 条，绑定失败 {len(unbound)} 条**"
        )
        for claim in unbound[:5]:
            lines.append(
                f"- `{claim.get('kind')}` `{claim.get('target')}` → "
                f"**{claim.get('status')}**"
            )
    else:
        lines.append("**没有可核验的声明**（答案按未验证披露）")

    if tests:
        passed = sum(int(item.get("passed") or 0) for item in tests)
        failed = sum(int(item.get("failed") or 0) for item in tests)
        lines.append(f"\n**测试**：{passed} passed / {failed} failed")

    if contradictions:
        lines.append(f"\n**矛盾 {len(contradictions)} 条**")
        for item in contradictions[:5]:
            claim = item.get("claim", {})
            lines.append(
                f"- {item.get('kind')}: `{claim.get('kind')}` "
                f"`{claim.get('target')}` — {item.get('detail')}"
            )

    if gaps:
        lines.append(f"\n**未完成 {len(gaps)} 项**")
        for gap in gaps[:5]:
            lines.append(f"- `{gap.get('id')}`: {gap.get('reason')}")

    if delivery.get("unverified_paths"):
        lines.append(
            "\n**未验证路径**："
            + ", ".join(f"`{p}`" for p in delivery["unverified_paths"][:8])
        )

    lines += [
        "",
        "<details><summary>如何复现</summary>",
        "",
        f"- 账本：`{report.get('replay', {}).get('ledger', '')}`",
        f"- 命令：`{report.get('replay', {}).get('command', '')}`",
        f"- 链完整性：**{integrity.get('valid')}** "
        f"（{integrity.get('events')} 事件，head "
        f"`{str(integrity.get('last_hash', ''))[:12]}…`）",
        "",
        "</details>",
        "",
        f"> {report.get('limits', {}).get('note', '')}",
        "",
    ]
    return "\n".join(lines)


def render_markdown(report: Mapping[str, Any]) -> str:
    task = report.get("task", {})
    status = report.get("status", {})
    integrity = report.get("integrity", {})
    tools = report.get("tools", {})
    delivery = report.get("delivery", {})
    contradictions = list(report.get("contradictions") or ())
    gaps = list(report.get("gaps") or ())
    claims = list(report.get("claims") or ())

    lines = [
        f"# Delivery Report — `{task.get('id', '')}`",
        "",
        f"**Receipts:** `{status.get('evidence_status', '')}` · "
        f"**Claims:** {len(claims)} · "
        f"**Answer:** `{'verified' if status.get('answer_verified') else 'unverified'}` · "
        f"**Phase:** `{status.get('phase', '')}` · "
        f"**Commit:** `{status.get('commit') or 'none'}`",
        "",
        "## Task",
        "",
        f"- Objective: {task.get('objective', '')}",
        f"- Model: `{task.get('model', '')}`",
        f"- Workspace: `{task.get('workspace', '')}`",
        "",
        "## Claims vs receipts",
        "",
    ]
    if claims:
        lines += [
            "| Claim | Target | Resolution |",
            "|---|---|---|",
        ]
        for claim in claims:
            lines.append(
                f"| `{claim.get('kind')}` | `{claim.get('target')}` | "
                f"**{claim.get('status')}** |"
            )
    else:
        lines.append("_No explicit claims. The answer is unverified by design._")

    lines += ["", "## What actually ran", ""]
    lines.append(
        f"- Tool calls: **{tools.get('calls', 0)}** "
        f"({tools.get('failures', 0)} failed)"
    )
    for name, count in sorted((tools.get("by_tool") or {}).items()):
        lines.append(f"  - `{name}` × {count}")
    tests = list(tools.get("tests") or ())
    if tests:
        lines.append("")
        lines.append("| Test command | Passed | Failed | Exit |")
        lines.append("|---|---:|---:|---:|")
        for test in tests:
            lines.append(
                f"| `{test.get('command')}` | {test.get('passed')} | "
                f"{test.get('failed')} | {test.get('exit_code')} |"
            )

    lines += ["", "## Where claims and facts disagree", ""]
    if contradictions:
        for item in contradictions:
            claim = item.get("claim", {})
            lines.append(
                f"- **{item.get('kind')}** — `{claim.get('kind')}` "
                f"`{claim.get('target')}`: {item.get('detail')}"
            )
    else:
        lines.append("_No contradiction detected._")

    lines += ["", "## Open gaps", ""]
    if gaps:
        for gap in gaps:
            lines.append(
                f"- `{gap.get('id')}` ({gap.get('stage')}): {gap.get('reason')}"
            )
    else:
        lines.append("_No open gaps._")

    lines += [
        "",
        "## Delivery",
        "",
        f"- Verified paths: {', '.join(f'`{p}`' for p in delivery.get('verified_paths') or ()) or 'none'}",
        f"- Unverified paths: {', '.join(f'`{p}`' for p in delivery.get('unverified_paths') or ()) or 'none'}",
        "",
        "## Reproduce",
        "",
        f"- Ledger: `{report.get('replay', {}).get('ledger', '')}`",
        f"- Command: `{report.get('replay', {}).get('command', '')}`",
        f"- Chain valid: **{integrity.get('valid')}** "
        f"({integrity.get('events')} events, head `{str(integrity.get('last_hash', ''))[:12]}…`)",
        "",
        "## Answer",
        "",
        "```text",
        _truncate(str(report.get("answer") or "")).strip(),
        "```",
        "",
        f"> {report.get('limits', {}).get('note', '')}",
        "",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    import sys

    # Windows consoles default to GBK; the report is UTF-8 (Chinese objectives
    # are common).  Reconfigure rather than emitting mojibake.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ledger", help="task id or path to a .jsonl ledger")
    parser.add_argument("--out", type=Path, default=None, help="output directory")
    parser.add_argument(
        "--json", action="store_true", help="print JSON instead of Markdown"
    )
    parser.add_argument(
        "--comment",
        action="store_true",
        help="compact PR-comment form (verdict first, reproduce steps collapsed)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 unless the answer is verified (for a CI gate)",
    )
    args = parser.parse_args(argv)

    path = resolve_ledger(args.ledger)
    report = build_report(path)
    if args.json:
        text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
        suffix = ".report.json"
    elif args.comment:
        text = render_comment(report)
        suffix = ".comment.md"
    else:
        text = render_markdown(report)
        suffix = ".report.md"

    if args.out is not None:
        args.out.mkdir(parents=True, exist_ok=True)
        target = args.out / (path.stem + suffix)
        target.write_text(text + "\n", encoding="utf-8")
        print(target)
    else:
        print(text)

    if not report["integrity"]["valid"]:
        return 1
    if args.strict and not report["status"].get("answer_verified"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
