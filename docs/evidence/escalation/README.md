# 回合内能力升级（#392）：内核侧完成，TTY 侧被新 bug 阻断

## 已完成（内核 + 单测，7/7）

`request_escalation` 是模型对「本轮被拒的那次调用」的**正式请求**：

| 验收 | 状态 |
|---|---|
| 批准 → 下一次调用真正执行（网关收到调用） | ✅ |
| 拒绝 → `blocked`，理由含 capability 与用户回答 | ✅ |
| 无审批器（非 TTY）→ 立即 fail-closed，理由可操作（`--permission bypass` / TTY） | ✅ |
| push / destructive → **不弹面板**直接拒 | ✅ |
| 计划模式 → 循环层先拒（`plan-only task requires a plan proposal`），不会询问 | ✅ |
| grant 一次性：下一次同调用被拒，且不偷偷重问 | ✅ |

安全语义未动：`allow_approval` 仍控制策略自动询问；升级是**人**的显式决定，
一次性 grant、入 ledger（`GRANT_ADDED` / `USER_APPROVAL{escalation:true}`）、
调用后由 `_call_tool` 的 finally 撤销。

## 真实 TTY：模型行为正确，但审批面板收不到按键

真实 ConPTY + deepseek-v4-flash，prompt `查询一下北京天气，生成天气文档保存下来`：

1. 模型**正确发出 `request_escalation`**（不再编造边界、不再自我设限）；
2. 审批面板正常渲染，含真实命令：
   ```text
   需要授权 — 模型请求执行 bash
   命令: Invoke-RestMethod -Uri 'https://wttr.in/Beijing?format=j1' | Out-File ...
   y=只这次 a=本会话都允许 A=永远允许 s=只允许这条命令 n=拒绝 q=取消任务
   ```
3. **答 `y` 或 `n` 都不生效**：面板停在原地，ledger 停在 `state_transition(action proposed)`，
   没有 `USER_APPROVAL`；Ctrl-C 也不被处理；状态栏会重绘（说明 pt 会话仍在跑）。

这是 pre-existing 的路径（`_ask_approval_choice` 在 kernel 轮次内复用 `_pt_session`），
#392 只是第一个真正走到它的功能。已开 #394 跟踪，含复现与已排除的假设。
