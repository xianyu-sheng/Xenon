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
import re
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

# ── 跨回合承诺：用户回复“继续”时应续接上一轮的行动，而不是重新闲聊 ──

_CONTINUATION_PHRASES = frozenset(
    {
        "继续",
        "继续吧",
        "继续下一步",
        "继续执行",
        "接着",
        "接着来",
        "好",
        "好的",
        "好呀",
        "好的继续",
        "可以",
        "可以了",
        "行",
        "行吧",
        "开始",
        "开始吧",
        "干吧",
        "来吧",
        "动手吧",
        "下一步",
        "确认",
        "同意",
        "批准",
        "授权",
        "嗯",
        "嗯嗯",
        "就这样",
        "continue",
        "goon",
        "goahead",
        "keepgoing",
        "proceed",
        "yes",
        "y",
        "ok",
        "okay",
        "a",
    }
)
_UTTERANCE_NOISE = re.compile(
    r"[\s，。！？、；：,.!?;:'\"“”‘’「」『』（）()\[\]【】…—\-]+"
)
_PROMISE_NEGATION = re.compile(
    r"(?:无需|不用|不必|不需要)(?:我)?继续|no\s+need\s+to\s+continue",
    re.IGNORECASE,
)
_PROMISE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:回复|输入|回我|键入|发送|回答)\s*[「“\"'（(]?\s*(?:继续|yes|ok|好|可以|y)",
        re.IGNORECASE,
    ),
    re.compile(r"我(?:就|将|会|去)?(?:继续|接着)", re.IGNORECASE),
    re.compile(
        r"(?:授权|确认|同意|批准)(?:后|之后|以后|了)[^\n]{0,40}"
        r"(?:我|就|会|将|继续)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:等|等待|期待)(?:你|您)[^\n]{0,20}(?:回复|确认|授权|同意|选择|命令)",
        re.IGNORECASE,
    ),
    re.compile(
        r"下一步[：:，,]?[^\n]{0,40}(?:回复|输入|确认|授权|执行)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:reply|say|type|send)\s+[\"'“]?(?:continue|yes|ok|go\s*ahead)",
        re.IGNORECASE,
    ),
    re.compile(r"I(?:'ll| will| am going to)\s+(?:continue|proceed)", re.IGNORECASE),
    re.compile(
        r"(?:once|after)\s+you\s+(?:confirm|approve|reply|say)", re.IGNORECASE
    ),
    re.compile(
        r"waiting\s+for\s+your\s+(?:confirmation|approval|go-?ahead)",
        re.IGNORECASE,
    ),
)
_PENDING_SCAN_WINDOW = 600


@dataclass(frozen=True)
class PendingAction:
    """A promise the assistant made that only the user's confirmation blocks."""

    engine: str
    level: ExecutionLevel
    intent: str | None
    operations: frozenset[str]
    reason: str
    promise: str
    kind: str = "continuation"  # continuation | approval
    gate_id: str = ""


def _normalize_utterance(text: str) -> str:
    return _UTTERANCE_NOISE.sub("", text or "").lower()


def is_continuation_utterance(text: str) -> bool:
    """只认短确认语，防止把“继续优化 X”当成对旧承诺的授权。"""

    return _normalize_utterance(text) in _CONTINUATION_PHRASES


_APPROVAL_VERB = re.compile(
    r"(?:写入|写到|写了|写吧|保存|保存吧|存到|落盘|执行|执行吧|动手|"
    r"授权|批准|同意|确认|可以写|去写|继续写)",
    re.IGNORECASE,
)
_LEADING_APPROVAL = re.compile(
    r"^(?:好|可以|行|嗯|请|授权|批准|同意|写入|写到|写吧|保存|落盘|执行|动手)",
    re.IGNORECASE,
)


def is_approval_utterance(text: str) -> bool:
    """用户对“被搁置的写入提案”的确认语。

    比 continuation 更宽：只要句子短且包含写/执行意图，或本身就是以
    写入动词开头的指令（“好写入到桌面”/“请写入”/“授权写入到 D 盘”）。
    """

    if is_continuation_utterance(text):
        return True
    normalized = _normalize_utterance(text)
    if not normalized:
        return False
    if len(normalized) <= 40 and _APPROVAL_VERB.search(normalized):
        return True
    return bool(_LEADING_APPROVAL.match(normalized))


