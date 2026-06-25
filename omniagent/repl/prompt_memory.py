"""自主持久化决策 — Prompt Memory Manager.

在每次 Agent 执行后评估对话内容，检测值得长期存储的模式和偏好，
自动写入 PromptStore 的 memories/ 目录。

双重机制：
1. 事后评估（每轮 REPL 后自动运行，无额外 LLM 调用）
2. Agent 工具 remember（Agent 主动调用，实时持久化）
"""

from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from typing import Any

from omniagent.repl.prompt_store import PromptEntry, PromptStore

logger = logging.getLogger(__name__)

# ── 去重阈值 ─────────────────────────────────────────────────
_DEDUP_SIMILARITY = 0.65  # 内容相似度超过此阈值视为重复

# ── 触发正则 ─────────────────────────────────────────────────

_PREFERENCE_PATTERNS = [
    re.compile(
        r"(?:always|prefer|never|usually|习惯|总是|通常|不要|偏好|喜欢用|常用|一直用)",
        re.IGNORECASE,
    ),
    re.compile(r"(?:请务必|一定要|记得|别忘了|以后都|每次都要)"),
]

_CORRECTION_PATTERNS = [
    re.compile(
        r"(?:不对|不是|错了|误解|理解错|不应该|说错了|搞错了)",
        re.IGNORECASE,
    ),
    # 英文纠正：要求纠正词后面有空白 + 情态/确认词
    # 匹配 "No, you should..." / "Actually, that's wrong" 等
    re.compile(
        r"(?:correct|wrong|misunderstand|actually|rather|instead|no,)\s+"
        r"(?:should|you should|it's|that's|i meant|what i meant)",
        re.IGNORECASE,
    ),
]

# ── 错误模式记忆（同一错误出现 ≥2 次触发） ─────────────────
_ERROR_MEMORY: dict[str, int] = {}  # key: error_signature → count


def _error_signature(error_text: str) -> str:
    """提取错误签名（用于跨轮次匹配同一类错误）。"""
    # 去除具体文件路径、行号、时间戳等变量部分
    sig = re.sub(r'File ".*?", line \d+', 'File "<path>", line <N>', error_text)
    sig = re.sub(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}', '<timestamp>', sig)
    sig = re.sub(r'0x[0-9a-fA-F]+', '<hex>', sig)
    # 取前 200 字符作为签名
    return sig[:200].strip()


def _similarity(a: str, b: str) -> float:
    """计算两段文本的相似度。"""
    return SequenceMatcher(None, a, b).ratio()


