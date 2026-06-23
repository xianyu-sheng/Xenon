# OmniAgent Eval Report

- Mode: `mock`
- Model: `mock-agent`
- Run date: `2026-06-23 18:11:47 UTC`
- Tasks: 20
- Success Rate: 100.0%
- Average Tokens: 112.0
- Tool Calls: 36
- Tool Failures: 0

| Task | Category | Success | Tokens | Tool Calls | Tool Failures | Notes |
| --- | --- | --- | ---: | ---: | ---: | --- |
| `edit-python-function` | file_edit | yes | 117 | 2 | 0 | Mock agent used expected tools deterministically. |
| `add-unit-test` | file_edit | yes | 115 | 2 | 0 | Mock agent used expected tools deterministically. |
| `refactor-duplicate-code` | file_edit | yes | 125 | 3 | 0 | Mock agent used expected tools deterministically. |
| `update-readme-command` | file_edit | yes | 113 | 2 | 0 | Mock agent used expected tools deterministically. |
| `code-search-entrypoint` | code_search | yes | 114 | 2 | 0 | Mock agent used expected tools deterministically. |
| `code-search-tool-node` | code_search | yes | 112 | 2 | 0 | Mock agent used expected tools deterministically. |
| `code-search-model-router` | code_search | yes | 112 | 2 | 0 | Mock agent used expected tools deterministically. |
| `code-search-context-injection` | code_search | yes | 114 | 2 | 0 | Mock agent used expected tools deterministically. |
| `run-focused-tests` | tool_call | yes | 100 | 1 | 0 | Mock agent used expected tools deterministically. |
| `inspect-git-status` | tool_call | yes | 105 | 1 | 0 | Mock agent used expected tools deterministically. |
| `generate-diff-preview` | tool_call | yes | 102 | 1 | 0 | Mock agent used expected tools deterministically. |
| `call-weather-tool` | tool_call | yes | 103 | 1 | 0 | Mock agent used expected tools deterministically. |
| `remember-user-preference` | context_memory | yes | 115 | 2 | 0 | Mock agent used expected tools deterministically. |
| `use-project-rules` | context_memory | yes | 112 | 2 | 0 | Mock agent used expected tools deterministically. |
| `load-saved-session` | context_memory | yes | 102 | 1 | 0 | Mock agent used expected tools deterministically. |
| `compact-long-context` | context_memory | yes | 112 | 2 | 0 | Mock agent used expected tools deterministically. |
| `revise-after-test-failure` | multi_turn_revision | yes | 130 | 3 | 0 | Mock agent used expected tools deterministically. |
| `revise-after-review` | multi_turn_revision | yes | 112 | 2 | 0 | Mock agent used expected tools deterministically. |
| `handle-missing-api-key` | multi_turn_revision | yes | 106 | 1 | 0 | Mock agent used expected tools deterministically. |
| `mcp-tool-flow` | multi_turn_revision | yes | 118 | 2 | 0 | Mock agent used expected tools deterministically. |
