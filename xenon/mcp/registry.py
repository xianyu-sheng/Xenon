"""
MCP Registry — MCP 服务器注册和工具发现。

管理多个 MCP 服务器连接，统一工具命名空间。
"""

from __future__ import annotations

import logging
import re
import threading
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
    """根据工具名和描述推断工具分类。

    注意：这是**仅供展示**的死数据（``tool_categories`` 目前无任何消费者），
    且关键词分类与 ``docs/ARCHITECTURE.md``/``CLAUDE.md``「禁止用封闭关键词集合
    做路由/分类」的原则存在张力（issue：应改用 inputSchema/tool type 驱动）。
    因此这里只做保守的词边界匹配，避免子串穿越（如 "web" 误中 "webhook"），
    分类不确定时返回 "other"。不应基于它做任何路由决策。
    """
    text = (tool_name + " " + description).lower()
    for cat, kws in CATEGORY_KEYWORDS.items():
        for kw in kws:
            # \b 词边界：防止子串误匹配（"web" 命中 "webhook"）、
            # 中文场景 \b 无法生效，按 token 切分后再比对。
            if re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])", text):
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


_REDACT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Bearer token（必须在 URL query 之前，避免 query 正则吃掉 Authorization 头）
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE), r"\1<redacted>"),
    # 常见密钥/令牌键值对（json 风格或 query 参数）
    (re.compile(r"(api[_-]?key|apikey|token|secret|password|passwd|authorization|credential)(\s*[:=]\s*)([^\s,}\"']+)", re.IGNORECASE), r"\1\2<redacted>"),
    # URL query 中的敏感参数值（仅匹配带 ? 的 query 片段）
    (re.compile(r"(\?[^\s#]*(?:&|^)[^=#\s]+=)[^&#\s]+", re.IGNORECASE), r"\1<redacted>"),
)


