# 内核轮次内的审批面板（#395）：根因与修复

## 现象

真实 ConPTY：kernel 轮次内的审批面板会渲染，但**任何应答都不生效**——
`y`、`n`、回车都一样，Ctrl-C 也不被处理，ledger 停在 `action proposed`。

## 根因（由临时追踪直接给出）

```
runtime.escalation.ask
confirm.enter tool=bash
pt.prompt.enter
pt.prompt.return answer='ny'      ← 按的是 y，拿到 "ny"
pt.prompt.enter                   ← 无效 → 重新提示（看起来像卡死）
```

`prompt_toolkit` 的 `session.prompt(question, default="n")` 里，**`default` 会预填
编辑缓冲区**，它不是占位符。于是：

| 用户按键 | 缓冲区 | 提交值 | 结果 |
|---|---|---|---|
| `y` + Enter | `n` + `y` | `"ny"` | 非法 → 重新提示 |
| Enter | `n` + 空 | `"nn"` → 实际 `"n"` | 拒绝（用户以为"没反应"） |

**任何输入都出不去**：合法答案只可能是单选键，而缓冲区永远先有一个 `n`。
一句 `(n)` 提示已经写在问题文本里，`default` 纯属多余且有害。

## 修复

```python
# before: answer = session.prompt(question, default=default)
answer = session.prompt(question).strip().lower()
if answer == "":
    answer = default          # 空输入 = 默认，语义不变
```

## 真实验证（真实 ConPTY + deepseek-v4-flash）

| 应答 | 修复前 | 修复后 |
|---|---|---|
| `y` | 永久挂起 | 6.7s 完成，ledger 出现 `user_approval{approved:true, escalation:true}` → `policy_decision{allow, "scoped grant: curl …"}` → `to: tool_call` |
| `n` | 按 Enter 无效 | **2.5s**，`blocked`：`escalation denied (shell): 用户拒绝` |
| `q` | 无效 | **2.6s**，`blocked`：`escalation denied (shell): 用户取消任务` |
| `Ctrl-C` | 无人处理 | **立即**，`blocked`：`用户取消任务` |

transcript 见本目录：`answer-y.txt` / `answer-n.txt` / `answer-q.txt` / `ctrl-c.txt`。

## 顺带修掉的第二个真 bug：授权范围没有告诉模型

`y` 的第一次真实运行里，审批通过后模型**改了命令**
（`$ProgressPreference='SilentlyContinue'; Invoke-RestMethod …`），
grant 是按原命令绑定的 → `policy_decision{veto, "shell requires a grant"}` → 任务 blocked。
边界行为是对的，缺的是告知：现在授权成功后写一条确定性 note：

```
Escalation approved for exactly this call: shell `curl -s "https://wttr.in/Beijing?format=j1"`.
Propose that exact call next; any other command is refused.
```

重跑后模型发的就是**被授权的那条命令**，grant 命中并真正执行。

## 回归测试

`tests/test_permissions_command.py`：
- pt 路径**不得**传 `default=`（预填即 bug）；
- 空输入 → 默认；
- 非法答案（`"ny"`）→ 重新提示，不吞掉。

## 已知遗留（不属于本 issue）

被授权的 shell 命令本身可能长时间不返回（本机网络访问 `wttr.in` 就是），
目前**没有 per-call 超时**；那是另一个问题，已记录在 #368 的兜底讨论里。
