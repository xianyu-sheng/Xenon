# Extending Xenon

> English | [中文版](EXTENDING.md)
>
> *Note: This translation may lag behind the source documentation; the [Chinese version](EXTENDING.md) is the authoritative source of truth. (Synchronized with commit `730d198`).*

Xenon provides five registry-based extension paths without requiring modifications to the core codebase.

## 1. Registering MCP Servers (Terminal Commands)

```bash
# Filesystem MCP server
xenon> /mcp add fs npx -y @modelcontextprotocol/server-filesystem .
✅ MCP server 'fs' connected. 10 tools discovered.

# HTTP POST JSON-RPC prototype (the endpoint must return direct JSON responses)
xenon> /mcp add web http://localhost:3000/mcp

# List connected servers
xenon> /mcp list
```

The HTTP URL above is merely a placeholder example; you must replace it with the actual POST JSON-RPC endpoint provided by your server. Currently, `SSETransport` does not yet implement MCP SSE `endpoint` discovery or streaming response ID correlation, nor does it represent full support for Streamable HTTP; do not directly provide a `/sse` address that only handles SSE handshakes. For details on configuration versus connection validation, see [MCP Transport Boundaries](INTEGRATIONS.md#mcp-传输边界).

Note: In the current REPL, `/mcp add` may still display "connected, 0 tools discovered" when initialization or tool discovery fails; this message should not be taken as proof of a successful handshake (see [#103](https://github.com/xianyu-sheng/Xenon/issues/103)).

Source: `xenon/repl/command_groups/resources.py`, `xenon/mcp/registry.py`

## 2. Registering Agent Skills (Terminal Commands)

```bash
# Interactive creation
xenon> /skill create

# Import from GitHub
xenon> /skill import https://github.com/user/repo/tree/main/skills/my-skill

# List installed skills
xenon> /skill list
```

Skills are stored in `.xenon/skills/<name>/SKILL.md` and are loaded automatically upon startup.  
Source: `xenon/repl/skill_manager.py`

## 3. Registering Tool Handlers (Python API)

```python
from xenon.nodes.tool_registry import register_tool_handler


def my_search_handler(node, context):
    query = getattr(node, "search_pattern", "")
    # Execute search logic
    return {
        "action_type": "my_search",
        "success": True,
        "content": f"Search results: {query}",
    }


register_tool_handler(
    "my_search",
    my_search_handler,
    description="Search my knowledge base",
)
```

Once registered, the tool automatically appears in the tool list visible to the model.  
Source: `xenon/nodes/tool_registry.py`

## 4. Registering Reasoning Engines (Python API)

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
    description="My custom reasoning strategy",
    mode_line="· MyCustom executing",
    result_title="MyCustom Result",
)
```

Register once, and the `/mode` list, REPL router, and setup wizard will all automatically recognize it.  
Source: `xenon/modes.py` (`MODE_CATALOG` + `LoopProfile`)

## 5. Registering Terminal Commands (Python API)

```python
from xenon.repl.command_registry import command_handler, register_command

register_command(
    "/myfeature",
    "My new feature",
    "/myfeature [args] - Do something",
)


@command_handler("/myfeature")
def _cmd_myfeature(*, args: str, session_state: dict, **kwargs):
    # Command logic
    return f"Processing complete: {args}"
```

After restarting, `/help` will automatically list the new command.  
Source: `xenon/repl/command_registry.py`
