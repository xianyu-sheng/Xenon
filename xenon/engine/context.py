"""
AgentContext — 全局上下文总线。

所有节点共享的运行时状态容器。节点通过 context 读写数据，
实现解耦的数据传递（例如 LLMNode 写入 output_slot，RouterNode 读取该 slot 做条件判断）。
"""

from __future__ import annotations

import copy
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

_ACTIVE_TOOL_STATES = frozenset({"pending", "running", "retrying"})


class AgentContext:
    """线程不安全的全局状态总线，单次 DAG 执行周期内使用。"""

    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self._store: dict[str, Any] = initial.copy() if initial else {}
        self._history: list[dict[str, Any]] = []  # 每步快照，用于调试回溯
        self._conversation_messages: list[dict[str, str]] = []
        # Tool execution can happen in Plan-Execute worker threads.  Keep its
        # durable checkpoint updates atomic without changing the historical
        # single-threaded contract of the rest of AgentContext.
        self._tool_checkpoint_lock = threading.RLock()
        self._tool_checkpoint_callback: Any = None
        self._escalation_callback: Any = None
        self._approval_callback: Any = None
        self._plan_callback: Any = None

    # ── 读写 ──────────────────────────────────────────────
    def get(self, key: str, default: Any = None) -> Any:
        return self._store.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._store[key] = value

    def update(self, data: dict[str, Any]) -> None:
        self._store.update(data)

    def has(self, key: str) -> bool:
        return key in self._store

    def snapshot(self) -> None:
        """保存当前状态快照（浅拷贝）。"""
        self._history.append(copy.copy(self._store))

    def set_conversation_messages(self, messages: list[dict[str, str]]) -> None:
        """设置多轮对话历史（由 REPL 层注入）。"""
        self._conversation_messages = messages

    def get_conversation_messages(self) -> list[dict[str, str]]:
        """获取多轮对话历史。"""
        return self._conversation_messages

    def to_dict(self) -> dict[str, Any]:
        """返回内部状态的浅拷贝（公开接口，替代直接访问 _store）。"""
        with self._tool_checkpoint_lock:
            return self._store.copy()

    def set_tool_checkpoint_callback(self, callback: Any = None) -> None:
        """Register a transient persistence hook for tool lifecycle changes.

        The callback is deliberately kept outside ``_store`` so session JSON
        never attempts to serialize a bound method or closure.
        """
        with self._tool_checkpoint_lock:
            self._tool_checkpoint_callback = callback

    def set_escalation_callback(self, callback: Any = None) -> None:
        """Register a transient hook that may grant a higher turn level.

        Called with ``(tool_name, required_level, reason)`` when a tool
exceeds the current turn's ``_execution_level``.  Returning True lets the
        caller raise the level and execute the tool; False keeps the denial.
        Kept outside ``_store`` for the same reason as the checkpoint hook.
        """
        with self._tool_checkpoint_lock:
            self._escalation_callback = callback

    def request_escalation(
        self,
        tool_name: str,
        required_level: int,
        reason: str = "",
    ) -> bool:
        """Ask the registered callback for permission; False when absent."""
        callback = self._escalation_callback
        if callback is None:
            return False
        try:
            return bool(callback(tool_name, required_level, reason))
        except Exception:  # noqa: BLE001 — 授权回调异常按拒绝处理
            return False

    def set_approval_callback(self, callback: Any = None) -> None:
        """Register the tool-boundary approval channel (REPL injects it)."""
        with self._tool_checkpoint_lock:
            self._approval_callback = callback

    def get_approval_callback(self) -> Any:
        """只读访问审批通道（子代理继承父上下文时使用）。"""
        return self._approval_callback

    def request_approval(
        self,
        tool_name: str,
        params: dict[str, Any] | None = None,
        reason: str = "",
    ) -> str:
        """Ask the boundary policy about one concrete tool call.

        Returns a closed outcome string.  When no channel is registered the
        call is allowed, preserving the level-based semantics of library and
        direct-engine users; the REPL always registers a channel, where the
        real policy (workspace writes free, commands and out-of-workspace
        writes ask) applies and every failure mode denies.

        ``_kernel_adjudicated`` short-circuits this: when the kernel's own
        ``PermissionPolicy`` has already ruled on the action, asking again here
        would be a *second* decision about the same call -- and one made from
        less information.  A measured run showed the two disagreeing in both
        directions: an in-workspace edit the kernel allowed came back to the
        human, while an out-of-workspace edit the kernel vetoed could be waved
        through by answering the panel.  The dependency inverts too: the
        executor runs *under* the kernel, so its fallback is for callers that
        have no kernel at all.  Those never set the flag.
        """

        from xenon.nodes.approval_policy import APPROVAL_ALLOWED_ONCE, APPROVAL_OUTCOMES

        if self._store.get("_kernel_adjudicated"):
            return APPROVAL_ALLOWED_ONCE
        callback = self._approval_callback
        if callback is None:
            return APPROVAL_ALLOWED_ONCE
        try:
            outcome = str(callback(tool_name, params or {}, reason) or "")
        except Exception:  # noqa: BLE001 — 审批通道异常按不可用（拒绝）处理
            return "unavailable"
        return outcome if outcome in APPROVAL_OUTCOMES else "unavailable"

    def add_security_advisories(
        self,
        provenance: dict[str, Any],
        advisories: list[dict[str, Any]],
        *,
        limit: int = 20,
    ) -> None:
        """Retain bounded, deduplicated advisory context for this turn."""

        if not isinstance(provenance, dict) or not advisories:
            return
        with self._tool_checkpoint_lock:
            current = list(self._store.get("_security_advisories", []))
            seen = {
                (
                    item.get("provenance", {}).get("source_type"),
                    item.get("provenance", {}).get("source_ref"),
                    item.get("advisory", {}).get("rule_id"),
                )
                for item in current
                if isinstance(item, dict)
            }
            compact_provenance = {
                "source_type": str(provenance.get("source_type") or "tool"),
                "source_ref": str(provenance.get("source_ref") or "")[:200],
                "untrusted": bool(provenance.get("untrusted", True)),
            }
            for advisory in advisories:
                if not isinstance(advisory, dict):
                    continue
                compact_advisory = {
                    "kind": str(advisory.get("kind") or "prompt_injection"),
                    "rule_id": str(advisory.get("rule_id") or ""),
                    "severity": str(advisory.get("severity") or "warning"),
                }
                if not compact_advisory["rule_id"]:
                    continue
                key = (
                    compact_provenance["source_type"],
                    compact_provenance["source_ref"],
                    compact_advisory["rule_id"],
                )
                if key in seen:
                    continue
                seen.add(key)
                current.append(
                    {
                        "provenance": compact_provenance,
                        "advisory": compact_advisory,
                    }
                )
            self._store["_security_advisories"] = current[-max(1, limit) :]

    def get_security_advisories(self) -> list[dict[str, Any]]:
        """Return a defensive copy of current-turn advisory context."""

        with self._tool_checkpoint_lock:
            return copy.deepcopy(self._store.get("_security_advisories", []))

    def clear_security_advisories(self) -> None:
        """Drop advisory influence at the same boundary as other turn state."""

        with self._tool_checkpoint_lock:
            self._store.pop("_security_advisories", None)

    def set_plan_callback(self, callback: Any = None) -> None:
        """Register the plan-approval channel (REPL injects it)."""
        with self._tool_checkpoint_lock:
            self._plan_callback = callback

    def request_plan_approval(self, plan_text: str) -> dict:
        """Ask the registered channel to approve a submitted plan.

        No channel (library/headless) fails closed: not approved.
        """
        callback = self._plan_callback
        if callback is None:
            return {"approved": False, "feedback": "无交互通道，无法批准计划"}
        try:
            result = callback(plan_text)
            if isinstance(result, dict):
                return {
                    "approved": bool(result.get("approved")),
                    "feedback": str(result.get("feedback") or ""),
                }
        except Exception:  # noqa: BLE001 — 审批通道异常按驳回
            pass
        return {"approved": False, "feedback": "审批通道异常"}

    def record_tool_checkpoint(
        self,
        checkpoint: dict[str, Any],
        *,
        history_limit: int = 32,
    ) -> None:
        """Atomically retain and optionally persist a privacy-safe checkpoint.

        ``_tool_execution_checkpoint`` remains the backward-compatible newest
        event. ``_tool_execution_active`` is the durable source of truth for
        every concurrently unfinished execution, keyed by execution ID.
        """
        with self._tool_checkpoint_lock:
            item = copy.deepcopy(checkpoint)
            history = list(self._store.get("_tool_execution_history", []))
            history.append(item)
            self._store["_tool_execution_history"] = history[-history_limit:]
            self._store["_tool_execution_checkpoint"] = item

            execution_id = item.get("execution_id")
            if execution_id:
                active = copy.deepcopy(self._store.get("_tool_execution_active", {}))
                if not isinstance(active, dict):
                    active = {}
                execution_key = str(execution_id)
                if item.get("state") in _ACTIVE_TOOL_STATES:
                    active[execution_key] = item
                else:
                    active.pop(execution_key, None)
                self._store["_tool_execution_active"] = active
            callback = self._tool_checkpoint_callback
            if callback is not None:
                # Keep state publication and its atomic session write in one
                # linearizable critical section.  If another worker could
                # mutate the ledger while this callback was writing, a hard
                # process kill could leave only the older worker on disk.
                try:
                    callback(item)
                except Exception as exc:  # noqa: BLE001 - best effort
                    logger.debug("工具恢复点持久化失败（不影响执行）: %s", exc)

    def items(self):
        """返回 _store.items() 的视图。"""
        return self._store.items()

    @property
    def history(self) -> list[dict[str, Any]]:
        return list(self._history)

    def __repr__(self) -> str:
        return f"AgentContext(keys={list(self._store.keys())})"