def _redact_text(text: str) -> str:
    """对将写入证据链/日志的文本做轻量脱敏。

    覆盖常见凭据形态（key=value、URL query、Bearer token）。
    只做模式替换，不改写文本结构；与 callbacks 的
    ``mask_sensitive_params``（参数级）互补。
    """
    if not isinstance(text, str):
        text = str(text)
    for pattern, repl in _REDACT_PATTERNS:
        text = pattern.sub(repl, text)
    return text


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
        # 并发保护：REPL 主线程驱动，但 engine/base.py 与 agent_context
        # 允许跨会话/回调并发访问共享注册表（见 PromptLanes/Context 的
        # RLock 先例），对共享 hash 的所有写路径加同一把锁。
        self._lock = threading.RLock()
        # server_name -> MCPClient
        self.clients: dict[str, MCPClient] = {}
        # tool_name -> (server_name, tool_info)
        self.tool_map: dict[str, tuple[str, dict[str, Any]]] = {}
        # 惰性模式：尚未连接的服务器配置（name -> {command, args, url, env}）
        self._pending_configs: dict[str, dict[str, Any]] = {}
        self.tool_categories: dict[str, list[str]] = {}
        # 短名歧义追踪：short_name -> 提供该短名的 server 集合。
        # 当多个 server 提供同名工具时，短名不再是可靠索引——
        # 不会被注册进 tool_map，避免隐式路由到"第一个"导致误调用。
        self._short_name_owners: dict[str, set[str]] = {}
        # 已判定为歧义的短名集合（供 call_tool 给出明确 disambiguation 提示）
        self.ambiguous_short_names: set[str] = set()

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
        with self._lock:
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
        with self._lock:
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
                with self._lock:
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
        """发现所有服务器的工具（惰性服务器会自动连接）。

        统一命名空间契约：``server:tool`` 是确定性主键（始终写入 tool_map）。
        短名仅是**便利别名**，仅当该短名在全部 server 中唯一时才注册；
        若多个 server 提供同名工具（歧义），短名不注册，统一走全名，
        避免 call_tool 用短名时隐式路由到"第一个"导致误调用。
        """
        # 先连接所有惰性服务器
        self._ensure_connected()

        all_tools = {}
        with self._lock:
            # 重建追踪状态：server 增删/连接失败后，残留的短名归属
            # 会让已移除的 server 继续影响歧义判定，必须每次重算。
            self._short_name_owners = {}
            self.ambiguous_short_names = set()
            self.tool_categories = {}
            for server_name, client in self.clients.items():
                try:
                    tools = client.list_tools()
                    all_tools[server_name] = tools
                    for tool in tools:
                        # 类型守卫：跳过不合规条目而不是 KeyError 崩掉整个发现
                        if not isinstance(tool, dict):
                            logger.warning(
                                f"MCP 服务器 '{server_name}' 返回不合规工具条目（非 dict）: {tool!r:.120}"
                            )
                            continue
                        tool_name = tool.get("name")
                        if not isinstance(tool_name, str) or not tool_name.strip():
                            logger.warning(
                                f"MCP 服务器 '{server_name}' 返回无合法 name 的工具条目，跳过"
                            )
                            continue
                        # 使用 server:tool 作为全局名称（确定性主键）
                        global_name = f"{server_name}:{tool_name}"
                        self.tool_map[global_name] = (server_name, tool)

                        # 短名歧义检测：记录每个短名由哪些 server 提供
                        owners = self._short_name_owners.setdefault(tool_name, set())
                        owners.add(server_name)

                        # 分类（供展示，见 infer_category docstring）
                        cat = infer_category(
                            tool_name,
                            tool.get("description", "") if isinstance(tool, dict) else "",
                        )
                        self.tool_categories.setdefault(cat, []).append(global_name)
                    logger.info(f"MCP 服务器 '{server_name}': 发现 {len(tools)} 个工具")
                except Exception as e:
                    logger.warning(f"MCP 服务器 '{server_name}' 工具发现失败: {e}")

            # 第二遍：短名若被多个 server 提供则判定为歧义，不注册短名；
            # 短名唯一时注册为便利别名（向后兼容：LLM 可能用短名调用）。
            self.ambiguous_short_names = {
                short for short, owners in self._short_name_owners.items()
                if len(owners) > 1
            }
            for short in self.ambiguous_short_names:
                # 从 tool_map 移除可能残留的歧义短名（多 server 场景下
                # 保证短名绝不指向某一个 server）
                self.tool_map.pop(short, None)
            for short, owners in self._short_name_owners.items():
                if len(owners) == 1:
                    # 唯一短名：注册为别名（server 已知，直接取全名条目）
                    (single_server,) = owners
                    full = f"{single_server}:{short}"
                    entry = self.tool_map.get(full)
                    if entry is not None:
                        self.tool_map[short] = entry
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
        # 歧义短名拦截：多个 server 提供同名工具时，短名不再是可靠索引。
        # 必须用 server:tool 全名调用，否则给出明确 disambiguation 提示，
        # 而不是静默路由到任一 server。
        if ":" not in tool_name and tool_name in self.ambiguous_short_names:
            owners = sorted(self._short_name_owners.get(tool_name, set()))
            raise ValueError(
                f"MCP 工具 '{tool_name}' 在多个服务器间存在歧义"
                f"（{', '.join(owners)}），请使用 'server:tool' 全名调用，"
                f"例如 '{owners[0]}:{tool_name}'"
            )

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
            # 防御：tool_info 可能缺 name（外部 MCP 服务数据不可信），
            # 用短名/全名做兜底，避免 KeyError 崩溃。
            target_name = tool_info.get("name", tool_name.split(":")[-1] if ":" in tool_name else tool_name)
            result = client.call_tool(target_name, arguments)
        except Exception as exc:
            if runtime is not None:
                runtime.record_tool_observation(
                    tool=f"mcp_call:{tool_name}",
                    params={"server": server_name},
                    success=False,
                    # 脱敏后再落入证据链，避免异常文本泄露 API key/路径等
                    summary=_redact_text(str(exc))[:300],
                )
            raise
        if runtime is not None:
            runtime.record_tool_observation(
                tool=f"mcp_call:{tool_name}",
                params={"server": server_name},
                success=True,
                summary=_redact_text(_mcp_result_summary(result))[:300],
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
        with self._lock:
            for name, client in self.clients.items():
                try:
                    client.close()
                except Exception as e:
                    logger.warning(f"关闭 MCP 服务器 '{name}' 失败: {e}")
            self.clients.clear()
            self.tool_map.clear()
            self._pending_configs.clear()
            self.tool_categories.clear()
            self._short_name_owners.clear()
            self.ambiguous_short_names.clear()

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