class PromptMemoryManager:
    """自主持久化评估器。

    分析对话内容，检测值得存储的模式，写入 PromptStore。
    """

    def __init__(self, store: PromptStore) -> None:
        self.store = store
        self._persisted_hashes: set[int] = set()  # content hash 去重

    def evaluate(
        self,
        history: list[Any],
        user_input: str,
        assistant_output: str,
    ) -> list[dict[str, Any]]:
        """评估对话轮次，返回待持久化的候选列表。

        Args:
            history: 对话历史（ConversationTurn 列表或 dict 列表）
            user_input: 用户输入文本
            assistant_output: Agent 输出文本

        Returns:
            候选列表，每项 {"category": str, "content": str, "tags": list[str]}
        """
        candidates: list[dict[str, Any]] = []

        # 1. 检测用户偏好
        pref = self._detect_preference(user_input, assistant_output)
        if pref:
            candidates.append(pref)

        # 2. 检测用户纠正
        corr = self._detect_correction(user_input, assistant_output, history)
        if corr:
            candidates.append(corr)

        # 3. 检测重复错误
        err = self._detect_error_pattern(assistant_output)
        if err:
            candidates.append(err)

        return candidates

    def persist(
        self,
        category: str,
        content: str,
        tags: list[str] | None = None,
    ) -> PromptEntry | None:
        """持久化一条记忆到 PromptStore。

        执行去重检查，避免重复写入相似内容。
        """
        content_hash = hash(content.strip())

        # 去重：完全相同 → 跳过
        if content_hash in self._persisted_hashes:
            logger.debug("跳过重复记忆: %s", content[:50])
            return None

        # 去重：与已有 memory 相似度检查
        for existing in self.store.list_memories():
            if _similarity(content, existing.content) > _DEDUP_SIMILARITY:
                logger.debug("跳过相似记忆: %s ≈ %s", content[:50], existing.content[:50])
                return None

        entry = self.store.add_memory(
            name=category,
            content=content,
            tags=tags or [],
            priority="medium",
        )
        self._persisted_hashes.add(content_hash)
        return entry

    # ═══════════════════════════════════════════════════════════
    # 检测方法
    # ═══════════════════════════════════════════════════════════

    def _detect_preference(
        self,
        user_input: str,
        assistant_output: str,
    ) -> dict[str, Any] | None:
        """检测用户偏好信号。

        匹配规则：
        - 用户输入包含偏好关键词
        - 且助手输出中确认了该偏好（避免误触发 "always forget" 这类不相关句子）
        """
        for pat in _PREFERENCE_PATTERNS:
            if pat.search(user_input):
                # 验证助手确认 — 防止 "Never mind" / "I always forget" 误触发
                ack_kw = ["好的", "明白", "了解", "记住了", "noted", "understood", "got it", "ok"]
                if not any(kw in assistant_output.lower() for kw in ack_kw):
                    return None
                # 提取偏好内容
                clean = user_input.strip().replace("\n", " ")
                if len(clean) > 200:
                    clean = clean[:200] + "..."
                return {
                    "category": "user-prefs",
                    "content": f"用户偏好: {clean}",
                    "tags": ["user-preference"],
                }
        return None

    def _detect_correction(
        self,
        user_input: str,
        assistant_output: str,
        history: list[Any],
    ) -> dict[str, Any] | None:
        """检测用户对 Agent 的纠正。

        条件：
        - 用户输入包含纠正关键词
        - 且助手确认了纠正（输出包含 "理解/明白/好的/抱歉"）
        """
        is_correction = any(pat.search(user_input) for pat in _CORRECTION_PATTERNS)
        if not is_correction:
            return None

        assistant_ack = any(
            kw in assistant_output.lower()
            for kw in ["理解", "明白", "好的", "抱歉", "sorry", "understood", "got it"]
        )
        if not assistant_ack:
            return None

        clean = user_input.strip().replace("\n", " ")
        if len(clean) > 200:
            clean = clean[:200] + "..."
        return {
            "category": "learned-patterns",
            "content": f"用户纠正: {clean}",
            "tags": ["correction", "learned"],
        }

    def _detect_error_pattern(self, assistant_output: str) -> dict[str, Any] | None:
        """检测重复出现的错误模式。

        同一错误签名出现 ≥2 次时触发持久化。
        """
        # 检查输出中是否包含错误标记
        has_error = bool(
            re.search(r"(?:error|错误|异常|失败|traceback)", assistant_output, re.IGNORECASE)
        )
        if not has_error:
            return None

        sig = _error_signature(assistant_output)
        count = _ERROR_MEMORY.get(sig, 0) + 1
        _ERROR_MEMORY[sig] = count

        if count >= 2:
            # 清理全局记录（已触发持久化）
            del _ERROR_MEMORY[sig]
            clean = assistant_output.strip().replace("\n", " ")
            if len(clean) > 300:
                clean = clean[:300] + "..."
            return {
                "category": "learned-patterns",
                "content": f"重复错误模式 (出现 {count} 次): {clean}",
                "tags": ["error-pattern", "learned"],
            }

        return None


def reset_error_memory() -> None:
    """清空全局错误模式记录（用于测试重置）。"""
    _ERROR_MEMORY.clear()
