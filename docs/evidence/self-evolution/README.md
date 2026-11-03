# 自进化（self-evolution）切片 1+2：存储 + 事件 + 提取器

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
