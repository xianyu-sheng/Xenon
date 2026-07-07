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
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

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


@dataclass
class ConversationTurn:
    """一轮对话记录。"""

    role: str  # "user" | "assistant" | "system" | "tool"
    content: str
    model_used: str | None = None
    node_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
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
        if track_real_usage:
            self._subscribe_usage()

    # ── 对话管理 ──────────────────────────────────────────

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """添加一条消息到历史。"""
        turn = ConversationTurn(role=role, content=content, **kwargs)
        self.history.append(turn)

    def add_user_message(self, content: str) -> None:
        self.add_message("user", content)

    def add_assistant_message(self, content: str, *, model_used: str | None = None) -> None:
        self.add_message("assistant", content, model_used=model_used)

    def add_system_message(self, content: str) -> None:
        self.add_message("system", content)

    def get_messages(self) -> list[dict[str, str]]:
        """将历史转换为 LLM API 所需的 messages 格式。"""
        return [{"role": turn.role, "content": turn.content} for turn in self.history]

    def trim_last_assistant(self) -> str | None:
        """移除并返回最后一条 assistant 消息（用于撤回 LLM 幻觉回复）。"""
        for i in range(len(self.history) - 1, -1, -1):
            if self.history[i].role == "assistant":
                return self.history.pop(i).content
        return None

    # ── Token 估算 ────────────────────────────────────────

    def _subscribe_usage(self) -> None:
        """订阅 llm_client 的 usage 回调（P3-Q1 续 / §8.8.1）。

        懒导入避免 repl ↔ utils 循环；回调异常已被 llm_client 隔离，这里只做记录。
        """
        try:
            from omniagent.utils.llm_client import register_usage_callback

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
        # P3-Q1 续：回退到旧快照后，记录的真实 usage 已不对应当前 history → 失效
        self._real_usage = None
        return True

    @property
    def undo_depth(self) -> int:
        return len(self._undo_stack)

    # ── /compact 压缩 ────────────────────────────────────

    # F3：6 段结构化摘要的段名（顺序即输出顺序）
    _SIX_SEGMENTS = (
        "原始目标", "已完成步骤", "关键约束",
        "当前文件状态", "剩余待办", "关键数据",
    )

    def compact(
        self,
        summary: str | None = None,
        model_priority: list[str] | None = None,
        *,
        session_id: str | None = None,
    ) -> str:
        """压缩对话历史（F3 三层策略 + 6 段结构化摘要）。

        分流 by usage_ratio():
        - Tier 1 (<compact_threshold=60% 且无手动摘要): 跳过，不改写历史；
        - Tier 2 (60-85%): LLM 6 段压缩 older，保留 recent 3 轮；
        - Tier 3 (>compact_force=85%): _safe_truncation 安全截断（不调 LLM，
          避免超限输入触发 400），保留 system + 最近 30% 非系统消息。

        B5 兼容：older 为空时直接返回（不反向增加消息）。
        """
        ratio = self.usage_ratio()
        older, recent = self._split_recent(keep_rounds=3)

        # B5：无可压缩的早期消息 → 不改写历史
        if not older:
            return summary or "（无可压缩的早期对话，无需压缩）"

        # Tier 1：<60% 且无手动摘要 → 跳过
        if ratio < self.compact_threshold and not summary:
            return f"（当前上下文使用率 {ratio:.0%}，低于 {self.compact_threshold:.0%} 阈值，无需压缩）"

        self.save_snapshot()

        # P3-Q1 续：压缩自身的 LLM 摘要调用会经 usage 回调，但其 prompt 是被
        # 压缩的 older 片段、非当前 history → 抑制记录，避免污染 current_token_usage
        prev_suppress = self._suppress_usage
        self._suppress_usage = True
        try:
            if ratio > self.compact_force and not summary:
                # Tier 3：>85% 安全截断（不调 LLM）
                new_history = self._safe_truncation()
                summary = (
                    f"（上下文使用率 {ratio:.0%} 超过 {self.compact_force:.0%}，"
                    f"已安全截断，保留 system + 最近 {len(new_history)} 条消息）"
                )
            else:
                # Tier 2：60-85% LLM 6 段压缩（或手动摘要）
                summary = summary or self._llm_summary_6seg(model_priority, older)
                new_history = [
                    ConversationTurn(
                        role="system",
                        content=(
                            f"[对话历史已压缩] 以下是之前 {len(older)} 条消息的 6 段摘要：\n\n{summary}"
                        ),
                    )
                ] + recent
        finally:
            self._suppress_usage = prev_suppress

        self.history = new_history
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
            from omniagent.utils.llm_client import chat_completion

            # 构建对话文本并头尾截断
            parts = []
            for t in messages[-20:]:  # 最多取 20 条喂给 LLM
                tag = {"user": "用户", "assistant": "助手", "system": "系统", "tool": "工具"}.get(t.role, t.role)
                parts.append(f"[{tag}] {t.content[:300]}")
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
            for model_id in (model_priority or []):
                try:
                    raw = chat_completion(model_id, msgs, max_tokens=800, temperature=0.3)
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
            base = self.persist_dir or (Path.home() / ".omniagent" / "sessions" / sid)
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
