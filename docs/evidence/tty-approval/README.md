# Real TTY approval transcript — kernel MCP

Date: 2026-10-27  
Runtime: `XENON_RUNTIME=kernel` via a real ConPTY (`pywinpty`)  
Model: `deepseek/deepseek-v4-pro`  
MCP: configured `github-mcp-server`  
Task: read-only GitHub repository search

## What this proves

The user saw the actual `需要授权` panel and entered `y`; this was not a
`_confirm_tool` override. The kernel then recorded `USER_APPROVAL`, executed
`mcp_call`, and returned the real MCP result.

Transcript excerpt:

```text
Require approval panel:
  模型请求执行 mcp_call，超出工作区或属于命令执行。
  原因：风险等级 CRITICAL
选择 [y/n/a/q] (n): y
· 已授权本次操作

🧠 Runtime: kernel (deepseek/deepseek-v4-pro)
  💭 kernel · 2 steps · 1 tools · verified

● Kernel
  仓库全名（full_name）：xianyu-sheng/Xenon
  星标数（stargazers_count）：56
```

## Result

```json
{
  "accepted": true,
  "answer": "已通过 GitHub MCP 工具 search_repositories 实时查询，结果如下：\n\n- **仓库全名（full_name）**：xianyu-sheng/Xenon\n- **星标数（stargazers_count）**：56\n\n其他实时信息（供参考）：\n- 语言：Python\n- Forks：12，Open Issues：44\n- 项目描述：Verifiable AI coding agent harness（可验证的 AI 编码代理）\n- 地址：https://github.com/xianyu-sheng/Xenon\n- 是否为 fork：否；是否私有：否\n\n本轮仅为只读查询，未做任何修改。",
  "claims": [],
  "debt": [],
  "evidence_status": "verified",
  "finalize_sha": null,
  "metrics": {
    "events": 24.0,
    "repair_attempts": 0.0,
    "replans": 0.0,
    "steps": 2.0,
    "tool_calls": 1.0
  },
  "phase": "finalized",
  "resolutions": [],
  "schema_version": "kernel/v1",
  "task_id": "repl-c5cabd1812c0",
  "verdicts": [
    {
      "fact_ids": [],
      "obligation_id": "task",
      "reason": "",
      "stage": "v2",
      "status": "satisfied"
    }
  ]
}
```

Metrics: steps=2.0, tools=1.0,
claims=0, evidence=verified.

## Ledger

- `policy_decision: require_approval` scope `remote, github:search_repositories`
- `user_approval: approved=true`
- `tool_receipt: mcp_call` with NetworkFact content
- `control_decision: deliver`
- `finalized: evidence_status=verified`

`ledger.jsonl` + anchor verify locally (`valid=True`).

## Notes

- This TTY run used a real terminal, but the workspace was not a git repo, so
  `finalize_sha` is null; it was read-only.
- The non-TTY path remains fail-closed and was not changed by this evidence run.
- MCP content is untrusted; it is bounded in the ledger/view.
