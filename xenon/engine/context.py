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


class AgentContext:
    """线程不安全的全局状态总线，单次 DAG 执行周期内使用。"""

    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self._store: dict[str, Any] = initial.copy() if initial else {}
        self._history: list[dict[str, Any]] = []  # 每步快照，用于调试回溯
        self._conversation_messages: list[dict[str, str]] = []  # 多轮对话历史
        # Tool execution can happen in Plan-Execute worker threads.  Keep its
        # durable checkpoint updates atomic without changing the historical
        # single-threaded contract of the rest of AgentContext.
        self._tool_checkpoint_lock = threading.RLock()
        self._tool_checkpoint_callback: Any = None

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

    def record_tool_checkpoint(
        self,
        checkpoint: dict[str, Any],
        *,
        history_limit: int = 32,
    ) -> None:
        """Atomically retain and optionally persist a privacy-safe checkpoint."""
        callback = None
        with self._tool_checkpoint_lock:
            item = copy.deepcopy(checkpoint)
            history = list(self._store.get("_tool_execution_history", []))
            history.append(item)
            self._store["_tool_execution_history"] = history[-history_limit:]
            self._store["_tool_execution_checkpoint"] = item
            callback = self._tool_checkpoint_callback
        if callback is not None:
            try:
                callback(item)
            except Exception as exc:  # noqa: BLE001 - persistence is best effort
                logger.debug("工具恢复点持久化失败（不影响执行）: %s", exc)

    def items(self):
        """返回 _store.items() 的视图。"""
        return self._store.items()

    @property
    def history(self) -> list[dict[str, Any]]:
        return list(self._history)

    def __repr__(self) -> str:
        return f"AgentContext(keys={list(self._store.keys())})"
