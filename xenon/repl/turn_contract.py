"""TurnContract — one deterministic authorization contract per user turn.

Design (confirmed with the maintainer):

* The LLM classifier **leads** the open-ended semantic intent.
* The regex signal layer **owns constraints** (explicit negations, chat-only,
  no-tools) and explicit primitive structures (写到 X / 运行 X / 删除 X).
* A deterministic merge layer produces the contract:

  1. explicit negations always veto — no model may override them;
  2. explicit regex structures win over an LLM that missed them;
  3. chat-only / code-in-chat intents cannot invent file operations;
  4. low-confidence LLM write/execute proposals without regex evidence become
     ``ask_required`` instead of silent grants;
  5. if the classifier is disabled/failed/timed out, the previous regex-only
     policy is used verbatim (``degraded=True``).

Every layer (REPL routing, engines, evidence gates, strategy tips) must consume
the contract instead of calling ``classify_execution_policy`` again.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Protocol

from xenon.repl.execution_policy import (
    ExecutionLevel,
    ExecutionPolicy,
    ExecutionSignals,
    classify_execution_policy,
    extract_execution_signals,
)

logger = logging.getLogger(__name__)

_WRITE_OPERATIONS = frozenset({"write", "create", "delete", "move"})
_READ_OPERATIONS = frozenset({"read", "network"})
_CHAT_ONLY_INTENTS = frozenset({"chat", "explain", "design", "novel"})
_QUERY_INTENTS = frozenset({"query", "research"})


class IntentClassifierLike(Protocol):
    """Structural type for the LLM classifier (keeps tests network-free)."""

    enabled: bool
    confidence_threshold: float

    def classify(
        self,
        text: str,
        *,
        context_messages: list[dict] | None = None,
        hints: dict[str, Any] | None = None,
    ) -> Any: ...


@dataclass(frozen=True)
class TurnContract:
    """Authorization + capability contract for exactly one user turn."""

    level: ExecutionLevel
    intent: str | None
    operations: frozenset[str]
    proposed_level: ExecutionLevel
    confidence: float
    degraded: bool
    ask_required: bool
    reason: str
    evidence: tuple[str, ...]
    signals: ExecutionSignals
    classifier_reasoning: str = ""

    @property
    def requires_tools(self) -> bool:
        return self.level >= ExecutionLevel.READ_ONLY

    @property
    def allows_write(self) -> bool:
        return self.level >= ExecutionLevel.WRITE and not self.signals.no_write

    @property
    def allows_execute(self) -> bool:
        return self.level >= ExecutionLevel.EXECUTE and not self.signals.no_execute

    @property
    def locks_answer_only(self) -> bool:
        return self.level is ExecutionLevel.ANSWER_ONLY and (
            self.signals.no_write or self.signals.no_execute
        )

    def approve(self) -> "TurnContract":
        """Return the contract the user's explicit consent would produce."""

        if not self.ask_required:
            return self
        return replace(
            self,
            level=self.proposed_level,
            ask_required=False,
            reason=f"{self.reason}；用户已确认",
        )

    def to_execution_policy(self) -> ExecutionPolicy:
        """Compatibility shim for call sites not yet migrated to the contract."""

        return ExecutionPolicy(
            self.level,
            self.reason,
            explicit_no_write=self.signals.no_write,
            explicit_no_execute=self.signals.no_execute,
        )


def signals_to_hints(signals: ExecutionSignals) -> dict[str, Any]:
    """Serialize regex signals for the LLM classifier prompt (加固)."""

    return {
        "write_snippets": list(signals.write_snippets),
        "execute_snippets": list(signals.execute_snippets),
        "read_snippets": list(signals.read_snippets),
        "negations": list(signals.negation_snippets),
        "chat_only": signals.chat_only,
        "no_tools": signals.no_tools,
    }


def _regex_intent(text: str) -> str | None:
    from xenon.repl.prompt_optimizer import detect_intent

    return detect_intent(text)


def _operations_for_level(level: ExecutionLevel) -> frozenset[str]:
    if level >= ExecutionLevel.EXECUTE:
        return frozenset({"read", "execute"})
    if level >= ExecutionLevel.WRITE:
        return frozenset({"read", "write"})
    if level >= ExecutionLevel.READ_ONLY:
        return frozenset({"read"})
    return frozenset()


def _fallback_contract(
    text: str,
    signals: ExecutionSignals,
    intent: str | None,
    *,
    reason: str,
) -> TurnContract:
    """Regex-only degradation: keeps the pre-classifier behavior verbatim."""

    policy = classify_execution_policy(text, intent=intent)
    return TurnContract(
        level=policy.level,
        intent=intent,
        operations=_operations_for_level(policy.level),
        proposed_level=policy.level,
        confidence=1.0,
        degraded=True,
        ask_required=False,
        reason=f"{reason}：{policy.reason}",
        evidence=signals.write_patterns + signals.negation_snippets,
        signals=signals,
    )


def _constraint_contract(
    text: str,
    signals: ExecutionSignals,
    intent: str | None,
) -> TurnContract:
    """Deterministic hard constraints (negations / chat-only / no-tools)."""

    policy = classify_execution_policy(text, intent=intent)
    return TurnContract(
        level=policy.level,
        intent=intent,
        operations=frozenset(),
        proposed_level=policy.level,
        confidence=1.0,
        degraded=False,
        ask_required=False,
        reason=f"确定性约束优先：{policy.reason}",
        evidence=signals.negation_snippets,
        signals=signals,
    )


