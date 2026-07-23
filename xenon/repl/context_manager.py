"""
Context Manager — 对话历史与 Token 管理。

职责：
1. 维护多轮对话的 message history。
2. 估算 token 用量（基于词数的粗略估算）。
3. 支持 /compact 压缩：将旧对话摘要化，释放 context window。
4. 支持 /undo 回退：撤销最近一轮对话。
"""

from __future__ import annotations

import copy
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SENSITIVE_MEMORY_KEY = re.compile(
    r"(?:api[_-]?key|token|secret|password|authorization|cookie)",
    re.IGNORECASE,
)

# P3-Q7 / §8.26.2：CJK 范围扩展——基本区 + 扩展 A + 兼容区 + 假名 + 韩文音节。
# 原仅 '一' <= c <= '鿿'（U+4E00–U+9FFF）遗漏扩展 A/日文假名/韩文，致估算偏低。
_CJK_RE = re.compile(
    r'[㐀-䶿'   # CJK 扩展 A
    r'一-鿿'    # CJK 基本区
    r'豈-﫿'    # CJK 兼容表意
    r'぀-ヿ'    # 平假名 + 片假名
    r'가-힯]'   # 韩文音节
)


def _estimate_tokens(text: str) -> int:
    """估算 token 数（模块级，供 ``ConversationTurn`` 缓存与 ``ContextManager`` 共用）。

    规则（注释与代码统一，§8.26.2 修正原注释 len/3 与代码 len//2 不一致）：
    - CJK 字符（含扩展 A/兼容/假名/韩文）约 2 token/字；
    - 英文约 1.3 token/word；
    - 代码/JSON 密集按 0.4×chars；
    - 始终不低于 ``len(text)//2``（防止无空格长串被低估）。
    """
    if not text:
        return 0

    cjk_count = len(_CJK_RE.findall(text))
    words = len(text.split())
    chars = len(text)

    # 检测是否包含大量代码/JSON
    code_chars = text.count('{') + text.count('}') + text.count(';') + text.count('=')
    is_code_heavy = code_chars > chars * 0.02

    # 基础估算：至少 len//2（防止无空格长串被低估）
    char_based = max(chars // 2, 1)

    if is_code_heavy:
        return max(words * 2, int(chars * 0.4))
    elif cjk_count > chars * 0.3:
        return max(words, int(cjk_count * 2), char_based)
    else:
        return max(words, int(words * 1.3), char_based)


def _infer_turn_type(role: str, content: str) -> str:
    """根据 role 和 content 推断 turn 类型（v0.5.0 分层上下文标注）。"""
    if role == "user":
        return "user_input"
    if role == "assistant":
        # 检测是否包含工具调用标记
        if "<function_call>" in content or "tool_calls" in content:
            return "tool_call"
        return "assistant_output"
    if role == "tool":
        return "tool_result"
    if role == "system":
        return "system"
    return "general"


def _guess_tool_name(turn: Any) -> str:
    """从 ConversationTurn 中猜测工具名称（v0.5.0）。"""
    # 从 metadata 中查找
    meta = getattr(turn, "metadata", {}) or {}
    if "tool_name" in meta:
        return str(meta["tool_name"])
    # 从 content 前缀猜测（Observation: tool_name ...）
    content = getattr(turn, "content", "")
    if content.startswith("Observation:"):
        # 尝试提取工具名
        import re as _re
        m = _re.match(r"Observation:\s*(\w+)", content)
        if m:
            return m.group(1)
    return "unknown"


@dataclass
class ConversationTurn:
    """一轮对话记录。"""

    role: str  # "user" | "assistant" | "system" | "tool"
    content: str
    model_used: str | None = None
    node_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # v0.5.0：分层上下文标注
    task_tier: int = 3  # Q1-Q5，来自 AutoRouter 路由决策
    turn_type: str = "general"  # user_input | assistant_output | tool_call | tool_result
    semantic_group_id: str | None = None  # 语义块分组 ID
    turn_index: int = 0  # 对话中的全局位置序号
    # P3-Q7 / §8.9.2：token 估算懒缓存——content 添加后不变，避免状态栏/每轮全量重算。
    _token_count: int | None = field(default=None, init=False, repr=False, compare=False)

    @property
    def token_count(self) -> int:
        """该 turn 的 token 估算（首次访问计算并缓存）。"""
        if self._token_count is None:
            self._token_count = _estimate_tokens(self.content)
        return self._token_count


class ContextManager:
    """
    对话上下文管理器。

    管理 message history、token 估算、压缩和回退。
    """

    def __init__(
        self,
        *,
        max_tokens: int = 128000,
        compact_threshold: float = 0.6,
        compact_force: float = 0.85,
        track_real_usage: bool = False,
    ) -> None:
        self.max_tokens = max_tokens
        # F3 双阈值：compact_threshold=warn（60%，触发 LLM 压缩），
        # compact_force=force（85%，跳过 LLM 改用安全截断，避免超限输入）
        self.compact_threshold = compact_threshold
        self.compact_force = compact_force
        self.history: list[ConversationTurn] = []
        self._undo_stack: list[list[ConversationTurn]] = []
        # P3-Q10 / §8.26.9：undo 栈上限，防多次 /compact 全量 deepcopy 线性堆积累积内存。
        self.max_undo_snapshots: int = 5
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0
        # F3 压缩持久化（可选）：设置后每次压缩写一份 markdown 快照
        self.session_id: str | None = None
        self.persist_dir: Path | None = None
        # P3-Q1 续 / §8.8.1：真实 usage 优先于启发式估算。
        # track_real_usage=True 时订阅 llm_client 的 usage 回调，记录最近一次
        # chat_completion 调用的真实 token 用量；current_token_usage() 优先返回
        # 真实 total_tokens（= 该次 prompt+completion ≈ 当前历史占用），无真实
        # 数据时回退 _estimate_tokens 启发式（首调前 / 离线 / mock 场景）。
        self._real_usage: dict[str, int] | None = None
        self._suppress_usage: bool = False  # compact 自身摘要调用期间抑制记录
        self._usage_unsub: Any = None
        # v0.5.0：分层上下文管理
        self._active_tier: int = 3  # 当前活跃任务层级 (Q1-Q5)
        self._turn_counter: int = 0  # 全局轮次计数器
        self._semantic_group_counter: int = 0  # 语义块 ID 计数器
        self._working_memory: dict[str, Any] = {}  # 跨压缩持久化的工作记忆
        # 可替换的系统上下文层（项目规则、长期记忆检索、单轮指令等）。
        # 它们不属于聊天历史，避免每轮 append 后重复膨胀，也不会被 compact
        # 当成用户对话压缩掉。
        # Replaceable prompt overlays are split by cache stability.  Stable
        # layers (for example the project snapshot) belong immediately after
        # the fixed system prompt.  Volatile layers (retrieved memories and
        # other per-turn state) are kept near the current user request so a
        # change cannot invalidate the reusable conversation prefix.
        self._context_messages: dict[str, tuple[str, bool]] = {}
        # Structural rewrites start a new cache family. Appending ordinary
        # conversation turns deliberately keeps the same epoch.
        self.cache_epoch: int = 0
        if track_real_usage:
            self._subscribe_usage()

    # ── 对话管理 ──────────────────────────────────────────

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """添加一条消息到历史。

        v0.5.0：自动标注 task_tier、turn_type、turn_index。
        """
        # 自动推断 turn_type
        turn_type = kwargs.pop("turn_type", None)
        if turn_type is None:
            turn_type = _infer_turn_type(role, content)
        # 自动标注 task_tier（可通过 kwargs 覆盖）
        task_tier = kwargs.pop("task_tier", None)
        if task_tier is None:
            task_tier = self._active_tier
        # 自增轮次计数器
        self._turn_counter += 1
        turn = ConversationTurn(
            role=role, content=content,
            task_tier=task_tier,
            turn_type=turn_type,
            turn_index=self._turn_counter,
            **kwargs,
        )
        self.history.append(turn)

    def set_active_tier(self, tier: int) -> None:
        """设置当前活跃任务层级（由 AutoRouter 在路由后调用）。"""
        if 1 <= tier <= 5:
            self._active_tier = tier

    @property
    def active_tier(self) -> int:
        """当前活跃任务层级。"""
        return self._active_tier

    def begin_semantic_group(self) -> str:
        """开始一个语义块，返回 group_id。"""
        self._semantic_group_counter += 1
        return f"sg-{self._semantic_group_counter}"

    def end_semantic_group(self, group_id: str) -> None:
        """标记语义块结束（当前为预留接口，后续压缩时使用）。"""
        # v0.5.0：预留接口，当前仅记录 group_id 供后续 compress 读取
        pass

    def update_working_memory(self, key: str, value: Any) -> None:
        """更新工作记忆中的键值对（跨压缩持久化）。"""
        self._working_memory[key] = value

    def get_working_memory(self) -> dict[str, Any]:
        """获取当前工作记忆的浅拷贝。"""
        return dict(self._working_memory)

    def replace_working_memory(self, memory: dict[str, Any] | None) -> None:
        """Replace persistent working memory when loading a saved session."""
        self._working_memory = dict(memory or {})

    def set_context_message(
        self,
        key: str,
        content: str | None,
        *,
        stable: bool = False,
    ) -> None:
        """Set or clear one replaceable system-context layer.

        ``stable=True`` is reserved for content that will not change during a
        session, such as the detected project snapshot.  Query-dependent
        memory and other turn-local context must use the default volatile
        layer so they are appended after the reusable history prefix.
        """
        if content and content.strip():
            self._context_messages[str(key)] = (content.strip()[:20_000], bool(stable))
        else:
            self._context_messages.pop(str(key), None)

    def get_context_messages(self, *, stable: bool | None = None) -> list[dict[str, str]]:
        """Return replaceable overlays in deterministic insertion order.

        Passing ``stable=True`` or ``False`` selects a cache tier.  ``None``
        preserves the public compatibility behaviour and returns both tiers.
        """
        return [
            {"role": "system", "content": content}
            for content, is_stable in self._context_messages.values()
            if stable is None or is_stable is stable
        ]

    @classmethod
    def _safe_context_value(cls, value: Any, *, key: str = "") -> Any:
        """Bound and redact values before they enter a prompt or session trace."""
        if key and _SENSITIVE_MEMORY_KEY.search(key):
            return "[REDACTED]"
        if isinstance(value, dict):
            return {
                str(k): cls._safe_context_value(v, key=str(k))
                for k, v in list(value.items())[:20]
            }
        if isinstance(value, (list, tuple)):
            return [cls._safe_context_value(v) for v in list(value)[:20]]
        if isinstance(value, str):
            return value[:1000]
        if isinstance(value, (int, float, bool, type(None))):
            return value
        return str(value)[:1000]

    def working_memory_prompt(self) -> str:
        """Render bounded persistent facts for injection into the next LLM call."""
        if not self._working_memory:
            return ""
        safe = self._safe_context_value(self._working_memory)
        payload = json.dumps(safe, ensure_ascii=False, sort_keys=True)
        return (
            "[Xenon 持久工作记忆]\n"
            "以下是本会话中已验证的状态；使用前如有必要可再次检查：\n"
            f"{payload[:4000]}"
        )

    def add_tool_trace(
        self,
        tool_name: str,
        params: dict[str, Any] | None,
        success: bool,
        result: str = "",
        error: str | None = None,
    ) -> None:
        """Persist a compact, redacted tool call/result pair across turns."""
        safe_params = self._safe_context_value(params or {})
        params_text = json.dumps(safe_params, ensure_ascii=False, sort_keys=True)
        group_id = self.begin_semantic_group()
        self.add_message(
            "assistant",
            f"[工具调用: {tool_name}]\n参数: {params_text[:1500]}",
            turn_type="tool_call",
            semantic_group_id=group_id,
            metadata={"tool_name": tool_name, "success": bool(success)},
        )
        status = "成功" if success else "失败"
        detail = result or error or "（无文本结果）"
        self.add_message(
            "tool",
            f"[{status}] {detail[:2000]}",
            turn_type="tool_result",
            semantic_group_id=group_id,
            metadata={"tool_name": tool_name, "success": bool(success)},
        )
        self.end_semantic_group(group_id)

    def add_provider_messages(self, messages: list[dict[str, Any]]) -> int:
        """Keep native provider protocol messages for active-session replay.

        The exact payload is volatile metadata: it is used by ``get_messages``
        during the current process so DeepSeek can continue thinking-mode tool
        calls across user turns. Session export intentionally drops the raw
        payload and keeps only a bounded human-readable summary, avoiding the
        persistence of long reasoning traces or unredacted tool output.
        """
        added = 0
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role", ""))
            if role not in {"assistant", "tool"}:
                continue
            raw_content = message.get("content", "")
            if isinstance(raw_content, str) and raw_content:
                summary = raw_content[:2000]
            elif message.get("tool_calls"):
                names = []
                for call in message.get("tool_calls", []):
                    function = call.get("function", {}) if isinstance(call, dict) else {}
                    name = str(function.get("name", ""))
                    if name:
                        names.append(name)
                summary = f"[原生工具调用: {', '.join(names) or 'unknown'}]"
            else:
                summary = "[原生工具协议消息]"
            self.add_message(
                role,
                summary,
                turn_type="tool_call" if role == "assistant" else "tool_result",
                metadata={"api_message": copy.deepcopy(message)},
            )
            added += 1
        return added

    def add_user_message(self, content: str, **kwargs: Any) -> None:
        """Append a user turn with optional semantic metadata."""
        self.add_message("user", content, **kwargs)

    def add_assistant_message(self, content: str, *, model_used: str | None = None) -> None:
        self.add_message("assistant", content, model_used=model_used)

    def add_system_message(self, content: str) -> None:
        self.add_message("system", content)

    def get_messages(
        self,
        *,
        include_working_memory: bool = False,
        include_context_messages: bool = False,
    ) -> list[dict[str, Any]]:
        """Convert history to API-safe messages.

        Persisted tool turns intentionally do not pretend to be native function
        calls: without a provider-issued ``tool_call_id``, sending role=tool is
        rejected by several OpenAI-compatible APIs.  They are therefore replayed
        as user observations while retaining role=tool in ``history`` for
        compaction, routing and session inspection.
        """
        history_messages: list[dict[str, Any]] = []
        for turn in self.history:
            api_message = (turn.metadata or {}).get("api_message")
            if isinstance(api_message, dict):
                history_messages.append(copy.deepcopy(api_message))
            elif turn.role == "tool":
                tool_name = str((turn.metadata or {}).get("tool_name", "unknown"))
                history_messages.append({
                    "role": "user",
                    "content": f"[工具结果: {tool_name}]\n{turn.content}",
                })
            else:
                history_messages.append({"role": turn.role, "content": turn.content})

        # Keep the fixed leading system turn first, then cache-stable project
        # context.  Existing conversation history remains ahead of volatile
        # state.  The current user turn stays last so retrieved memory and
        # working state apply to that request without breaking the earlier
        # reusable prefix.
        messages: list[dict[str, Any]] = []
        while history_messages and history_messages[0].get("role") == "system":
            messages.append(history_messages.pop(0))
        if include_context_messages:
            messages.extend(self.get_context_messages(stable=True))

        current_user: dict[str, Any] | None = None
        if history_messages and history_messages[-1].get("role") == "user":
            current_user = history_messages.pop()
        messages.extend(history_messages)

        if include_working_memory:
            memory = self.working_memory_prompt()
            if memory:
                messages.append({"role": "system", "content": memory})
        if include_context_messages:
            messages.extend(self.get_context_messages(stable=False))
        if current_user is not None:
            messages.append(current_user)
        return messages

    def export_history(self) -> list[dict[str, Any]]:
        """Serialize history without losing tool roles and semantic metadata."""
        return [
            {
                "role": turn.role,
                "content": turn.content,
                "model_used": turn.model_used,
                "node_id": turn.node_id,
                "metadata": self._safe_context_value({
                    key: value
                    for key, value in (turn.metadata or {}).items()
                    if key != "api_message"
                }),
                "task_tier": turn.task_tier,
                "turn_type": turn.turn_type,
                "semantic_group_id": turn.semantic_group_id,
            }
            for turn in self.history
        ]

    def trim_last_assistant(self) -> str | None:
        """移除并返回最后一条 assistant 消息（用于撤回 LLM 幻觉回复）。"""
        for i in range(len(self.history) - 1, -1, -1):
            if self.history[i].role == "assistant":
                return self.history.pop(i).content
        return None

    def trim_last_user(self) -> str | None:
        """移除并返回最后一条 user 消息（用于引擎异常时清理孤立 user 消息）。

        P2-修复5 (观察项-2)：当 LLM 引擎抛异常时，user 消息已 add 但无对应
        assistant 响应，history 出现 user-only 序列。优先用 add_assistant_message
        占位错误消息，无法占位时回退到此方法清理 user 消息。
        """
        for i in range(len(self.history) - 1, -1, -1):
            if self.history[i].role == "user":
                return self.history.pop(i).content
        return None

    # ── Token 估算 ────────────────────────────────────────

    def _subscribe_usage(self) -> None:
        """订阅 llm_client 的 usage 回调（P3-Q1 续 / §8.8.1）。

        懒导入避免 repl ↔ utils 循环；回调异常已被 llm_client 隔离，这里只做记录。
        """
        try:
            from xenon.utils.llm_client import register_usage_callback

            self._usage_unsub = register_usage_callback(self._on_usage)
        except Exception:  # noqa: BLE001 — 订阅失败不应阻断 ContextManager 构造
            logger.warning("真实 usage 订阅失败，回退启发式估算", exc_info=True)
            self._usage_unsub = None

    def _on_usage(self, model_id: str, usage: Any, latency: float) -> None:
        """usage 回调适配：把 LLMUsage 转成 dict 记录（compact 期间抑制）。"""
        if self._suppress_usage:
            return
        self.record_real_usage(
            getattr(usage, "prompt_tokens", 0),
            getattr(usage, "completion_tokens", 0),
            getattr(usage, "total_tokens", 0),
        )

    def record_real_usage(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int | None = None,
    ) -> None:
        """记录最近一次 chat_completion 的真实 token 用量（P3-Q1 续 / §8.8.1）。

        total_tokens 缺省时取 prompt+completion。该值反映模型实际看到的输入+
        输出大小 ≈ 当前历史占用（直连模式下与 history 一一对应；引擎模式下含
        ReAct/Plan 草稿，略偏高，偏向更早触发 compact，安全侧）。
        """
        total = int(total_tokens) if total_tokens else int(prompt_tokens) + int(completion_tokens)
        self._real_usage = {
            "prompt": int(prompt_tokens),
            "completion": int(completion_tokens),
            "total": total,
        }

    def estimate_tokens(self, text: str) -> int:
        """估算 token 数（委托模块函数，保留向后兼容）。

        规则见模块级 ``_estimate_tokens``；CJK 范围已扩展（§8.26.2）。
        """
        return _estimate_tokens(text)

    def current_token_usage(self) -> int:
        """当前历史的 token 占用（P3-Q1 续 / §8.9.2）。

        优先返回真实 usage 的 total_tokens（最近一次 chat_completion 的
        prompt+completion ≈ 当前历史占用）；无真实数据时回退各 turn 缓存的
        ``token_count`` 求和（O(n) 免重算，§8.9.2 memoization）。
        """
        if self._real_usage is not None:
            return self._real_usage["total"]
        return sum(turn.token_count for turn in self.history)

    def real_usage(self) -> dict[str, int] | None:
        """最近一次真实 usage（prompt/completion/total），无则 None。"""
        return None if self._real_usage is None else dict(self._real_usage)

    def usage_ratio(self) -> float:
        """当前 token 使用率 (0.0 ~ 1.0+)。"""
        return self.current_token_usage() / self.max_tokens if self.max_tokens > 0 else 0.0

    def needs_compact(self) -> bool:
        """是否需要压缩。"""
        return self.usage_ratio() >= self.compact_threshold

    # ── /undo 回退 ────────────────────────────────────────

    def save_snapshot(self) -> None:
        """保存当前历史快照（用于 undo）。

        P3-Q10 / §8.26.9：栈有 ``max_undo_snapshots`` 上限，超出时丢弃最旧快照，
        避免多次 /compact 全量 deepcopy 线性堆积累积内存。
        """
        self._undo_stack.append(copy.deepcopy(self.history))
        overflow = len(self._undo_stack) - self.max_undo_snapshots
        if overflow > 0:
            del self._undo_stack[:overflow]

    def undo(self) -> bool:
        """
        回退到上一个快照。

        Returns:
            True 如果成功回退，False 如果没有可回退的快照。
        """
        if not self._undo_stack:
            return False
        self.history = self._undo_stack.pop()
        self.cache_epoch += 1
        # P3-Q1 续：回退到旧快照后，记录的真实 usage 已不对应当前 history → 失效
        self._real_usage = None
        return True

    @property
    def undo_depth(self) -> int:
        return len(self._undo_stack)

    # ── /compact 压缩 ────────────────────────────────────

    @staticmethod
    def _reorder_for_summary(model_priority: list[str] | None) -> list[str]:
        """v0.5.0：摘要调用优先选快模型（flash/mini/haiku），降延迟。

        摘要不需要旗舰模型的能力，用轻量模型可以节省 70-80% 延迟。
        """
        if not model_priority:
            return []
        fast_keywords = ("flash", "mini", "haiku", "lite", "turbo", "v4-flash")
        fast = [m for m in model_priority if any(k in m.lower() for k in fast_keywords)]
        slow = [m for m in model_priority if m not in fast]
        return fast + slow  # 快模型优先，慢模型兜底

    # F3：6 段结构化摘要的段名（顺序即输出顺序）
    _SIX_SEGMENTS = (
        "原始目标", "已完成步骤", "关键约束",
        "当前文件状态", "剩余待办", "关键数据",
    )

    def _preprocess_older(
        self,
        older: list[ConversationTurn],
        strategy: Any = None,
    ) -> list[ConversationTurn]:
        """v0.5.0 P0-1：语义分块预处理 older 消息。

        1. 用 SemanticChunker 将 older 分组成语义块
        2. 对工具链块中的工具结果做分类压缩（减少 LLM 摘要的输入噪音）
        3. 对非工具块不做处理
        4. 返回预处理后的 turns 列表（保持原始顺序）
        """
        if not older or len(older) <= 2:
            return list(older)  # 太少，不分块

        try:
            from xenon.repl.semantic_chunker import SemanticChunker
            from xenon.repl.context_strategies import ToolOutputClassifier

            chunker = SemanticChunker()
            chunks = chunker.group(older)

            # 如果只有一个块，不需要预处理
            if len(chunks) <= 1:
                return list(older)

            classifier = ToolOutputClassifier()
            max_chars = getattr(strategy, "tool_output_max_chars", 500) if strategy else 500

            processed: list[ConversationTurn] = []
            for chunk in chunks:
                if chunk.has_tool_calls and chunk.is_atomic:
                    # 工具链块：压缩其中的工具结果
                    for turn in chunk.turns:
                        tt = getattr(turn, "turn_type", "general")
                        if tt == "tool_result":
                            tool_name = _guess_tool_name(turn)
                            compressed = classifier.compress(tool_name, turn.content, max_chars=max_chars)
                            import copy
                            new_turn = copy.copy(turn)
                            new_turn.content = compressed
                            processed.append(new_turn)
                        else:
                            processed.append(turn)
                else:
                    # 非工具块：原样保留
                    processed.extend(chunk.turns)

            return processed
        except Exception:
            # 预处理失败不应中断压缩流程
            return list(older)

    def compact(
        self,
        summary: str | None = None,
        model_priority: list[str] | None = None,
        *,
        session_id: str | None = None,
        budget_phase: str | None = None,
    ) -> str:
        """压缩对话历史（v0.5.0 分层策略 + F3 6 段结构化摘要）。

        分流逻辑（tier-aware）：
        1. 从 TieredStrategySelector 获取当前 tier 的压缩策略
        2. 语义分块预处理 older（P0-1：工具输出预压缩）
        3. 根据 SpaceBudget 判定空间状态（充裕/紧张/危急）
        4. 充裕 → LLM 压缩（3 或 6 段，取决于 tier）
        5. 紧张 → 最小模型 + 极简 prompt 或正则兜底
        6. 危急 → 按 tier 分层截断（drop/label/auto_summary/structured_truncate/cross_tier_evict）

        B5 兼容：older 为空时直接返回（不反向增加消息）。
        """
        from xenon.repl.context_strategies import (
            SpaceBudget,
            TieredStrategySelector,
            handle_crisis,
        )

        # 获取当前 tier 的压缩策略
        selector = TieredStrategySelector()
        strategy = selector.select(self._active_tier, phase=budget_phase)
        trigger = strategy.trigger_threshold

        ratio = self.usage_ratio()
        older, recent = self._split_recent(keep_rounds=strategy.keep_recent_rounds)

        # B5：无可压缩的早期消息 → 不改写历史
        if not older:
            return summary or "（无可压缩的早期对话，无需压缩）"

        # v0.5.0 P0-1：语义分块预处理 — 压缩 older 中的工具输出，减少 LLM 输入噪音
        older = self._preprocess_older(older, strategy)

        # 使用策略阈值判断是否需要压缩
        if ratio < trigger and not summary:
            return (
                f"（当前上下文使用率 {ratio:.0%}，"
                f"低于 Q{self._active_tier} 阈值 {trigger:.0%}，无需压缩）"
            )

        self.save_snapshot()

        # 评估空间状态
        space_state = SpaceBudget.evaluate(ratio)

        # P3-Q1 续：压缩自身的 LLM 摘要调用会经 usage 回调，但其 prompt 是被
        # 压缩的 older 片段、非当前 history → 抑制记录，避免污染 current_token_usage
        prev_suppress = self._suppress_usage
        self._suppress_usage = True
        try:
            if space_state == "critical" and not summary:
                # 危急：无法调用 LLM → 分层截断
                current_idx = self._turn_counter
                new_history, crisis_summary = handle_crisis(
                    older, recent, strategy, current_idx,
                )
                summary = crisis_summary or (
                    f"（上下文使用率 {ratio:.0%}，空间危急，"
                    f"Q{self._active_tier} 分层截断完成）"
                )
            elif space_state == "tight" and not summary:
                # 紧张：用最小模型 + 极简 prompt，或正则兜底
                if self._active_tier <= 2:
                    # Q1-Q2: 正则兜底
                    summary = self._auto_summary(messages=list(older))
                elif self._active_tier == 3:
                    # Q3: 尝试最小模型
                    summary = self._llm_summary_nseg(
                        model_priority, older,
                        n_segments=3,
                        max_prompt_chars=1500,
                    )
                else:
                    # Q4-Q5: 先压缩工具输出腾空间，再调 LLM
                    summary = self._llm_summary_after_tool_trim(
                        model_priority, older,
                        n_segments=strategy.summary_segments,
                        strategy=strategy,
                    )
                summary_turn = ConversationTurn(
                    role="system",
                    content=(
                        f"[对话历史已压缩（空间紧张）] "
                        f"以下是之前 {len(older)} 条消息的摘要：\n\n{summary}"
                    ),
                    task_tier=self._active_tier,
                    turn_type="system",
                    turn_index=self._turn_counter + 1,
                )
                new_history = [summary_turn] + recent
                self._turn_counter += 1
            else:
                # 充裕：正常 LLM 压缩
                if self._active_tier <= 2 and not summary:
                    # Q1-Q2: 3 段简化摘要
                    summary = summary or self._llm_summary_nseg(
                        model_priority, older, n_segments=3,
                    )
                else:
                    # Q3-Q5: 6 段结构化摘要（或手动摘要）
                    summary = summary or self._llm_summary_6seg(
                        model_priority, older,
                    )
                summary_turn = ConversationTurn(
                    role="system",
                    content=(
                        f"[对话历史已压缩] "
                        f"以下是之前 {len(older)} 条消息的 {strategy.summary_segments} 段摘要：\n\n{summary}"
                    ),
                    task_tier=self._active_tier,
                    turn_type="system",
                    turn_index=self._turn_counter + 1,
                )
                new_history = [summary_turn] + recent
                self._turn_counter += 1
        finally:
            self._suppress_usage = prev_suppress

        self.history = new_history
        self.cache_epoch += 1
        # 压缩后 history 结构性变更，旧真实 usage 不再对应 → 失效，下次调用重新填充
        self._real_usage = None
        self._persist_compact_md(summary, session_id)
        return summary

    def _split_recent(self, keep_rounds: int = 3) -> tuple[list[ConversationTurn], list[ConversationTurn]]:
        """按 user 轮数切分 older / recent（recent 保留最近 keep_rounds 轮完整对话）。"""
        user_count = 0
        cut_idx = 0
        for i in range(len(self.history) - 1, -1, -1):
            if self.history[i].role == "user":
                user_count += 1
                if user_count >= keep_rounds:
                    cut_idx = i
                    break
        return self.history[:cut_idx], self.history[cut_idx:]

    def _safe_truncation(self) -> list[ConversationTurn]:
        """F3 Tier 3 安全截断：保留所有 system 消息 + 最近 30% 非系统消息（min5 max20）。"""
        system_msgs = [t for t in self.history if t.role == "system"]
        non_system = [t for t in self.history if t.role != "system"]
        if not non_system:
            return list(self.history)
        keep = max(5, min(20, int(len(non_system) * 0.3)))
        keep = min(keep, len(non_system))
        # 仅保留第一条 system（主提示词），丢弃旧的压缩摘要 system，避免累积
        head_system = system_msgs[:1]
        return head_system + non_system[-keep:]

    def _head_tail_truncate(self, text: str, head_ratio: float = 0.4, tail_ratio: float = 0.5) -> str:
        """F3 输入头尾截断：取前 40% + 后 50%，丢弃中间 10%，控制 LLM 摘要输入体量。

        仅对长文本（≥200 字符）生效——短文本截断反而损失信息。
        """
        if not text:
            return text
        n = len(text)
        if n < 200:
            return text
        head_end = int(n * head_ratio)
        tail_start = n - int(n * tail_ratio)
        if head_end >= tail_start:
            return text  # 文本不长，无需截断
        return text[:head_end] + f"\n…（省略中间 {tail_start - head_end} 字符）…\n" + text[tail_start:]

    def _llm_summary_6seg(
        self,
        model_priority: list[str] | None,
        messages: list[ConversationTurn],
    ) -> str:
        """F3 6 段结构化 LLM 摘要（含头尾截断 + 备选模型重试 + 解析校验）。

        失败链路：LLM 6 段 → 解析失败 → _auto_summary 正则兜底。
        """
        try:
            from xenon.utils.llm_client import chat_completion

            # 构建对话文本：tier-aware 截断（高阶任务保留更多细节）
            parts = []
            for t in messages[-15:]:
                tag = {"user": "用户", "assistant": "助手", "system": "系统", "tool": "工具"}.get(t.role, t.role)
                turn_tier = getattr(t, "task_tier", 3)
                if turn_tier >= 4:
                    limit = 500  # Q4-Q5：保留更多细节，含代码参数、架构决策
                elif turn_tier >= 2:
                    limit = 300  # Q2-Q3：标准截断
                else:
                    limit = 150  # Q1：激进截断
                parts.append(f"[{tag}] {t.content[:limit]}")
            conversation = self._head_tail_truncate("\n".join(parts))

            system_prompt = (
                "请将以下对话压缩为严格的 6 段结构化摘要，每段以【段名】开头，"
                "该段无内容时写\"无\"。总长不超过 600 字。段名与顺序固定：\n"
                "【原始目标】用户的核心需求与最终目标。\n"
                "【已完成步骤】已执行的操作及其结果（含工具调用）。\n"
                "【关键约束】用户强调的约束、偏好、技术栈、命名规范。\n"
                "【当前文件状态】已创建/修改/读取的文件路径及其状态。\n"
                "【剩余待办】尚未完成的任务与下一步。\n"
                "【关键数据】必须记住的代码片段、配置值、ID、错误信息。"
            )
            msgs = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": conversation},
            ]

            # 备选模型重试：依次尝试 model_priority，首个成功且解析通过即返回
            for model_id in self._reorder_for_summary(model_priority):
                try:
                    raw = chat_completion(model_id, msgs, max_tokens=1000, temperature=0.1)
                    parsed = self._parse_six_segments(raw)
                    if parsed:
                        return parsed
                    logger.debug(f"6 段摘要解析失败，尝试下一个模型: {model_id}")
                except Exception as e:  # noqa: BLE001 — 备选模型逐一兜底
                    logger.warning(f"6 段摘要模型 {model_id} 失败: {e}，尝试下一个")
                    continue

            # 所有模型都失败或解析不通过 → 正则兜底
            return self._auto_summary(messages=messages)
        except Exception as e:  # noqa: BLE001 — 压缩绝不能上抛
            logger.warning(f"6 段摘要整体失败，回退自动摘要: {e}")
            return self._auto_summary(messages=messages)

    def _parse_six_segments(self, raw: str) -> str | None:
        """解析 LLM 输出为 6 段；至少含【原始目标】或【已完成步骤】才算有效。"""
        if not raw or not raw.strip():
            return None
        # 按【段名】切分
        pattern = re.compile(r"【([^】]+)】")
        matches = list(pattern.finditer(raw))
        if not matches:
            return None
        segments: dict[str, str] = {}
        for i, m in enumerate(matches):
            name = m.group(1).strip()
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
            segments[name] = raw[start:end].strip()

        # 校验：至少包含核心段之一
        if not any(name in segments for name in ("原始目标", "已完成步骤")):
            return None

        # 按规范顺序重组
        lines = []
        for seg in self._SIX_SEGMENTS:
            content = segments.get(seg, "无")
            lines.append(f"【{seg}】{content}")
        return "\n".join(lines)

    def _persist_compact_md(self, summary: str, session_id: str | None) -> None:
        """F3 持久化：压缩成功后写一份带时间戳的 markdown 快照。"""
        try:
            sid = session_id or self.session_id or "default"
            base = self.persist_dir or (Path.home() / ".xenon" / "sessions" / sid)
            base.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            path = base / f"compact-{ts}.md"
            path.write_text(
                f"# 对话压缩快照 ({ts})\n\n"
                f"- session: {sid}\n"
                f"- 使用率: {self.usage_ratio():.1%}\n\n"
                f"## 摘要\n\n{summary}\n",
                encoding="utf-8",
            )
            logger.debug(f"压缩快照已持久化: {path}")
        except Exception as e:  # noqa: BLE001 — 持久化失败不影响压缩主流程
            logger.warning(f"压缩快照持久化失败（已忽略）: {e}")

    def _llm_summary_nseg(
        self,
        model_priority: list[str] | None,
        messages: list[ConversationTurn],
        *,
        n_segments: int = 3,
        max_prompt_chars: int | None = None,
    ) -> str:
        """v0.5.0：通用 n 段 LLM 摘要（3 段用于 Q1-Q2，6 段用于 Q3+）。

        与 _llm_summary_6seg 的区别：支持可变段数 + prompt 长度限制。
        """
        try:
            from xenon.utils.llm_client import chat_completion

            # 构建对话文本
            parts = []
            for t in messages[-15:]:
                tag = {"user": "用户", "assistant": "助手", "system": "系统", "tool": "工具"}.get(t.role, t.role)
                turn_tier = getattr(t, "task_tier", 3)
                limit = 500 if turn_tier >= 4 else (300 if turn_tier >= 2 else 150)
                parts.append(f"[{tag}] {t.content[:limit]}")
            conversation = "\n".join(parts)
            if max_prompt_chars and len(conversation) > max_prompt_chars:
                conversation = self._head_tail_truncate(conversation)

            if n_segments <= 3:
                system_prompt = (
                    "请将以下对话压缩为简洁的 3 段摘要，每段以【段名】开头，"
                    "该段无内容时写\"无\"。总长不超过 300 字。段名与顺序固定：\n"
                    "【原始目标】用户的需求。\n"
                    "【已完成步骤】已执行的操作。\n"
                    "【当前状态】涉及的文件和关键结论。"
                )
                segments_to_parse = ("原始目标", "已完成步骤", "当前状态")
            else:
                system_prompt = (
                    "请将以下对话压缩为严格的 6 段结构化摘要，每段以【段名】开头，"
                    "该段无内容时写\"无\"。总长不超过 600 字。段名与顺序固定：\n"
                    "【原始目标】用户的核心需求与最终目标。\n"
                    "【已完成步骤】已执行的操作及其结果（含工具调用）。\n"
                    "【关键约束】用户强调的约束、偏好、技术栈、命名规范。\n"
                    "【当前文件状态】已创建/修改/读取的文件路径及其状态。\n"
                    "【剩余待办】尚未完成的任务与下一步。\n"
                    "【关键数据】必须记住的**具体数值**——代码参数值、配置常量、"
                    "超时时间、并发数、阈值、端口号、版本号、ID 等。请逐一列出。"
                )
                segments_to_parse = self._SIX_SEGMENTS

            msgs = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": conversation},
            ]

            for model_id in self._reorder_for_summary(model_priority):
                try:
                    # 3 段约需 300 tokens，6 段约需 1000 tokens（中文字 ×2）
                    tok_budget = 300 if n_segments <= 3 else 1000
                    raw = chat_completion(model_id, msgs, max_tokens=tok_budget, temperature=0.1)
                    parsed = self._parse_n_segments(raw, segments_to_parse)
                    if parsed:
                        return parsed
                except Exception as e:
                    logger.warning(f"{n_segments} 段摘要模型 {model_id} 失败: {e}，尝试下一个")
                    continue

            return self._auto_summary(messages=messages)
        except Exception as e:
            logger.warning(f"{n_segments} 段摘要整体失败，回退自动摘要: {e}")
            return self._auto_summary(messages=messages)

    def _parse_n_segments(self, raw: str, segments: tuple[str, ...]) -> str | None:
        """解析 LLM 输出为 n 段；至少含第一段才算有效。"""
        if not raw or not raw.strip():
            return None
        pattern = re.compile(r"【([^】]+)】")
        matches = list(pattern.finditer(raw))
        if not matches:
            return None
        found: dict[str, str] = {}
        for i, m in enumerate(matches):
            name = m.group(1).strip()
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
            found[name] = raw[start:end].strip()

        if segments[0] not in found:
            return None

        lines = []
        for seg in segments:
            content = found.get(seg, "无")
            lines.append(f"【{seg}】{content}")
        return "\n".join(lines)

    def _llm_summary_after_tool_trim(
        self,
        model_priority: list[str] | None,
        messages: list[ConversationTurn],
        *,
        n_segments: int = 6,
        strategy: Any = None,
    ) -> str:
        """v0.5.0：先压缩工具输出腾空间，再调 LLM 做摘要。

        用于空间紧张时的 Q4-Q5 任务。如果 older 消息中没有工具结果，
        则直接走正常 6 段 LLM 摘要路径（避免不必要的 prompt 截断）。
        """
        try:
            from xenon.repl.context_strategies import ToolOutputClassifier

            # 检查是否有工具结果需要压缩
            has_tool_results = any(
                getattr(t, "turn_type", "general") == "tool_result"
                for t in messages
            )

            if not has_tool_results:
                # 纯用户/助手对话 → 走正常 6 段路径，不做 prompt 截断
                return self._llm_summary_6seg(model_priority, messages)

            # 有工具结果 → 压缩后做截断安全的 LLM 摘要
            classifier = ToolOutputClassifier()
            trimmed = []
            for turn in messages:
                if getattr(turn, "turn_type", "general") == "tool_result":
                    tool_name = _guess_tool_name(turn)
                    compressed_content = classifier.compress(
                        tool_name, turn.content,
                        max_chars=300,
                        phase="converge",
                    )
                    import copy
                    new_turn = copy.copy(turn)
                    new_turn.content = compressed_content
                    trimmed.append(new_turn)
                else:
                    trimmed.append(turn)

            return self._llm_summary_nseg(
                model_priority, trimmed,
                n_segments=n_segments,
                max_prompt_chars=3000,  # 放宽到 3000（原 2000 过激）
            )
        except Exception as e:
            logger.warning(f"工具输出预压缩失败: {e}，回退自动摘要")
            return self._auto_summary(messages=messages)

    def _auto_summary(self, messages: list | None = None) -> str:
        """智能自动摘要（保留关键信息）——6 段 LLM 失败时的正则兜底。"""
        target = messages or self.history

        # 提取关键信息
        file_paths = set()
        errors = []
        operations = []
        user_requests = []

        import re
        path_pattern = re.compile(r'[\w/\\.-]+\.(?:py|js|ts|html|css|json|yaml|yml|toml|md|txt|sh|go|rs)')

        for t in target:
            content = t.content
            # 提取文件路径
            file_paths.update(path_pattern.findall(content))
            # 提取错误
            if "error" in content.lower() or "错误" in content or "失败" in content:
                errors.append(content[:150])
            # 提取用户请求
            if t.role == "user":
                user_requests.append(content[:200])
            # 提取操作
            if any(kw in content.lower() for kw in ["创建", "写入", "修改", "删除", "created", "written", "modified"]):
                operations.append(content[:150])

        parts = []

        if user_requests:
            parts.append("用户需求:")
            for req in user_requests[-3:]:
                parts.append(f"  - {req}")

        if file_paths:
            paths = list(file_paths)[:10]
            parts.append(f"涉及文件: {', '.join(paths)}")

        if operations:
            parts.append("执行的操作:")
            for op in operations[-3:]:
                parts.append(f"  - {op}")

        if errors:
            parts.append("遇到的问题:")
            for err in errors[-2:]:
                parts.append(f"  - {err}")

        return "\n".join(parts) if parts else "（无对话内容）"

    # ── 统计 ──────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        """返回当前上下文统计信息。"""
        real = self._real_usage
        return {
            "total_messages": len(self.history),
            "user_messages": sum(1 for t in self.history if t.role == "user"),
            "assistant_messages": sum(1 for t in self.history if t.role == "assistant"),
            "system_messages": sum(1 for t in self.history if t.role == "system"),
            "estimated_tokens": self.current_token_usage(),
            "token_source": "real" if real is not None else "heuristic",
            "real_usage": dict(real) if real is not None else None,
            "max_tokens": self.max_tokens,
            "usage_ratio": f"{self.usage_ratio():.1%}",
            "undo_available": self.undo_depth,
            "needs_compact": self.needs_compact(),
        }

    def clear(self) -> None:
        """清空所有历史。"""
        self.save_snapshot()
        self.history.clear()
        self._working_memory.clear()
        self.cache_epoch += 1
        # P3-Q1 续：清空后真实 usage 不再对应 → 失效
        self._real_usage = None

    def close(self) -> None:
        """退订 usage 回调（P3-Q1 续）：长生命周期对象销毁前调用，避免回调泄漏。"""
        if self._usage_unsub is not None:
            try:
                self._usage_unsub()
            except Exception:  # noqa: BLE001
                logger.debug("usage 回调退订异常（已忽略）", exc_info=True)
            self._usage_unsub = None
