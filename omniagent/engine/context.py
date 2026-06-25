"""
AgentContext — 全局上下文总线。

所有节点共享的运行时状态容器。节点通过 context 读写数据，
实现解耦的数据传递（例如 LLMNode 写入 output_slot，RouterNode 读取该 slot 做条件判断）。
"""

from __future__ import annotations

import copy
from typing import Any


class AgentContext:
    """线程不安全的全局状态总线，单次 DAG 执行周期内使用。"""

    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self._store: dict[str, Any] = initial.copy() if initial else {}
        self._history: list[dict[str, Any]] = []  # 每步快照，用于调试回溯
        self._conversation_messages: list[dict[str, str]] = []  # 多轮对话历史
        self.prompt_store: Any = None  # PromptStore 引用（由 REPL 注入）

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
        return self._store.copy()

    def items(self):
        """返回 _store.items() 的视图。"""
        return self._store.items()

    @property
    def history(self) -> list[dict[str, Any]]:
        return list(self._history)

    def __repr__(self) -> str:
        return f"AgentContext(keys={list(self._store.keys())})"
