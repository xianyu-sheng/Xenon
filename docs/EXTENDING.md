# 扩展 Xenon

> 中文 | [English](EXTENDING.en.md)

Xenon 提供五条注册式扩展路径，无需修改核心代码。

## 1. 注册 MCP 服务器（终端命令）

```bash
# 文件系统 MCP 服务器
xenon> /mcp add fs npx -y @modelcontextprotocol/server-filesystem .
✅ MCP 服务器 'fs' 已连接  发现 10 个工具

# HTTP POST JSON-RPC 原型（端点须直接返回 JSON 响应）
xenon> /mcp add web http://localhost:3000/mcp

# 查看已连接的服务器
xenon> /mcp list
```

SSE 传输现在是真的 HTTP+SSE（2024-11-05 那套）：客户端先 `GET` 服务器地址
并保持流打开，收到 `event: endpoint` 后才把请求 `POST` 到该会话专属地址，
响应按 JSON-RPC `id` 在流上关联（并发请求不会串台），断线会重连一次，重连
失败则 `fail-closed` 报错而不是悬挂；重复事件按 `id:` 去重。`/mcp list` 会
如实显示 `sse: 已握手 endpoint=…` / `未连接（原因）`。

尚未实现的是 **Streamable HTTP（2025-03-26）**——那套用单端点 POST +
可选 SSE 升级，语义不同。若服务器只支持 Streamable HTTP，请勿填入其地址。
配置与连接验证的区别见 [MCP 传输边界](INTEGRATIONS.md#mcp-传输边界)。

注意：当前 REPL 的 `/mcp add` 在初始化或工具发现失败时，仍可能显示
“已连接，发现 0 个工具”；这条显示也不能作为握手成功的证明，见
[#103](https://github.com/xianyu-sheng/Xenon/issues/103)。

出处：`xenon/repl/command_groups/resources.py`、`xenon/mcp/registry.py`

## 2. 注册 Agent Skill（终端命令）

```bash
# 交互式创建
xenon> /skill create

# 从 GitHub 导入
xenon> /skill import https://github.com/user/repo/tree/main/skills/my-skill

# 查看已安装技能
xenon> /skill list
```

Skill 存储在 `.xenon/skills/<name>/SKILL.md`，启动时自动加载。
出处：`xenon/repl/skill_manager.py`

## 3. 注册工具处理器（Python API）

```python
from xenon.nodes.tool_registry import register_tool_handler


def my_search_handler(node, context):
    query = getattr(node, "search_pattern", "")
    # 执行搜索逻辑
    return {
        "action_type": "my_search",
        "success": True,
        "content": f"搜索结果: {query}",
    }


register_tool_handler(
    "my_search",
    my_search_handler,
    description="搜索我的知识库",
)
```

注册后工具自动出现在模型可见的工具列表中。
出处：`xenon/nodes/tool_registry.py`

## 4. 新增一个模式 / loop profile（Python API）

引擎类已经删除：现在只有一个 `LoopRuntime`，模式只是它的**一张阶段参数表**。
在 `xenon/modes.py` 里加一条 `ModeSpec`，`/mode`、setup wizard、`--mode`
和路由会同时看到它：

```python
from xenon.kernel.strategies import LoopProfile
from xenon.modes import MODE_CATALOG, ModeSpec

MODE_CATALOG["review-only"] = ModeSpec(
    name="review-only",
    description="只读审查：先计划，再逐步执行",
    loop=LoopProfile(plan=True, step_scope=True, step_calls=4),
)
```

- `loop` 声明跑哪些阶段（plan / step / reflect）以及每步预算；
- `loop=None` 的模式内核不接受（fail closed）；
- 只是别名时设 `alias_of`，不要复制参数（`react` 就是这样指向 `direct` 的）；
- 「再加一个执行器」不是扩展点——那会把单循环重新分叉成多引擎。

出处：`xenon/modes.py`（`MODE_CATALOG`）、`xenon/kernel/strategies/__init__.py`（`LoopProfile`）

## 5. 注册终端命令（Python API）

```python
from xenon.repl.command_registry import command_handler, register_command

register_command(
    "/myfeature",
    "我的新功能",
    "/myfeature [args] - 做某件事",
)


@command_handler("/myfeature")
def _cmd_myfeature(*, args: str, session_state: dict, **kwargs):
    # 命令逻辑
    return f"处理完成: {args}"
```

重启后 `/help` 自动列出新命令。
出处：`xenon/repl/command_registry.py`
