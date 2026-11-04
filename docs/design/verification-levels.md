# 三级校验：实际跑在哪（审计 #419 / 补完 #420）

> 审计方式：不看注释，看**真实复杂任务**（11 次 receipt，三处关联改造）产生的 verdict 事件。

## 修复后（#420）：三级都在跑，并且是同一形状的事件

`react` 模式、同一个硬任务（`calc.py` 三处改造，7 次 receipt）的真实账本：

```text
三级 verdict 数: {'v0': 6, 'v1': 2, 'v2': 3}
  v1 implicit-step-1 -> satisfied
  v1 implicit-step-2 -> satisfied
  v2 v2-file_changed-1 -> satisfied      (calc.py)
  v2 v2-file_changed-2 -> satisfied      (test_calc.py)
  v2 v2-test_passed-3 -> satisfied       (test_calc.py)
```

三条都是 `stage=v1/v2` 的 `step_verdict` 事件，`/evidence` 与回放都能直接数出来。
`intake_recorded` 里也能看到派生出来的 obligations。

## 还有过的一次假阴性（值得记）

第一版把 V1 建在 action 的 `targets` 上，但 `apply_patch` 的路径在**效果**里，`targets` 是空
→ 两条 `unverifiable`，直接把一次真的成功（7 passed）判成 blocked。
改成从 receipt 的 `FsWrite.paths` 派生后恢复正常。**校验层的假阴性比漏检更伤**：它会让
人误以为工具坏了。

## 修复前：三级只跑了一级

| 级别 | 代码位置 | 真实任务里跑了吗 | 说明 |
|---|---|---|---|
| **V0 动作级** | `VerificationController.reconcile()` | ✅ 每个 receipt 一条（实测 6–7 条） | 不变量：exit code、no-op write、target 匹配、commit sha、worktree delta |
| **V1 步骤级** | `VerificationController.close_step()` | ❌ 0 条（`direct`/`react` 模式） | 只在**计划步骤**上存在；真实计划每步 `expects: []` → 永不触发 |
| **V2 任务级** | `audit()` / `Auditor.audit()` | ⚠️ 部分 | finish claim 会解析（实测 3 条 `claim_resolved`），但**全局覆盖恒为空**：`obligations` 从未从用户任务派生 |

三条实测证据（同一份 `calc.py` 三处改造任务）：

```
react 模式：          stages = {'v0': 6}                 finalize: verified_paths=[calc.py, test_calc.py]
plan-execute 模式：   stages = {'v0': 5, 'v1': 6}        DEBT: file_changed test_calc.py — no changed file fact
```

第二行说明 plan 模式下 V1 一旦真的运行，**立刻抓到了真问题**（agent 说改了测试，账本里没有对应事实），
并按规则阻断交付。

## 本轮补的（#419）

**V1 从「这一步实际做了什么」派生期望**，不再等模型在计划里写 `expects`：

- `edit` 步骤 → `file_changed(target)`；
- `read` 步骤不派生（读没有承诺；没读到新东西只是重复，不是撒谎）；
- `verify` 步骤留给 finish claim（那里已经检查 test_passed）；
- 派生出的 obligation 走**同一个 resolver**，窗口起点用 `plan_since_seq`，
  不会被计划开始之前的写入满足。

单测：`tests/kernel/test_controller.py` 三条（派生、不派生、无变更事实 → VIOLATED）。

## 仍然缺的（下一步）

1. **V2 的全局覆盖是空的**：`TaskSpec.obligations` / `Auditor.audit(obligations=...)`
   依赖的 obligations 从未从用户请求派生。现在的"任务级"实际只有：
   - finish claim 的解析（模型自己说要交的东西）；
   - finalize 的 `declared writes ⊆ verified paths`（#418）。
   这两者都是"对得上"检查，但没有覆盖用户明确提出的**多条要求**（本次任务有三条）。
2. **`direct`/`react` 没有 step 概念**：V1 天然不适用。要么承认"无计划时步骤级 = 动作级"，
   要么让每个 tool call 成为一个 step。当前选择是前者，需要在文档里写明。
3. **V2 未落成 verdict 事件**：finalize 的判定在 `finalize_started` 里，
   而不是一条 `stage=v2` 的 verdict，所以 `/evidence` 里看不到三级齐备的样子。

## #420 做了什么

1. **V2 有了输入**：`xenon/harness/intake.py::derive_obligations()` 从任务文本确定性地派生
   `file_changed(路径)` 与 `test_passed(测试文件)`，写入 `TaskSpec.obligations`，
   并在 `intake_recorded` 里记账。
   - **不**把散文要求（"不要删测试"）伪造成覆盖：无法确定性绑定，伪造覆盖比没有更糟。
   - 忽略 README/LICENSE 这类文档；同一文件只计一次；上限 12 条。
2. **三级同形事件**：audit 的 V2 verdict 也写成 `STEP_VERDICT` 事件，`/evidence`、回放、
   审计脚本都不必再钻 `audit_reported` 的 payload。
3. **无计划的回合也有 V1**：`direct`/`react` 没有步骤，就把这一轮当作一个隐式步骤，
   从 receipt 的 `FsWrite.paths` 派生 `file_changed`，交付前结算。
4. **差异进入自我进化**：V2 verdict 为 VIOLATED 时调用 `_propose_lesson("claim_fact_mismatch")`
   ——"声称与实际不一致"正是可进化层该学的模式，而**不是**拦截。

## 原则（用户定的，代码里现在是这样）

- 校验只回答"对得上吗"，不判断"对不对"；
- 对不上 → 教学/补一轮/披露给人，**不阻断执行**；
- 交付不强制 commit（无提交仓库也可以交付），但必须列出 `verified_paths` 与缺口。
