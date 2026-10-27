# MCP tool selection — 46 discovered, 16 injected

Date: 2026-10-27  
Model: `deepseek/deepseek-v4-pro`  
MCP: configured `github-mcp-server`  
Task: read-only repository search for `xianyu-sheng/Xenon`

## What this proves

The registry discovered 46 canonical GitHub tools. The deterministic selector
injected 16, and the model selected `github:search_repositories`.

```text
mcp_tools: github:search_repositories, github:create_repository,
github:fork_repository, github:list_repository_collaborators,
github:search_commits, github:search_code, github:search_pull_requests,
github:issue_read, github:pull_request_read, github:search_users,
github:search_issues, github:add_issue_comment, github:get_release_by_tag,
github:list_pull_requests, github:create_or_update_file, github:list_commits
```

Result:

```text
accepted True evidence=verified debt=0
tool github:search_repositories
url mcp://github:search_repositories
full_name = "xianyu-sheng/Xenon"
stargazers_count = 56
```

`Ledger.verify()` is valid with the copied anchor.

## Notes

- The selector is deterministic: short tool-name tokens are weighted above
  descriptions; ties sort by canonical `server:tool` name.
- Common English stopwords and a small Chinese hint map (搜索/查询/仓库/问题/...)
  are used only to build the objective token set.
- `XENON_MCP_TOOLS` overrides auto-selection; `XENON_MCP_TOOL_LIMIT` caps it.
- Unselected tools are not injected and remain unresolved; this is a budget
  choice, not a sandbox.
