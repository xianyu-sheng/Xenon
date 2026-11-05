# 三个机制的触发证据（#422）

审计器对普通任务会给出大量 `n/a`——诚实，但不能当证据。这三个**确定性探针**
（无模型、无网络）专门触发它们，各写一份真实账本。

```bash
python docs/evidence/triggered-mechanisms/probe.py
python scripts/audit_mechanisms.py docs/evidence/triggered-mechanisms/probe-*.jsonl
```

## 1. 失败 → 教学（`probe-failure.jsonl`）

第一次工具调用失败，运行**没有停止**，下一份视图拿到建议，账本留痕：

```text
tool_receipt      status=failed  error="[exec_read] read_file 需要 file_path"
control_decision  {decision: "teach_next_step", reason: "The call is missing a required parameter…"}
step_verdict      v0 …
audit_reported    …
```

审计：

```text
OK   失败即教学（可观测）    1 次失败 / 1 条已记账的教学提示
```

> 这条检查在 #421 之前是 `MISS`：建议只写进内存，账本里看不到，等于不可验证。

## 2. 循环 → 收尾（`probe-loop.jsonl`）

同一个调用连发 6 次：先教一次，第 5 次关闭工具（`force_finish`），第 6 次用掉
一次性额度，随后正常收尾交付。

```text
loop_detected     ×3
control_decision  {decision: "force_finish", receipts: 4}
control_decision  {decision: "force_finish_allow_once", remaining: 0}
delivered / finalized
```

审计：

```text
OK   循环检测 + 收尾     3 次检测 / 1 次收尾
OK   自我进化（lesson）  3 条 lesson 提案
```

## 3. 审批 → 执行（`probe-approval.jsonl`）

模型请求越权（shell），策略要求审批，人类批准，**授权被创建并被真正使用**：

```text
user_approval     {approved: true, escalation: true, scope: ["shell"]}
policy_decision   {kind: "allow", reason: "scoped grant: echo hi", scope: ["shell", "echo hi"]}
tool_receipt      shell …（真的执行了）
finalized
```

审计：

```text
OK   审批边界     1 次审批，最后 approved=True
```

> 这条链路是 0.9.8 的核心修复：此前「问了也白问」——批准后工具自己的围栏又拒一次。

## 审计器的两处语义修正

- **无写入动作时 v1 不适用**：`direct`/`react` 里没有编辑就没有步骤级期望，
  报 `n/a` 而不是 `MISS`（此前会把正常运行标成缺项）；
- **确定性探针没有模型调用**：`TaskSpec.constraints=("probe:scripted",)` 让用量检查
  报 `n/a`，而不是把一个不含模型的账本判成「无法解释成本」。

## 已知边界

- 探针用脚本化策略，验证的是**机制接线**，不是模型行为；
- 真实模型下的"声称但没做"目前只有脚本化评测（`evals/false_completion`）与两次真实
  dogfood（`#418` 就是真实任务抓到的）。
