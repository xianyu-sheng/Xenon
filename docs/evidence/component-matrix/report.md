# 组件真实任务矩阵

- 时间：2026-11-16 14:17:10
- 模型：`deepseek/deepseek-v4-flash`
- 入口：`node frontend/dist/cli.js`（真实 ConPTY，auto-edit）
- 结果：**54/54 PASS**
- 用时合计：213.8s · token 合计：88,695

## 组件用时与证据

| 场景 | 用时 | tokens | 工具 | 证据 | accepted | 账本 |
|---|---|---|---|---|---|---|
| read | 17.8s | 17,072 | read | partial | True | repl-43d508b0902d.jsonl |
| write | 23.2s | 18,238 | apply_patch, test, finalizer | verified | True | repl-9521e16f2ccb.jsonl |
| bugfix | 35.7s | 19,733 | read, read, read, apply_patch, test, finalizer | verified | True | repl-87be4412e3e2.jsonl |
| commands | 29.6s | 0 | - | unverified | None | chat-fdb7746f33c8.jsonl |
| plan | 21.2s | 8,748 | apply_patch, read, finalizer | verified | True | repl-3d83892b488b.jsonl |
| session | 30.6s | 0 | - | unverified | None | chat-ffb991971c4c.jsonl |
| mcp | 31.2s | 16,680 | train:query_trains | partial | True | repl-19539942b0ca.jsonl |
| cancel | 24.6s | 8,224 | - | - | None | repl-47d2335959c2.jsonl |

## 检查项

| 检查项 | 结果 | 说明 |
|---|---|---|
| read: turn finished | PASS |  |
| read: no approval needed | PASS | 0 |
| read: answer quotes the file | PASS |  |
| read: read receipt exists | PASS |  |
| read: evidence reported | PASS |  |
| read: no unmet debt | PASS |  |
| write: turn finished | PASS |  |
| write: an approval was exercised | PASS | 1 |
| write: hello.py written with print(246) | PASS |  |
| write: shell receipt carries the output | PASS | 246 |
| write: no unmet debt | PASS |  |
| bugfix: turn finished | PASS |  |
| bugfix: add returns the sum | PASS | def add(a, b): return a + b |
| bugfix: test file untouched | PASS |  |
| bugfix: pytest passes | PASS | [100%] 1 passed in 0.01s |
| bugfix: the model ran the tests | PASS | . |
| bugfix: no unmet debt | PASS |  |
| commands: /cost answers | PASS |  |
| commands: /cache answers | PASS |  |
| commands: /context answers | PASS |  |
| commands: /mode answers | PASS |  |
| commands: /tools answers | PASS |  |
| commands: /memory lessons answers | PASS |  |
| commands: /permissions answers | PASS |  |
| commands: no unknown command | PASS |  |
| commands: exit prints the saving report | PASS |  |
| plan: plan mode opened | PASS |  |
| plan: plan-only turn finished | PASS |  |
| plan: plan-only asked no write approval | PASS | 0 |
| plan: the write was deferred | PASS |  |
| plan: /plan status reports a pending plan | PASS |  |
| plan: /plan approve ran the plan | PASS |  |
| plan: output exists with the token | PASS |  |
| plan: plan events recorded | PASS | plan_accepted,plan_approved,plan_proposed |
| session: saved | PASS |  |
| session: --resume reports the restored session | PASS |  |
| session: the saved conversation is rendered | PASS |  |
| session: answer comes from the restored history | PASS |  |
| session: resumed turn finished | PASS |  |
| mcp: server added | PASS |  |
| mcp: tool discovered | PASS |  |
| mcp: turn finished | PASS |  |
| mcp: the tool was called | PASS | ['train:query_trains'] |
| mcp: answer carries real train data | PASS |  |
| mcp: tools survive a restart | PASS |  |
| cancel: approval appeared and was approved | PASS |  |
| cancel: Ctrl+C reports a cancellation | PASS | 取消 |
| cancel: the REPL still answers commands | PASS |  |
| dashboard: startup line shown | PASS |  |
| dashboard: URL and token parsed | PASS |  |
| dashboard: no token is rejected | PASS | 401 |
| dashboard: /api/state renders with the token | PASS |  |
| dashboard: snapshot has state | PASS |  |
| dashboard: dashboard.snapshot query answers | PASS |  |
