"""OmniAgent 工具模块 — 基于 BaseTool 抽象的工具注册与执行体系。

每个工具都是独立的 BaseTool 子类，通过 ToolRegistry 统一管理，
替代原有的 ToolNode 单体类架构。
"""

from omniagent.tools.base import BaseTool, ToolResult
from omniagent.tools.registry import ToolRegistry

__all__ = ["BaseTool", "ToolResult", "ToolRegistry"]