def build_turn_contract(
    text: str,
    *,
    classifier: IntentClassifierLike | None = None,
    context_messages: list[dict] | None = None,
    fallback_intent: str | None = None,
) -> TurnContract:
    """Build the single per-turn contract from regex signals + LLM intent.

    The regex layer always runs (cheap).  The LLM classifier is skipped
    entirely for hard-constraint turns, saving one call on "不要写文件"
    style requests and guaranteeing constraints can never be model-overridden.

    ``fallback_intent`` lets the REPL pass its context-resolved intent (e.g.
    a terse follow-up inheriting ``query``) so degradation stays identical to
    the previous regex-only behavior.
    """

    signals = extract_execution_signals(text)
    regex_intent = _regex_intent(text)
    resolved_intent = fallback_intent if fallback_intent is not None else regex_intent

    # 1) 硬约束：不调用 LLM，省一次调用且禁令不可被推翻。
    if (
        signals.no_tools
        or signals.chat_only
        or (signals.no_write and signals.no_execute)
    ):
        return _constraint_contract(text, signals, resolved_intent)

    enabled = bool(classifier is not None and getattr(classifier, "enabled", False))
    if not enabled:
        return _fallback_contract(text, signals, resolved_intent, reason="分类器未启用")

    try:
        result = classifier.classify(
            text,
            context_messages=context_messages,
            hints=signals_to_hints(signals),
        )
    except Exception as exc:  # noqa: BLE001 — 分类失败必须优雅降级
        logger.warning("LLM 意图分类失败，回退正则层: %s", exc)
        return _fallback_contract(text, signals, resolved_intent, reason="分类器调用失败")

    if result is None or (result.intent is None and not result.operations):
        return _fallback_contract(text, signals, resolved_intent, reason="分类器无有效输出")

    intent = result.intent or resolved_intent
    operations = set(result.operations)

    # 2) 显式禁令一票否决（即使 LLM 忽略）。
    if signals.no_write:
        operations -= _WRITE_OPERATIONS
    if signals.no_execute:
        operations.discard("execute")

    # 3) 显式结构优先：正则看到"写到 X/运行 X"，LLM 漏了也补回来。
    if signals.explicit_write and not signals.no_write:
        operations.add("write")
    if signals.explicit_execute and not signals.no_execute:
        operations.add("execute")
    if signals.read_evidence:
        operations.add("read")

    # 4) chat-only / 纯生成意图不得发明文件操作。
    if result.chat_only and not (
        signals.explicit_write or signals.explicit_execute
    ):
        operations.clear()
    if intent in _CHAT_ONLY_INTENTS and not (
        signals.explicit_write or signals.explicit_execute
    ):
        operations -= _WRITE_OPERATIONS | {"execute"}
    if intent == "write_code" and not signals.explicit_write:
        operations -= _WRITE_OPERATIONS | {"execute"}
        if not signals.read_evidence:
            operations -= _READ_OPERATIONS

    # 5) 低置信 + 无正则证据 + 想写/执行 → 询问而不是静默授权。
    threshold = float(getattr(classifier, "confidence_threshold", 0.7) or 0.7)
    ask_required = bool(
        result.confidence < threshold
        and operations & (_WRITE_OPERATIONS | {"execute"})
        and not (signals.explicit_write or signals.explicit_execute)
    )

    # 6) 查询/调研意图保持至少只读（与旧行为一致的下限）。
    if intent in _QUERY_INTENTS:
        operations.add("read")

    # 7) 操作 → 级别。
    if "execute" in operations:
        proposed = ExecutionLevel.EXECUTE
    elif operations & _WRITE_OPERATIONS:
        proposed = ExecutionLevel.WRITE
    elif operations & _READ_OPERATIONS:
        proposed = ExecutionLevel.READ_ONLY
    else:
        proposed = ExecutionLevel.ANSWER_ONLY

    # 需要询问时先只授权到只读，用户确认后 approve() 提升到 proposed。
    level = proposed
    if ask_required and proposed >= ExecutionLevel.WRITE:
        level = ExecutionLevel.READ_ONLY

    reason = (
        f"LLM 意图={intent} 置信度={result.confidence:.2f} "
        f"操作={sorted(operations)}；"
        f"正则写入证据={list(signals.write_patterns) or '无'}；"
        f"禁令={list(signals.negation_snippets) or '无'}"
    )
    if ask_required:
        reason += "；低置信且无显式证据 → 需用户确认"
    if signals.explicit_write and not result.operations:
        reason += "；显式结构覆盖了分类器遗漏"

    contract = TurnContract(
        level=level,
        intent=intent,
        operations=frozenset(operations),
        proposed_level=proposed,
        confidence=float(result.confidence),
        degraded=False,
        ask_required=ask_required,
        reason=reason,
        evidence=signals.write_patterns
        + signals.write_snippets
        + signals.negation_snippets,
        signals=signals,
        classifier_reasoning=str(result.reasoning or ""),
    )
    logger.debug(
        "turn_contract level=%s proposed=%s intent=%s ops=%s conf=%.2f ask=%s degraded=%s",
        contract.level.name,
        contract.proposed_level.name,
        contract.intent,
        sorted(contract.operations),
        contract.confidence,
        contract.ask_required,
        contract.degraded,
    )
    return contract