def should_consume_pending(pending: PendingAction, text: str) -> bool:
    """本轮输入是否在回应这个待批/待续动作。"""

    if pending.kind == "approval":
        return is_approval_utterance(text)
    return is_continuation_utterance(text)


def detect_pending_action(
    assistant_text: str,
    *,
    engine: str,
    level: ExecutionLevel | int,
    intent: str | None,
    operations: frozenset[str] | set[str],
    reason: str = "",
) -> PendingAction | None:
    """从本轮最终回答的结尾识别“回复继续即可”的承诺。"""

    text = str(assistant_text or "")
    if not text:
        return None
    tail = text[-_PENDING_SCAN_WINDOW:]
    if _PROMISE_NEGATION.search(tail):
        return None
    for pattern in _PROMISE_PATTERNS:
        match = pattern.search(tail)
        if match is None:
            continue
        snippet = " ".join(
            tail[max(0, match.start() - 40) : match.end() + 80].split()
        )
        return PendingAction(
            engine=engine,
            level=ExecutionLevel(int(level)),
            intent=intent,
            operations=frozenset(operations),
            reason=reason or "上一轮承诺在用户确认后继续执行",
            promise=snippet,
        )
    return None


def continuation_hint(pending: PendingAction) -> str:
    """本轮提示词：把“继续”明确解释为对上轮承诺的授权。"""

    scope = {
        int(ExecutionLevel.ANSWER_ONLY): "仅对话",
        int(ExecutionLevel.READ_ONLY): "只读工具",
        int(ExecutionLevel.WRITE): "读写文件",
        int(ExecutionLevel.EXECUTE): "读写文件 + 命令执行",
    }.get(int(pending.level), "只读工具")
    if pending.kind == "approval":
        return (
            "## 用户已确认上一轮搁置的动作\n"
            f"搁置原因：{pending.reason}\n"
            "用户本轮明确确认执行。请直接完成写入/执行，不要重新询问授权，"
            "也不要只复述计划。\n"
            f"本轮授权范围：{scope}。"
        )
    return (
        "## 延续上轮任务（用户已确认）\n"
        f"上一轮你结束时提出：{pending.promise}\n"
        "用户本轮回复确认继续，即视为对上述未完成动作的授权。\n"
        f"本轮授权范围：{scope}。请直接继续完成未完成的动作，"
        "不要重新询问授权，也不要只复述计划。"
    )


def _continuation_contract(
    text: str,
    signals: ExecutionSignals,
    pending: PendingAction,
) -> TurnContract | None:
    """把上轮承诺与本轮约束合并；矛盾或退化时返回 None 走常规路径。"""

    if signals.no_tools or signals.chat_only:
        return None
    operations = set(pending.operations)
    if signals.no_write:
        operations -= _WRITE_OPERATIONS
    if signals.no_execute:
        operations.discard("execute")
    if signals.explicit_write and not signals.no_write:
        operations.add("write")
    if signals.explicit_execute and not signals.no_execute:
        operations.add("execute")
    if not operations:
        return None
    if "execute" in operations:
        level = ExecutionLevel.EXECUTE
    elif operations & _WRITE_OPERATIONS:
        level = ExecutionLevel.WRITE
    else:
        level = ExecutionLevel.READ_ONLY
    level = ExecutionLevel(min(int(level), int(pending.level)))
    reason = (
        f"延续上一轮未完成的行动（{pending.reason}）"
        if pending.kind != "approval"
        else f"用户确认上一轮搁置的动作（{pending.reason}）"
    )
    return TurnContract(
        level=level,
        intent=pending.intent,
        operations=frozenset(operations),
        proposed_level=level,
        confidence=1.0,
        degraded=False,
        ask_required=False,
        reason=reason,
        evidence=("continuation",) + signals.write_patterns,
        signals=signals,
        continuation=True,
    )


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
    continuation: bool = False
    deferred_write: bool = False

    @property
    def requires_tools(self) -> bool:
        return self.level >= ExecutionLevel.READ_ONLY

    @property
    def requires_write(self) -> bool:
        return self.level >= ExecutionLevel.WRITE

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
        "advisory": signals.advisory,
        "deferred_write": signals.deferred_write,
        "write_imperative": signals.write_imperative,
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


