"""BaseTool — 所有工具的抽象基类。

每个工具必须实现:
- name: 工具名 (如 "read_file")
- description: 工具描述 (用于 LLM 提示词)
- input_schema: JSON Schema 参数定义
- invoke(params) -> ToolResult: 执行逻辑

设计原则:
- 工具是无状态的，所有状态通过 params 和 context 传入
- ToolResult 统一返回格式，支持成功/失败/超时/权限拒绝等状态
- 与 KamaClaude 的 BaseTool 设计对齐
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel


@dataclass
class ToolResult:
    """工具执行结果。

    Attributes:
        content: 执行输出文本
        is_error: 是否出错
        error_type: 错误分类 — "runtime_error" | "timeout" | "schema_error" | "permission_denied"
        metadata: 附加元数据 (如文件路径、行数等)
    """

    content: str
    is_error: bool = False
    error_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, content: str, **metadata: Any) -> "ToolResult":
        """创建成功结果。"""
        return cls(content=content, metadata=metadata)

    @classmethod
    def error(cls, content: str, error_type: str = "runtime_error") -> "ToolResult":
        """创建错误结果。"""
        return cls(content=content, is_error=True, error_type=error_type)

    @classmethod
    def permission_denied(cls, reason: str) -> "ToolResult":
        """创建权限拒绝结果。"""
        return cls(content=reason, is_error=True, error_type="permission_denied")

    @classmethod
    def timeout(cls, tool_name: str, timeout_s: int) -> "ToolResult":
        """创建超时结果。"""
        return cls(
            content=f"Tool '{tool_name}' timed out after {timeout_s}s",
            is_error=True,
            error_type="timeout",
        )

    @classmethod
    def schema_error(cls, message: str) -> "ToolResult":
        """创建参数校验错误结果。"""
        return cls(content=message, is_error=True, error_type="schema_error")


class BaseTool(ABC):
    """所有 Agent 工具的抽象基类。

    子类必须设置:
        name: str — 工具名
        description: str — 工具描述
        input_schema: dict — JSON Schema 参数定义
        params_model: type[BaseModel] | None — Pydantic 参数模型（可选，用于校验）

    子类必须实现:
        async invoke(params) -> ToolResult
    """

    name: str = ""
    description: str = ""
    input_schema: dict[str, object] = {}
    params_model: ClassVar[type[BaseModel] | None] = None

    @abstractmethod
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        """执行工具调用。

        Args:
            params: 工具参数字典，key 为参数名，value 为参数值。

        Returns:
            ToolResult 统一执行结果。
        """
        ...

    def validate_params(self, params: dict[str, object]) -> dict[str, object]:
        """使用 Pydantic 模型校验参数（如果定义了 params_model）。

        返回校验后的参数字典。校验失败返回包含错误信息的空字典。
        子类可以在 invoke 前调用此方法。
        """
        if self.params_model is None:
            return params
        try:
            validated = self.params_model(**params)
            return validated.model_dump()
        except Exception as e:
            raise ValueError(f"参数校验失败 ({self.name}): {e}")

    def to_schema(self) -> dict[str, object]:
        """返回 Anthropic-compatible 工具 schema。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }
