"""
MCP Registry — MCP 服务器注册和工具发现。

管理多个 MCP 服务器连接，统一工具命名空间。
"""

from __future__ import annotations

import logging
from typing import Any

from xenon.mcp.client import MCPClient

logger = logging.getLogger(__name__)


CATEGORY_KEYWORDS = {
    "write_file": [
        "write", "edit", "create", "append", "insert", "save", "overwrite", "put",
        "modify", "update", "patch", "replace"
    ],
    "read_file": [
        "read", "get", "fetch", "load", "cat", "view", "show", "open",
        "display", "print", "output"
    ],
    "search": [
        "search", "grep", "find", "query", "locate", "scan", "match",
        "lookup", "filter", "detect"
    ],
    "command": [
        "command", "exec", "run", "shell", "bash", "terminal", "sh",
        "execute", "spawn", "launch"
    ],
    "git": [
        "git", "commit", "push", "pull", "clone", "branch", "checkout",
        "merge", "rebase", "status", "diff", "log"
    ],
    "web": [
        "web", "http", "fetch", "download", "browse", "request",
        "get_url", "post", "scrape", "crawl"
    ],
    "directory": [
        "list", "ls", "dir", "directory", "mkdir", "rmdir", "tree",
        "walk", "enumerate"
    ],
    "database": [
        "db", "database", "sql", "query", "insert", "update", "delete",
        "select", "table", "schema"
    ],
}


def infer_category(tool_name: str, description: str = "") -> str:
    """根据工具名和描述推断工具分类。"""
    text = (tool_name + " " + description).lower()
    for cat, kws in CATEGORY_KEYWORDS.items():
        if any(kw in text for kw in kws):
            return cat
    return "other"


def _validate_server_name(name: str) -> None:
    """校验 MCP 服务器名的合法性（命名空间标识符）。

    服务器名会进入两处对格式敏感的下游：
    1. 工具全局命名空间 ``{server}:{tool}``——含 ``:`` 的名字会让
       ``call_tool`` 的 ``split(":", 1)`` 路由解析产生歧义；
    2. credentials.yaml 持久化——空名/纯空白名会落盘成无法引用的条目。
    惰性模式（add_server_pending）不在注册时连接，若不在这里拦截，
    非法名字要到首次工具调用才失败，排查链路长得多。
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"MCP 服务器名不能为空: {name!r}")
    if name != name.strip():
        raise ValueError(f"MCP 服务器名首尾不能含空白字符: {name!r}")
    if ":" in name:
        raise ValueError(
            f"MCP 服务器名不能包含 ':'（与工具命名空间 server:tool 冲突）: {name!r}"
        )


def _mcp_result_summary(result: dict[str, Any]) -> str:
    """从 MCP tools/call 结果提取短摘要（content 文本或错误）。"""
    if not isinstance(result, dict):
        return str(result)[:300]
    if result.get("error"):
        return str(result["error"])[:300]
    content = result.get("content")
    if isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                texts.append(item["text"])
        joined = " ".join(texts)
        return joined[:300] if joined else str(result)[:300]
    return str(result)[:300]


class MCPRegistry:
    """MCP 服务器注册表。

    支持两种模式：
    - 即时模式：add_server() 立即启动子进程并连接
    - 惰性模式：add_server_pending() 仅存储配置，首次 discover_tools() 或
      _ensure_connected() 时才真正连接（避免启动阻塞）
    """

    def __init__(self) -> None:
        # server_name -> MCPClient
        self.clients: dict[str, MCPClient] = {}
        # tool_name -> (server_name, tool_info)
        self.tool_map: dict[str, tuple[str, dict[str, Any]]] = {}
        # 惰性模式：尚未连接的服务器配置（name -> {command, args, url, env}）
        self._pending_configs: dict[str, dict[str, Any]] = {}
        self.tool_categories: dict[str, list[str]] = {}

    def add_server(
        self,
        name: str,
        command: str | None = None,
        url: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> MCPClient:
        """添加 MCP 服务器。

        Args:
            name: 服务器名称（用于命名空间）
            command: stdio 模式的命令
            url: SSE 模式的 URL
            args: 命令参数
            env: 环境变量
        """
        _validate_server_name(name)
        if name in self.clients:
            logger.warning(f"MCP 服务器 '{name}' 已存在，跳过")
            return self.clients[name]

        if command:
            client = MCPClient.from_command(command, args, env, name=name)
        elif url:
            client = MCPClient.from_url(url, headers=headers, name=name)
        else:
            raise ValueError(f"MCP 服务器 '{name}' 需要 command 或 url")

        self.clients[name] = client
        logger.info(f"MCP 服务器已注册: {name}")
        return client

    def add_server_pending(
        self,
        name: str,
        command: str | None = None,
        url: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """仅存储服务器配置，不立即连接（惰性模式）。

        首次 discover_tools() 或 _ensure_connected(name) 时才真正启动子进程。
        """
        _validate_server_name(name)
        if not command and not url:
            raise ValueError(f"MCP 服务器 '{name}' 需要 command 或 url")
        if name in self.clients or name in self._pending_configs:
            logger.debug(f"MCP 服务器 '{name}' 已注册（惰性或已连接），跳过")
            return
        self._pending_configs[name] = {
            "command": command,
            "url": url,
            "args": args or [],
            "env": env,
            "headers": headers,
        }
        logger.info(f"MCP 服务器已登记（惰性）: {name}")

    def _ensure_connected(self, name: str | None = None) -> None:
        """确保指定或全部惰性服务器已连接。

        Args:
            name: 服务器名，为 None 时连接全部惰性服务器
        """
        if name and name not in self._pending_configs:
            return  # 已连接或不存在

        names_to_connect = [name] if name else list(self._pending_configs.keys())
        for n in names_to_connect:
            cfg = self._pending_configs.get(n)
            if cfg is None:
                continue
            try:
                if cfg.get("url"):
                    self.add_server(
                        n,
                        url=str(cfg["url"]),
                        headers=cfg.get("headers"),
                    )
                elif cfg.get("command"):
                    self.add_server(
                        n,
                        command=str(cfg["command"]),
                        args=[str(a) for a in cfg.get("args", [])],
                        env=cfg.get("env"),
                    )
                self._pending_configs.pop(n, None)  # 连接成功后才移除配置
            except Exception as e:
                logger.warning(f"惰性连接 MCP '{n}' 失败: {e}")
                # 配置保留在 _pending_configs 中，下次 discover_tools() 重试

    def has_pending_servers(self) -> bool:
        """是否有尚未连接的惰性服务器。"""
        return len(self._pending_configs) > 0

    def get_pending_server_names(self) -> list[str]:
        """返回尚未连接的惰性服务器名列表。"""
        return list(self._pending_configs.keys())

    def discover_tools(self) -> dict[str, list[dict[str, Any]]]:
        """发现所有服务器的工具（惰性服务器会自动连接）。"""
        # 先连接所有惰性服务器
        self._ensure_connected()

        all_tools = {}
        for server_name, client in self.clients.items():
            try:
                tools = client.list_tools()
                all_tools[server_name] = tools
                for tool in tools:
                    tool_name = tool.get("name", "unknown")
                    # 使用 server:tool 作为全局名称
                    global_name = f"{server_name}:{tool_name}"
                    self.tool_map[global_name] = (server_name, tool)
                    # 也注册短名称（如果没有冲突）
                    if tool_name not in self.tool_map:
                        self.tool_map[tool_name] = (server_name, tool)
                logger.info(f"MCP 服务器 '{server_name}': 发现 {len(tools)} 个工具")
                # 分类
                cat = infer_category(tool_name, tool.get("description", "") if isinstance(tool, dict) else "")
                self.tool_categories.setdefault(cat, []).append(global_name)
            except Exception as e:
                logger.warning(f"MCP 服务器 '{server_name}' 工具发现失败: {e}")

        return all_tools

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        context: Any = None,
    ) -> dict[str, Any]:
        """调用 MCP 工具。支持 server:tool 或直接 tool 名称。

        如果工具所在服务器尚未连接（惰性），则自动先连接。

        传入 ``context``（AgentContext）时，调用前后自动向该任务的
        EvidenceRuntime 写入 tool_request / tool_observation 证据，
        与 ToolExecutor 的在线验证链共用同一条 ledger。
        """
        # 尝试从 tool_map 查找
        entry = self.tool_map.get(tool_name)

        if not entry:
            # 可能是惰性服务器上尚未发现的工具——解析 server 前缀
            if ":" in tool_name:
                server_name = tool_name.split(":", 1)[0]
            else:
                server_name = tool_name

            # 如果该服务器还在 pending 状态，先连接
            if server_name in self._pending_configs:
                logger.info(f"按需连接惰性 MCP 服务器: {server_name}")
                self._ensure_connected(server_name)
                # 连接后发现工具
                self.discover_tools()
                entry = self.tool_map.get(tool_name)

            if not entry:
                # 尝试带 server 前缀
                for prefix in self.clients:
                    full_name = f"{prefix}:{tool_name}"
                    entry = self.tool_map.get(full_name)
                    if entry:
                        break

        if not entry:
            # 如果还有惰性服务器未连接，尝试连接全部后再找
            if self.has_pending_servers():
                logger.info("按需连接所有惰性 MCP 服务器...")
                self._ensure_connected()
                self.discover_tools()
                entry = self.tool_map.get(tool_name)
                if not entry and ":" not in tool_name:
                    for prefix in self.clients:
                        full_name = f"{prefix}:{tool_name}"
                        entry = self.tool_map.get(full_name)
                        if entry:
                            break

        if not entry:
            available = list(self.tool_map.keys())
            raise ValueError(f"未知 MCP 工具: '{tool_name}'。可用: {available}")

        server_name, tool_info = entry
        client = self.clients[server_name]

        # 在线验证链：调用前记录工具请求证据（context 可选传入）。
        # 用 .evidence 触发懒创建——任何持有 AgentContext 的调用方都能接入。
        runtime = getattr(context, "evidence", None) if context is not None else None
        if runtime is not None:
            runtime.record_tool_request(
                tool=f"mcp_call:{tool_name}",
                params={"server": server_name, "arguments": arguments or {}},
            )
        try:
            result = client.call_tool(tool_info["name"], arguments)
        except Exception as exc:
            if runtime is not None:
                runtime.record_tool_observation(
                    tool=f"mcp_call:{tool_name}",
                    params={"server": server_name},
                    success=False,
                    summary=str(exc)[:300],
                )
            raise
        if runtime is not None:
            runtime.record_tool_observation(
                tool=f"mcp_call:{tool_name}",
                params={"server": server_name},
                success=True,
                summary=_mcp_result_summary(result),
            )
        return result

    def format_all_tools_for_prompt(self) -> str:
        """将所有 MCP 工具格式化为 LLM 提示词。"""
        if not self.tool_map:
            self.discover_tools()

        lines = []
        for global_name, (server_name, tool) in sorted(self.tool_map.items()):
            if ":" not in global_name:
                continue  # 只显示带前缀的
            desc = tool.get("description", "")
            schema = tool.get("inputSchema", {})
            props = schema.get("properties", {})
            required = schema.get("required", [])

            params = []
            for pname, pinfo in props.items():
                req = "(必填)" if pname in required else ""
                params.append(f"{pname}: {pinfo.get('type', 'any')}{req}")

            params_str = ", ".join(params) if params else "无参数"
            lines.append(f"- {global_name}: {desc} (参数: {params_str})")

        return "\n".join(lines) if lines else "（无 MCP 工具）"

    def close_all(self) -> None:
        """关闭所有连接。"""
        for name, client in self.clients.items():
            try:
                client.close()
            except Exception as e:
                logger.warning(f"关闭 MCP 服务器 '{name}' 失败: {e}")
        self.clients.clear()
        self.tool_map.clear()
        self._pending_configs.clear()

    @classmethod
    def from_config(cls, servers_config: list[dict[str, Any]]) -> MCPRegistry:
        """从配置创建注册表。

        配置格式:
        [
            {"name": "filesystem", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path"]},
            {"name": "web", "url": "http://localhost:3000/sse"},
        ]
        """
        registry = cls()
        for server in servers_config:
            name = server.get("name", "unknown")
            try:
                registry.add_server(
                    name=name,
                    command=server.get("command"),
                    url=server.get("url"),
                    args=server.get("args"),
                    env=server.get("env"),
                    headers=server.get("headers"),
                )
            except Exception as e:
                logger.warning(f"添加 MCP 服务器 '{name}' 失败: {e}")
        return registry