def _with_deferred_proposal(
    signals: ExecutionSignals, operations: set[str] | frozenset[str]
) -> set[str]:
    """延迟语句（“让你写再写”）本身就是写意图的提案。"""

    ops = set(operations)
    if (
        signals.deferred_write
        and not signals.no_write
        and not (ops & (_WRITE_OPERATIONS | {"execute"}))
    ):
        ops.add("write")
    return ops


def _fallback_contract(
    text: str,
    signals: ExecutionSignals,
    intent: str | None,
    *,
    reason: str,
) -> TurnContract:
    """Regex-only degradation: keeps the pre-classifier behavior verbatim."""

    policy = classify_execution_policy(text, intent=intent)
    operations = _with_deferred_proposal(signals, _operations_for_level(policy.level))
    level = policy.level
    # 条件式写入在降级路径同样生效：登记提案但不授予写权限。
    deferred = bool(
        signals.deferred_write
        and not signals.no_write
        and operations & (_WRITE_OPERATIONS | {"execute"})
    )
    proposed = policy.level
    if deferred:
        proposed = (
            ExecutionLevel.EXECUTE
            if "execute" in operations
            else ExecutionLevel.WRITE
        )
        level = (
            ExecutionLevel.READ_ONLY
            if operations & _READ_OPERATIONS
            else ExecutionLevel.ANSWER_ONLY
        )
    return TurnContract(
        level=level,
        intent=intent,
        operations=frozenset(operations),
        proposed_level=proposed,
        confidence=1.0,
        degraded=True,
        ask_required=False,
        reason=f"{reason}：{policy.reason}",
        evidence=signals.write_patterns + signals.negation_snippets,
        signals=signals,
        deferred_write=deferred,
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


def _contract_from_level(level_value: int, reason: str = "") -> TurnContract:
    """Synthesize a minimal contract from a stored execution level.

    用于兼容只设置了 ``_execution_level`` 的旧调用方：仍以该级别为准，
    不再用 user_input 重新分类。
    """

    level = ExecutionLevel(int(level_value))
    return TurnContract(
        level=level,
        intent=None,
        operations=_operations_for_level(level),
        proposed_level=level,
        confidence=1.0,
        degraded=True,
        ask_required=False,
        reason=reason or "由 _execution_level 合成（无完整契约）",
        evidence=(),
        signals=ExecutionSignals(),
    )


def contract_for_level(level: ExecutionLevel | int) -> TurnContract:
    """Build a minimal contract with an explicit level.

    供库调用方与测试在边界构造契约（例如“本轮已知需要写权限”），
    避免依赖引擎内部的重新分类。
    """

    return _contract_from_level(int(level))


def ensure_turn_contract(
    ctx: Any,
    text: str,
    *,
    classifier: IntentClassifierLike | None = None,
    fallback_intent: str | None = None,
) -> TurnContract:
    """Return the turn's contract, building it once at the given boundary.

    This is the **only** lazy-build path: engines and gates called without a
    contract get one built here (regex-only unless a classifier is supplied),
    stored back into ``ctx`` so sibling layers reuse it, and never re-classify
    independently.  When ``ctx`` already carries an ``_execution_level`` but no
    contract, a minimal contract is synthesized from that level.
    """

    if ctx is not None:
        existing = ctx.get("_turn_contract")
        if existing is not None:
            return existing
        level = ctx.get("_execution_level")
        if level is not None:
            contract = _contract_from_level(
                int(level), str(ctx.get("_execution_reason") or "")
            )
            ctx.update(
                {
                    "_turn_contract": contract,
                    "_execution_reason": contract.reason,
                }
            )
            return contract

    contract = build_turn_contract(
        text,
        classifier=classifier,
        fallback_intent=fallback_intent,
    )
    if ctx is not None:
        # 只存契约供同层只读；不写 ``_execution_level``——引擎/库调用方没有
        # 显式级别时保持旧的“不拦截”语义，产品路径（REPL）会自己设置级别。
        ctx.set("_turn_contract", contract)
    return contract


def build_turn_contract(
    text: str,
    *,
    classifier: IntentClassifierLike | None = None,
    context_messages: list[dict] | None = None,
    fallback_intent: str | None = None,
    pending: PendingAction | None = None,
) -> TurnContract:
    """Build the single per-turn contract from regex signals + LLM intent.

    The regex layer always runs (cheap).  The LLM classifier is skipped
    entirely for hard-constraint turns, saving one call on "不要写文件"
    style requests and guaranteeing constraints can never be model-overridden.
    It is also skipped for a confirmed continuation (``pending`` + a short
    affirmation): the previous turn already decided intent/level, so the
    follow-up must inherit that decision instead of being re-chatted.

    ``fallback_intent`` lets the REPL pass its context-resolved intent (e.g.
    a terse follow-up inheriting ``query``) so degradation stays identical to
    the previous regex-only behavior.
    """

    signals = extract_execution_signals(text)

    if pending is not None and should_consume_pending(pending, text):
        inherited = _continuation_contract(text, signals, pending)
        if inherited is not None:
            logger.debug(
                "turn_contract continuation level=%s intent=%s ops=%s engine=%s",
                inherited.level.name,
                inherited.intent,
                sorted(inherited.operations),
                pending.engine,
            )
            return inherited

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
    structural_write = signals.explicit_write or signals.write_imperative
    if structural_write and not signals.no_write:
        operations.add("write")
    if signals.explicit_execute and not signals.no_execute:
        operations.add("execute")
    if signals.read_evidence:
        operations.add("read")

    # 4) chat-only / 纯生成意图不得发明文件操作。
    if result.chat_only and not (structural_write or signals.explicit_execute):
        operations.clear()
    if intent in _CHAT_ONLY_INTENTS and not (
        structural_write or signals.explicit_execute
    ):
        operations -= _WRITE_OPERATIONS | {"execute"}
    if intent == "write_code" and not structural_write:
        operations -= _WRITE_OPERATIONS | {"execute"}
        if not signals.read_evidence:
            operations -= _READ_OPERATIONS

    # 4b) 征询解释不是施工：问原因/思路/建议时，即使分类器给出 write/execute
    # 也收回（显式写入/执行结构仍然优先）。
    if signals.advisory and not (
        signals.strong_write or signals.explicit_execute
    ):
        operations -= _WRITE_OPERATIONS | {"execute"}

    # 4c) 延迟语句本身就是写意图的提案（“我让你写你再写”没有显式目标）。
    operations = _with_deferred_proposal(signals, operations)

    # 5) 低置信 + 无正则证据 + 想写/执行 → 询问而不是静默授权。
    threshold = float(getattr(classifier, "confidence_threshold", 0.7) or 0.7)
    ask_required = bool(
        result.confidence < threshold
        and operations & (_WRITE_OPERATIONS | {"execute"})
        and not (structural_write or signals.explicit_execute)
    )
    # 裸祈使句（“请写入”但没有目标/上下文）：先问一次，而不是静默授权。
    if (
        signals.write_imperative
        and not signals.strong_write
        and operations & (_WRITE_OPERATIONS | {"execute"})
    ):
        ask_required = True

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

    # 7b) 条件式写入：“先给我代码，我让你写你再写”。本轮只登记提案，
    # 不授予写权限；写操作保留在 operations 里供 REPL 建待批 Gate。
    deferred = bool(
        signals.deferred_write
        and not signals.no_write
        and operations & (_WRITE_OPERATIONS | {"execute"})
    )

    # 需要询问时先只授权到只读，用户确认后 approve() 提升到 proposed。
    level = proposed
    if deferred:
        level = (
            ExecutionLevel.READ_ONLY
            if operations & _READ_OPERATIONS
            else ExecutionLevel.ANSWER_ONLY
        )
        ask_required = False
    elif ask_required and proposed >= ExecutionLevel.WRITE:
        level = ExecutionLevel.READ_ONLY

    reason = (
        f"LLM 意图={intent} 置信度={result.confidence:.2f} "
        f"操作={sorted(operations)}；"
        f"正则写入证据={list(signals.write_patterns) or '无'}；"
        f"禁令={list(signals.negation_snippets) or '无'}"
    )
    if ask_required:
        reason += "；低置信且无显式证据 → 需用户确认"
    if deferred:
        reason += "；检测到延迟写入（等待用户确认）"
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
        deferred_write=deferred,
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
