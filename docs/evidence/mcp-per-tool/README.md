# Per-tool MCP ToolSpec — real model and real GitHub MCP

Date: 2026-10-27  
Model: `deepseek/deepseek-v4-pro`  
MCP server: configured `github-mcp-server`  
Task: read-only repository search for `xianyu-sheng/Xenon`

## What this proves

The REPL kernel adapter discovered 46 canonical `github:*` tools and built one
`ToolSpec` per tool. The model selected `github:search_repositories`; the
catalog bound that exact spec; the gateway translated it to the internal
`mcp_call` executor while keeping the real tool name in the receipt.

```text
approval network requires approval
accepted True evidence=verified debt=0 claims=0
tool github:search_repositories
url mcp://github:search_repositories
```

Answer returned from the real MCP result:

```text
full_name = "xianyu-sheng/Xenon"
stargazers_count = 56
```

## Notes

- The catalog used 46 full `server:tool` names plus the legacy generic
  `mcp_call` fallback only when no per-tool specs exist.
- The approval boundary is still the MCP boundary; the server process is not
  claimed to be sandboxed by Xenon.
- The receipt and `NetworkFact` retain the real tool name; the executor entry
  remains `mcp_call` internally.
