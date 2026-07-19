"""
MCP (Model Context Protocol) 客户端模块。

支持连接外部 MCP 服务器，发现和调用其提供的工具。
协议基于 JSON-RPC 2.0，支持 stdio 和 SSE 传输。
"""

from xenon.mcp.client import MCPClient
from xenon.mcp.registry import MCPRegistry

__all__ = ["MCPClient", "MCPRegistry"]
