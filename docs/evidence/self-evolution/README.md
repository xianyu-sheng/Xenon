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

## 端到端验证（2026-11-16，确定性总结之后）

`tty_lesson_double_run.py` 重跑（真实 TS 前端 + ConPTY +
`deepseek/deepseek-v4-flash`），并新增第三段 `/memory lessons` 断言：

```text
front-end ready
denying approval #1
turn 1 finished after 1 denial(s)
turn 2 finished — ledger: …\repl-bf5d5cbd1c88.jsonl
/memory lessons rendered: True

  PASS  turn 1 ledger: lesson_proposed
  PASS  turn 1 ledger: lesson_stored
  PASS  turn 2 ledger: lesson_applied
  PASS  every proposed lesson was stored; turn 2 applied them directly or
        through the rule that summarizes them
  PASS  if turn 1 summarized a rule, turn 2 carried that rule
  PASS  project-local memory holds the LESSON record on disk
  PASS  /memory lessons rendered the learned family
  PASS  /memory lessons named every stored lesson
ALL EVIDENCE CHECKS PASSED
```

账本 `repl-50cd26e287cd`（提出/存储）与 `repl-bf5d5cbd1c88`（应用）：

- `user_approval|shell` 提案 → `lsn-3ffd045bb4383d75` 存储 → 下一轮应用；
- `debt_opened|requirement_met` 提案 → `lsn-6c351d7d4c44ec49` 存储 → 应用；
- `/memory lessons` 逐条显示 id、状态、应用/复发计数与 `族:`。

同族聚类（同 shape ≥2 条失败 → 一条 `rule` + `LESSON_CLUSTERED`）由确定性
runner 用例覆盖：`tests/test_lessons.py::test_a_family_of_failures_summarizes
_into_a_rule_end_to_end`（两轮真实 `ClaimClient` 运行产生同 shape 债务）。
真实 UI 探针自然产生的两类失败 shape 不同（`user_approval|shell` 与
`debt_opened|requirement_met`），不构成同族，因此不会出现规则。

这次验收还发现并修掉一个边界 bug：`/memory lessons` 在 TS 前端里丢掉了
所有 `[lsn-…]` id——rich 把方括号标识当未知样式标签静默吞掉。修复落在
`xenon/repl/rich_markup.py`，交互 REPL 与协议 host 共用同一函数。

## 端到端验证（2026-11-16，跨进程埋坑回放）

`tty_lesson_pitfall_replay.py` 验证的不是"事件出现了"，而是"历史真的改变了
下一次行为"。三个**彼此独立的 xenon 进程**（真实 ConPTY + TS 前端 +
`deepseek/deepseek-v4-flash`，`--level auto-edit`）：

1. **会话 1（项目 A）**：让模型执行 shell 命令，审批面板选 `[4]` 拒绝
   （模型随后把命令改走 `verify`，再次被拒）→ 写入教训并落盘；
2. **杀掉进程**，**会话 2（项目 A，全新进程）**：埋坑任务——"列出项目根目录
   文件并读 README 第一行"。朴素做法是 `shell`（会再次弹审批）；
3. **会话 3（项目 B，另一个全新进程）**：同一埋坑任务，作为阴性对照。

```text
session 1: planting a denied shell call in project A
        denying approval #1
session 1 ledger: …\repl-7486403ef783.jsonl (1 denial(s))
session 2: NEW process, project A, planted trap task
session 3: NEW process, project B (negative control)

  PASS  session 1 stored a lesson on disk
  PASS  session 2 (new process) applied the stored lesson
  PASS  the model actually saw it: TASK_STARTED constraints carry [lesson ...]
  PASS  the trap was avoided: no approval panel in session 2
  PASS  session 2 answered from the file it read
  PASS  negative control: a project without history carries no lesson
  PASS  negative control still completed the same task
ALL EVIDENCE CHECKS PASSED
```

账本与真实载荷：

- `repl-7486403ef783`（会话 1）：3 条 `lesson_proposed` → 3 条
  `lesson_stored`，id `lsn-3ffd045bb4383d75`（`user_approval|shell`）、
  `lsn-5edf0e60348b279f`（`user_approval|verify`）、
  `lsn-6c351d7d4c44ec49`（`debt_opened|requirement_met`）。
- `repl-38f5d2d51f55`（会话 2）：**没有任何新进程内状态**，从磁盘读到 3 条
  教训并全部应用；`TASK_STARTED.constraints` 实际是：
  - `[lesson lsn-3ffd045bb4383d75] shell 调用被用户拒绝（用户拒绝）——下次避免同类调用`
  - `[lesson lsn-5edf0e60348b279f] verify 调用被用户拒绝（用户拒绝）——下次避免同类调用`
  - `[lesson lsn-6c351d7d4c44ec49] 声称 requirement_met 前必须存在对应工具回执（no successful tool receipt for this task）；不要凭空声称`
  本轮 `user_approval` 事件 0 次，屏幕 `需要授权` 0 次；回答里的目录清单来自
  工作区布局探测 + `read` 工具，而不是 `shell`。
- `repl-1cc3ce9dbf45`（会话 3，阴性对照）：没有 `lesson_applied`，没有
  `[lesson …]` 约束；同一任务照常完成（README 首行是 `# fresh project B`）。
  教训是项目本地的，不跨项目泄漏。

屏幕证据在 `pitfall_transcript.txt`（已压缩：只保留每轮尾部、去掉 Ink 整屏
重绘，避免动辄十几万行的终端录像淹没 diff）。

