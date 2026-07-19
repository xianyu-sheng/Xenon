"""
BaseNode — 所有节点的抽象基类。

每个节点是一个原子化的执行单元，拥有：
- 唯一 id
- 执行方法 execute(context) -> dict | None
- 可选的 output_slot，用于将结果写入 context
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from xenon.engine.context import AgentContext


class BaseNode(ABC):
    """抽象基础节点。所有具体节点必须继承此类并实现 execute()。"""

    def __init__(
        self,
        node_id: str,
        *,
        output_slot: str | None = None,
        default_next: str | None = None,
    ) -> None:
        self.id = node_id
        self.output_slot = output_slot
        self.default_next = default_next

    @abstractmethod
    def execute(self, context: AgentContext) -> dict[str, Any] | None:
        """
        执行节点逻辑。

        Args:
            context: 全局上下文总线，可读写。

        Returns:
            节点执行结果（可选）。如果设置了 output_slot，
            调度器会自动将返回值写入 context[output_slot]。
        """
        ...

    def _write_output(self, context: AgentContext, value: Any) -> None:
        """将结果写入 context 的 output_slot。"""
        if self.output_slot:
            context.set(self.output_slot, value)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} id={self.id!r}>"
