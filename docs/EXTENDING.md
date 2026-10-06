# 扩展 Xenon

Xenon 提供五条注册式扩展路径，无需修改核心代码。

## 1. 注册 MCP 服务器（终端命令）

```bash
# 文件系统 MCP 服务器
xenon> /mcp add fs npx -y @modelcontextprotocol/server-filesystem .
✅ MCP 服务器 'fs' 已连接  发现 10 个工具

# HTTP（SSE）传输
xenon> /mcp add web http://localhost:3000/sse

# 查看已连接的服务器
xenon> /mcp list
```

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

## 4. 注册推理引擎（Python API）

```python
from xenon.engine.base import BaseEngine
from xenon.engine.registry import register_engine


class MyCustomEngine(BaseEngine):
    def run(self, user_input, context=None) -> str:
        self._begin_run()
        messages = self._history_messages(user_input, limit=20)
        return self._call_llm(messages, phase="my_custom")


register_engine(
    "my-custom",
    factory=lambda **kw: MyCustomEngine(**kw),
    description="我的自定义推理策略",
    mode_line="· MyCustom 执行中",
    result_title="MyCustom 结果",
)
```

注册一次，`/mode` 列表、REPL 路由、setup wizard 全部自动识别。
出处：`xenon/engine/registry.py`

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
