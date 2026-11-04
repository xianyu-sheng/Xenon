# 机制审计器：只认账本证据（#421）

## 为什么有这个文件

这个项目反复出现同一个失败模式：**代码改了、测试绿了、于是宣布"完成"**，
但真实任务里那条机制根本没跑（V1/V2 就是这样被发现只跑了一级的）。

所以新增 `scripts/audit_mechanisms.py`：给它一个真实账本，它逐项回答
**"这条机制在本次运行里留下了痕迹吗"**，而不是读代码猜。

```bash
python scripts/audit_mechanisms.py ~/.xenon/ledger/task-*.jsonl
```

三种状态，刻意区分：

| 状态 | 含义 |
|---|---|
| `OK` | 机制在自己的事件里出现了，且内容符合预期 |
| `MISS` | **该跑却没跑**（例如有失败调用却没有一条教学记录） |
| `n/a` | 本次任务没有触发它（没有审批、没有循环、未到交付）——这不是缺陷，但也不是证据 |

## 审计项与实测结果

| 机制 | 检查的事件 | 真实硬任务（`task-bfdac14ccc21`） |
|---|---|---|
| V0 动作级校验 | `step_verdict[stage=v0]` | OK 6 条 |
| 三级齐备 V0/V1/V2 | `step_verdict` + `audit_reported` | OK `v0, v1, v2` |
| claim → fact 绑定 | `claim_proposed` / `claim_resolved` | OK 3 提出 / 3 绑定 |
| 交付（finalizer） | `finalize_started` | OK `verified_paths=[calc.py, test_calc.py]`，`commit=None`（无提交仓库） |
| 循环检测 + 收尾 | `loop_detected` / `control_decision` | n/a（本次无循环） |
| **失败即教学** | `control_decision[decision=teach_next_step]` | OK（另一次运行：1 次失败 / 1 条教学） |
| 自我进化 | `lesson_proposed` | n/a（本次无 lesson） |
| 审批边界 | `user_approval` | n/a（本次无审批） |
| 用量记账 | `model_usage` | OK 8 次调用，末次 prompt=5753 |

## 审计器抓到的第一个真问题

**"失败即教学"此前根本不可观测**：建议只写进内存里的 `_loop_notes`，
账本里一个字都没有。也就是说它可以"实现完成"而无人能验证——正是这个项目
要消灭的那类断言。

修复：产生建议时同时写
`control_decision {decision: "teach_next_step", reason: <建议>, scope: [tool]}`。
实测（`task-568006afb03f`）：`1 次失败 / 1 条已记账的教学提示`。

## 用法约定

- **CI 不跑审计器**：它需要一个真实账本，而账本来自真实运行；
- 想复核某个机制时，跑一次真实任务，再用它扫账本；
- `MISS` 必须开 issue，或者改机制，或者改审计项（三者选一，不许无视）。
