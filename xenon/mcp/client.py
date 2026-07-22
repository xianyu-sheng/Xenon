"""
MCP Client — Model Context Protocol 客户端。

实现 MCP 协议的核心方法：
- initialize: 初始化连接
- tools/list: 列出服务器提供的工具
- tools/call: 调用服务器工具
- resources/list: 列出服务器资源
- resources/read: 读取服务器资源
"""

from __future__ import annotations

import logging
from typing import Any

from xenon import __version__
from xenon.mcp.transport import MCPTransport, StdioTransport, SSETransport

logger = logging.getLogger(__name__)


class MCPClient:
    """MCP 客户端。"""

    def __init__(self, transport: MCPTransport, name: str = "xenon") -> None:
        self.transport = transport
        self.name = name
        self.server_info: dict[str, Any] = {}
        self.tools: list[dict[str, Any]] = []
        self.resources: list[dict[str, Any]] = []
        self._initialized = False

    @classmethod
    def from_command(cls, command: str, args: list[str] | None = None,
                     env: dict[str, str] | None = None, name: str = "xenon") -> MCPClient:
        """从命令行创建 stdio 客户端。"""
        transport = StdioTransport(command, args, env)
        return cls(transport, name)

    @classmethod
    def from_url(cls, url: str, headers: dict[str, str] | None = None,
                 name: str = "xenon") -> MCPClient:
        """从 URL 创建 SSE 客户端。"""
        transport = SSETransport(url, headers)
        return cls(transport, name)

    def initialize(self) -> dict[str, Any]:
        """初始化 MCP 连接。"""
        result = self.transport.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {},
                "resources": {},
            },
            "clientInfo": {
                "name": self.name,
                "version": __version__,
            },
        })

        if "error" in result:
            raise RuntimeError(f"MCP 初始化失败: {result['error']}")

        self.server_info = result.get("result", {}).get("serverInfo", {})
        self._initialized = True

        # 发送 initialized 通知
        self.transport.send({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        })

        logger.info(f"MCP 服务器已连接: {self.server_info.get('name', 'unknown')}")
        return result.get("result", {})

    def list_tools(self) -> list[dict[str, Any]]:
        """列出服务器提供的工具。"""
        if not self._initialized:
            self.initialize()

        result = self.transport.request("tools/list")
        if "error" in result:
            raise RuntimeError(f"列出工具失败: {result['error']}")

        self.tools = result.get("result", {}).get("tools", [])
        return self.tools

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """调用服务器工具。"""
        if not self._initialized:
            self.initialize()

        params = {"name": name}
        if arguments:
            params["arguments"] = arguments

        result = self.transport.request("tools/call", params)
        if "error" in result:
            raise RuntimeError(f"调用工具 '{name}' 失败: {result['error']}")

        return result.get("result", {})

    def list_resources(self) -> list[dict[str, Any]]:
        """列出服务器资源。"""
        if not self._initialized:
            self.initialize()

        result = self.transport.request("resources/list")
        if "error" in result:
            raise RuntimeError(f"列出资源失败: {result['error']}")

        self.resources = result.get("result", {}).get("resources", [])
        return self.resources

    def read_resource(self, uri: str) -> dict[str, Any]:
        """读取服务器资源。"""
        if not self._initialized:
            self.initialize()

        result = self.transport.request("resources/read", {"uri": uri})
        if "error" in result:
            raise RuntimeError(f"读取资源 '{uri}' 失败: {result['error']}")

        return result.get("result", {})

    def close(self) -> None:
        """关闭连接。"""
        self.transport.close()

    def get_tool_schema(self, tool_name: str) -> dict[str, Any] | None:
        """获取工具的 JSON Schema。"""
        for tool in self.tools:
            if tool.get("name") == tool_name:
                return tool.get("inputSchema", {})
        return None

    def format_tools_for_prompt(self) -> str:
        """将 MCP 工具格式化为 LLM 提示词。"""
        if not self.tools:
            self.list_tools()

        lines = []
        for tool in self.tools:
            name = tool.get("name", "unknown")
            desc = tool.get("description", "")
            schema = tool.get("inputSchema", {})
            props = schema.get("properties", {})
            required = schema.get("required", [])

            params = []
            for pname, pinfo in props.items():
                req = " (必填)" if pname in required else ""
                params.append(f"{pname}: {pinfo.get('type', 'any')}{req}")

            params_str = ", ".join(params) if params else "无参数"
            lines.append(f"- mcp:{name}: {desc} (参数: {params_str})")

        return "\n".join(lines)
