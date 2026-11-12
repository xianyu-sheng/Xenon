# 自进化（self-evolution）切片 1+2：存储 + 事件 + 提取器

> **历史快照（2026-11-09）**：本目录记录切片 1+2 的验收。此后「教训即记忆」落地——
> `lessons.yaml` 与 `/lessons`、`PITFALLS.md` 均已删除，教训是 `MemoryKind.LESSON`
> 记忆记录（`/memory` 查看）。当前口径以 `docs/design/self-evolution.md` 为准；
> 本文件下方关于 `lessons.yaml` 的描述是当时的实现。

设计：`docs/design/self-evolution.md`（design-first）。本证据覆盖切片 1
（`LessonStore` + `LESSON_*` 事件 + 指纹/退休语义）和切片 2（提取器 +
runtime 挂接）。切片 3（应用器）和 4（产物：`/lessons` + PITFALLS.md）后续。

## 真实运行（deepseek/deepseek-v4-flash）

```text
task phase: blocked  accepted: False
denied approvals: 1; lesson proposals: 1
  preference | trigger=user_approval | fp=1dd826ba62…
    action: shell 调用被用户拒绝（user: 先别跑）——下次避免同类调用
fingerprint matches proposer: True
PASS: a denial proposes a preference lesson, deterministically
```

人类第一次拒绝审批 → runtime 在失败点写 `LESSON_PROPOSED`（确定性模板，零
LLM），指纹与 `lesson_fingerprint` 一致（同一失败必然同一指纹）。

## 实现（护栏在代码里的落点）

| 护栏 | 落点 |
|---|---|
| 1. 只从 harness 判定学 | `lesson_proposer.py`：只接受 `policy_decision(veto)` / `debt_opened` / `loop_detected` / `user_approval(denied)`；模板从 payload 生成，绝不使用模型 prose |
| 2. 教训可证伪 | `Lesson.fingerprint`（事件 kind + canonical payload 哈希）+ `recurrences` 计数器，`record_recurrence` ≥3 次 → `retired`；stale（30 天未应用）不匹配 |
| 3. 教训是可见产物 | `~/.xenon/lessons.yaml`（`LessonStore`）+ `LESSON_PROPOSED/STORED/APPLIED/RETIRED` 4 个事件 |

架构落点：**kernel 纯**（`lesson_fingerprint`/`Lesson`/`proposer` 无 I/O，
K005 守卫验证），**存储在外层**（`xenon/harness/lessons.py` 的 `LessonStore`
才碰文件）。runtime 只在失败点发 `LESSON_PROPOSED` 事件，不自己存储。

## 测试

`tests/test_lessons.py`（12：指纹确定性/区分、空 store、匹配/不匹配、文件往返、
畸形丢弃、损坏文件、退休/stale 不匹配、复发退休、应用计数）、
`tests/kernel/test_lesson_proposer.py`（10：VETO→pitfall、debt→pitfall、
loop→parameter、denied→preference、approved 无、未知事件无、指纹确定、runtime
debt/deny 挂接）。

全量：3799 passed, 97 skipped；ruff / 三重守卫通过。

## 下一步（切片 3+4）

应用器：`run_task` 构建 `TaskSpec.constraints` 时查库、注入 + `LESSON_APPLIED`；
任务结束复发计数。产物：`/lessons` 命令 + `<repo>/.xenon/PITFALLS.md` 投影。

## 切片 3+4（教训注入 + 产物）

- **切片 3**：`run_task` 注入教训为 constraints（上限 3、最近优先、排除
  retired/parameter），模型在 system prompt 必见（`- [lesson L-0001] …`）；
  `LESSON_APPLIED` 事件；任务结束复用 proposer 规范化指纹做复发计数 →
  达阈值 `LESSON_RETIRED`。真实模型探针：1 条教训 → 恰好 1 条约束。
- **切片 4**：`/lessons`（列出活动/退休/计数）、`/lessons write`（显式生成
  `<repo>/.xenon/PITFALLS.md`，pitfall 教训投影，绝不自动写）。

## 端到端验证（2026-11-12，教训即记忆之后）

`tty_lesson_double_run.py` 用真实 ConPTY 驱动 TS 前端（`deepseek/deepseek-v4-flash`），
在同一个会话里跑两轮：

1. 让模型执行一条 shell 命令，在审批面板上**拒绝**；
2. 让它读 README（只读任务）。

断言全部基于账本与磁盘，不看代码：

```text
  PASS  turn 1 ledger: lesson_proposed
  PASS  turn 1 ledger: lesson_stored
  PASS  turn 2 ledger: lesson_applied
  PASS  every proposed lesson was stored and later applied (same ids)
  PASS  project-local memory holds the LESSON record on disk
        record: lsn-3ffd045bb4383d75 tags=['fp:3ffd045bb4383d7550e4d877', 'lesson:preference', 'recurrences:0']
ALL EVIDENCE CHECKS PASSED
```

账本：`~/.xenon/ledger/repl/repl-24745364d6bd.jsonl`（提出/存储）、
`repl-2a05429773f2.jsonl`（应用）。

脚本还修了一个测试侧的坑：拒绝一次不够——模型会把同一条命令改成 `test`（verify）
再试，于是出现**第二个**审批面板（新的 `id`）。旧版脚本只按一次 `4`，第二个面板
一直等，看上去像"审批卡死"。现在脚本循环拒绝每个面板，直到本轮结束。

上面「切片 4：`/lessons` + PITFALLS.md」的描述已废弃：产物是记忆记录，见
`docs/design/self-evolution.md`。
