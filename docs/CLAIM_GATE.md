# Claim Gate：执行与证据的确定性结合

## 结论

Xenon 的执行层现在不只“做完再说完成”，而是在步骤边界把 Agent 的预期
（claim）与真实证据（evidence）做确定性比对：

```text
计划契约
  → 生成结构化 claim
  → 工具真实执行产生 observation
  → Gate 比对 TaskState / tracker / worktree / 测试结果
  → rejected: 同一步骤内定向修复
  → 任务结束：criterion-level goal audit
  → 未通过条件定向重做
  → delivery gate 最终兜底
```

Gate 不调用 LLM，不产生额外模型费用。Claim 不是证据，不能直接让任务完成。

## Claim 契约

步骤开始时先写入 `status=pending` 的 claim 事件，明确本步预期；执行完成后
同一 claim id 再由 Gate 更新为 `verified` / `rejected` / `unverifiable`。

`xenon/engine/claim_gate.py` 定义以下确定性 claim：

| kind | 真实证据 |
|---|---|
| `file_written` | 本步骤有匹配的成功写工具调用，且目标文件存在 |
| `source_write` | 本步骤有成功的非测试源码写入 |
| `command_passed` | 本步骤运行了匹配的验证命令，exit=0 且没有失败标记 |
| `commit_created` | TaskState 有真实 commit，且不是步骤开始前的旧 commit |
| `worktree_clean` | 新 git status 探测干净（忽略生成缓存） |
| `test_artifact_nonempty` | 测试文件由本步骤写入且内容非空 |
| `pr_created` | TaskState 有真实 PR URL |

每个 claim 都带步骤开始时的 baseline（调用游标、旧 commit），因此旧步骤的
工作不能替新步骤“交差”。

## 事件与投影

Claim 结果写入唯一的 `EvidenceLedger`：

```text
kind: claim
source: gate
payload:
  claim_id / step_id / kind / subject / expected
  status: verified | rejected | unverifiable
  actual / reason / evidence_ids
  plan_version / attempt / task_id
```

`TaskStateProjection` 将 claim 结果投影到 `TaskState.claims`。旧计划版本的
claim 保留为审计历史，但不会阻塞新计划的完成判断。`TaskState.maybe_complete`
只接受当前计划中没有 rejected claim、且当前 criterion 没有 rejected 的状态。

## 并行 DAG 的串行修复屏障

并行 worker 不直接修改共享 TaskState，也不直接并发执行修复：

| 行为 | 串行 / DAG 串行 | DAG 并行 |
|---|---|---|
| Claim 校验 | 步骤完成后立即 | worker 只校验，主线程记录 |
| 失败修复 | 同一步骤内 | 波次汇合后串行修复 |
| 依赖步骤 | 修复成功则继续 | 修复成功则依赖继续运行 |
| 证据 | 父 ledger | worker 私有 ledger，屏障处 replay 到父 ledger |
| 修复失败 | 重规划/兜底 | 保持原 skip-cascade / 有界重规划 |

这样不会让两个 worker 同时写同一工作区，同时保持 serial 和 parallel 的
failure → repair → verify 契约一致。

## 步骤内修复

Plan-Execute 在每个步骤完成后立即运行 claim gate：

- 初始执行成功但 claim rejected → 不进入下一步，直接把真实差异交给
  `_repair_failed_step`；
- 修复轮成功后再检查 claim；仍失败则继续同一步骤的有限修复；
- 同一错误重复出现时短路，不无限重试；
- 串行、DAG 串行和 DAG 并行 worker 都覆盖；并行 worker 的 claim 结果和
  工具 observation 在主线程波次屏障记录/replay，避免并发修改 TaskState。

## 最终 Goal Audit

`run_with_delivery` 在 delivery 维度没有缺口后，运行
`claim_gate.audit_goal()`，按 criterion 检查：

- 非空测试文件
- 真实验证通过且不早于最后一次源码修改
- 非测试源码变更
- 真实 commit
- 干净 worktree
- 计划中的 write / verify / commit / clean 条件
- PR（如要求）
- 用户输入中的验收条件（例如“CI 通过”“测试全部通过”）；这类条件从旧
  `task_verifier.extract_acceptance_criteria` 对齐进入 goal audit，不再只做发布提醒

失败的 criterion 会生成带 `claim_id`、预期、实际、证据的定向 continuation；
下一轮只修这些条件，不重做已完成工作。仍不通过时保留原交付警告，并继续由
`missing_delivery()` 的 delivery gate 兜底。

`unverifiable` 会被记录，但不会单独触发无限修复；最终完成仍要求没有
rejected criterion。

## 验证

- `tests/test_claim_gate.py`：baseline 隔离、pending/verified claim、文件/命令/
  commit/空测试、用户验收条件、goal audit、投影回放、步骤内修复、delivery
  定向修复。
- `tests/test_e2_plan_dag.py`：并行失败修复后依赖继续运行；修复失败保留
  skip-cascade；worker observation 在屏障处进入父 ledger。
- `tests/test_task_state.py`：旧计划 claim 不阻塞新计划；skipped 侦察/执行
  步骤在有已验证目标条件时不掩盖真实交付。
- 全量测试通过后提交；真实基准用例需单独记录，不把单轮成绩外推。
