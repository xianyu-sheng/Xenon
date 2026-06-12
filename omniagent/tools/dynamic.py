"""动态工具注册 — RegisterTool + DynamicTool。

支持运行时通过 Python 函数路径或 shell 命令模板注册新工具。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import subprocess
from typing import Any, Callable

from omniagent.tools.base import BaseTool, ToolResult
from omniagent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# 全局动态工具注册表（供 RegisterTool 写入，其他引擎读取）
_dynamic_registry: ToolRegistry | None = None


def get_dynamic_registry() -> ToolRegistry:
    """获取全局动态工具注册表。"""
    global _dynamic_registry
    if _dynamic_registry is None:
        _dynamic_registry = ToolRegistry()
    return _dynamic_registry


class RegisterTool(BaseTool):
    """注册新的自定义工具。支持 python_function 和 command_template 两种模式。"""

    name = "register_tool"
    description = (
        "注册一个新的自定义工具。支持两种模式："
        "1) python_function: 传入 module.function 格式的 Python 函数路径；"
        "2) command_template: 传入 shell 命令模板（用 {param} 表示参数占位符）。"
        "注册成功后工具立即可用。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "tool_name": {"type": "string", "description": "新工具的名称（英文）"},
            "description": {"type": "string", "description": "工具功能描述，LLM 据此决定何时调用"},
            "python_function": {"type": "string", "description": "Python 函数路径，如 omniagent.utils.weather.get_weather"},
            "command_template": {"type": "string", "description": "Shell 命令模板，用 {param} 表示参数占位符"},
            "params": {"type": "object", "description": "参数定义字典 (JSON Schema)"},
        },
        "required": ["tool_name", "description"],
    }

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        tool_name = str(params.get("tool_name", ""))
        description = str(params.get("description", ""))
        param_schema = _parse_param_schema(params.get("params"))

        if not tool_name:
            return ToolResult.schema_error("register_tool 需要 tool_name 参数")

        python_function = str(params.get("python_function", ""))
        command_template = str(params.get("command_template", ""))

        registry = get_dynamic_registry()

        if python_function:
            return await self._register_python(tool_name, description, python_function, param_schema, registry)
        elif command_template:
            return await self._register_command(tool_name, description, command_template, param_schema, registry)
        else:
            return ToolResult.schema_error("必须提供 python_function 或 command_template 参数")

    async def _register_python(
        self, name: str, desc: str, func_path: str, schema: dict, registry: ToolRegistry,
    ) -> ToolResult:
        parts = func_path.rsplit(".", 1)
        if len(parts) != 2:
            return ToolResult.schema_error(f"python_function 格式错误，应为 module.function: {func_path}")

        try:
            mod = importlib.import_module(parts[0])
            func = getattr(mod, parts[1])
            if not callable(func):
                return ToolResult.schema_error(f"{func_path} 不是可调用对象")
        except Exception as e:
            return ToolResult.error(f"导入失败: {e}")

        tool = _make_function_tool(name, desc, schema, func)
        registry.register_dynamic(tool)
        logger.info(f"动态工具注册成功: {name} (Python: {func_path})")
        return ToolResult.ok(f"✅ 工具 '{name}' 注册成功（Python 函数: {func_path}）")

    async def _register_command(
        self, name: str, desc: str, template: str, schema: dict, registry: ToolRegistry,
    ) -> ToolResult:
        tool = _make_command_tool(name, desc, schema, template)
        registry.register_dynamic(tool)
        logger.info(f"动态工具注册成功: {name} (命令模板: {template})")
        return ToolResult.ok(f"✅ 工具 '{name}' 注册成功（命令模板: {template}）")


def _parse_param_schema(raw: Any) -> dict[str, object]:
    """解析参数 schema。"""
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    return {}


class _FunctionTool(BaseTool):
    """包装 Python 函数为 BaseTool。"""

    def __init__(self, name: str, description: str, input_schema: dict, func: Callable) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self._func = func

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        try:
            if asyncio.iscoroutinefunction(self._func):
                result = await self._func(**params)
            else:
                result = self._func(**params)
            return ToolResult.ok(str(result))
        except Exception as e:
            return ToolResult.error(str(e))


class _CommandTool(BaseTool):
    """包装 shell 命令模板为 BaseTool。"""

    def __init__(self, name: str, description: str, input_schema: dict, template: str) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self._template = template

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        cmd = self._template
        for key, value in params.items():
            cmd = cmd.replace(f"{{{key}}}", str(value))

        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            output = stdout.decode("utf-8", errors="replace").strip()
            if proc.returncode != 0:
                output += f"\n[STDERR]\n{stderr.decode('utf-8', errors='replace').strip()}"
            return ToolResult.ok(output, command=cmd, returncode=proc.returncode)
        except asyncio.TimeoutError:
            return ToolResult.timeout(self.name, 30)
        except Exception as e:
            return ToolResult.error(str(e))


def _make_function_tool(
    name: str, description: str, input_schema: dict, func: Callable,
) -> BaseTool:
    return _FunctionTool(name, description, input_schema, func)


def _make_command_tool(
    name: str, description: str, input_schema: dict, template: str,
) -> BaseTool:
    return _CommandTool(name, description, input_schema, template)
