"""The deterministic verdict type shared by tool-level evidence checks.

History: this module used to carry the engine's session-level gate pipeline
(``EvidenceGate`` subclasses, file-claim verification, patch binding).  The
kernel superseded that pipeline — obligations come from the plan, coverage is
decided by the V2 binder, and a claim without a receipt becomes debt — so the
gate classes and their helpers were retired with it.

What remains is :class:`GateVerdict`, the closed result type the tool executor
still produces when it checks a write before allowing it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# ── 校验结果契约 ──────────────────────────────────────────
@dataclass(slots=True)
class GateVerdict:
    """单个 Gate 的确定性校验结果。

    passed=False 时由循环决定补救动作；reason 是人类可读原因，
    payload 携带可选的补救素材（如需要追加到 LLM 输出的警告文本）。
    """

    phase: str
    passed: bool
    reason: str = ""
    severity: str = "warning"  # info | warning | error
    payload: Any = None
