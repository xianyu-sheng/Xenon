"""Task-level verification: acceptance criteria + final-answer audit.

The differentiation layer: before a task result is published, the
deterministic verifier checks the answer against the tool receipts —

* failure receipts contradicting a "success/done" claim;
* user-declared acceptance criteria (CI green / tests pass / build ok)
  that have no matching successful execution evidence.

This is the publish gate's first, non-blocking iteration: a failed
verification is printed prominently and recorded as a fact, but the answer
is still shown (blocking publication is the next iteration).
"""

from __future__ import annotations

import re

_ACCEPTANCE_RE = re.compile(
    r"(?:CI|流水线)\s*(?:通过|绿|成功)"
    r"|测试(?:全部|全|都)?(?:通过|绿|成功|全绿)"
    r"|pytest(?:\s+全部)?\s*通过"
    r"|(?:编译|构建|打包)(?:通过|成功)"
    r"|覆盖率\s*(?:>=|≥)\s*\d+"
    r"|不报错|没有错误|零错误|无失败|全绿"
    r"|tests?\s+pass|CI\s+green|CI\s+passes",
    re.IGNORECASE,
)
_SUCCESS_CLAIM = re.compile(
    r"(已(?:经)?(?:成功)?完成|全部通过|全绿|任务完成|目标(?:已)?达成"
    r"|通过验证|验证通过|修复完成|问题已解决|已修复|成功完成|成功修复"
    r"|All\s+\S*\s*passed)",
    re.IGNORECASE,
)
_FAILURE_CLAIM = re.compile(
    r"(失败|未通过|不通过|报错|failed|failures?|❌)", re.IGNORECASE
)


def extract_acceptance_criteria(text: str) -> list[str]:
    """Checklist-style extraction (constraint, not general intent)."""

    criteria: list[str] = []
    for match in _ACCEPTANCE_RE.finditer(text or ""):
        snippet = match.group(0).strip()
        if snippet and snippet not in criteria:
            criteria.append(snippet)
    return criteria


def verify_final_answer(
    answer: str,
    *,
    criteria: list[str] | tuple[str, ...] = (),
    tool_events: list[dict] | tuple[dict, ...] = (),
) -> tuple[bool, list[str]]:
    """Audit the final answer against receipts; returns (ok, reasons)."""

    reasons: list[str] = []
    events = list(tool_events)
    failures = [e for e in events if not e.get("success")]

    # 1) 失败回执与"成功/完成"宣称冲突。
    if failures and _SUCCESS_CLAIM.search(answer or "") and not (
        _FAILURE_CLAIM.search(answer or "")
    ):
        reasons.append(
            f"有 {len(failures)} 个失败的工具调用，但回答宣称成功"
        )

    # 2) 完成准则需要对应的成功证据。
    for criterion in criteria:
        if re.search(r"CI|流水线|pytest|测试|test|全绿|无失败|报错", criterion):
            failed_commands = [
                e
                for e in failures
                if str(e.get("tool", "")).lower() in {"command", "git", "pytest"}
            ]
            ok_commands = [
                e
                for e in events
                if e.get("success")
                and str(e.get("tool", "")).lower() in {"command", "git", "pytest"}
            ]
            if failed_commands or not ok_commands:
                reasons.append(f"完成准则未满足: {criterion}")

    return (not reasons), reasons
