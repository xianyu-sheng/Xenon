# 自进化（Self-Evolution）设计 —— 证据门控的在线改进

> 状态：**已落地**（2026-11-09 更新）。本文初版是设计稿；下文带「【实现】」
> 标记的段落是实现后对设计的修订。最重要的一条修订：**教训不单独建文件，
> 教训就是记忆**（见 §4），因此本文中所有 `lessons.yaml` / `PITFALLS.md` /
> `/lessons` 的描述均已废弃——它们曾经被实现，后按"只保留单一存储"的
> 决策删除。权威代码：`xenon/harness/lessons.py`、`xenon/harness/runner.py`。

## 0. 为什么叫"自进化"而不叫"自学习"

Xenon 没有权重，没有梯度，没有 RL。所谓"进化"是**证据门控的在线改进**：
从自己的失败里提取一条可执行的教训，把它变成可见、可撤销、可证伪的产物。
叫"自学习"会暗示模型权重在变；叫"自进化"强调的是**行为层面的、有证的
改进**——和 Xenon 的立身之本（证据校验）一致。

## 1. 三条护栏（不可让步）

### 护栏 1：只从 harness 判定的失败学习

模型自己说的"我错了/我应该…"**不是**教训来源——模型没有能力判断自己失败
的方式是否可复用。只有确定性判定可以：

| 判定 | 事件 | 可提取的教训类型 |
|---|---|---|
| 任务被拦 | `blocked`（`policy_decision` VETO / approval denied） | `pitfall`（这个仓库/这类操作不能这么干） |
| 开了债务 | `debt_opened`（claim 未被验证） | `pitfall`（声称与回执不符） |
| 循环被诊断 | `loop_detected`（`StuckDetector` 信号） | `parameter`（预算/策略参数不对） |
| 审批被拒 | `user_approval{approved:false}` | `preference`（用户不喜欢这种用法） |

**反例（不许做）**：模型的 reflection 文本、thought 里的"教训"、finish
总结里的自省——全是 prose，不可执行，不可证伪，会污染教训库。

### 护栏 2：教训必须可证伪（带 counter）

每条教训有一个**复发计数器**：当同一个教训在后续任务里被**应用**（产生
`LESSON_APPLIED`）但同源失败**仍然复发**，计数 +1；连续 N 次复发无改善，
教训**退休**（`LESSON_RETIRED`）并从活动集移除。

- 教训的"失败指纹"是确定性的（事件 kind + 关键 payload 的哈希），不是文本相似度；
- 退休是**记录**不是删除：`LESSON_RETIRED` 事件 + `retired: true` 标记，
  历史可回放；
- 一条教训如果 30 天内从未被应用，标记 `stale`（候选清理，不自动删）。

### 护栏 3：教训是可见产物

【实现】初版设计了三个产物（`lessons.yaml`、`PITFALLS.md`、`/lessons` 命令），
后来确定**教训即记忆**，三者全部废弃。实际产物：

| 产物 | 内容 | 谁读 |
|---|---|---|
| 一条 `MemoryKind.LESSON` 记忆记录（project-local） | 教训全文 + `fp:<指纹>` / `lesson:<kind>` / `recurrences:<n>` 标签 | 用户经 `/memory` 查看、编辑、归档 |
| `LESSON_APPLIED` 等事件 | 教训在某次任务中被应用（进该任务账本） | 回放 / 证据链 |

教训的可见性没有降级：`/memory` 能列出、编辑、归档任何一条教训，就像处理
其他记忆一样；指纹和复发计数以标签形式显示，人读得到。

一条教训从提取到生效的完整链路都可见：`LESSON_PROPOSED` → `LESSON_STORED`
→ `LESSON_APPLIED` → `LESSON_RETIRED`。**不需要人类或 harness 单独"确认"**：
内核判定的失败就是充分来源（护栏 1），存储在任务收尾时自动发生。

## 2. 三种教训

```yaml
lessons:
  - id: L-0001
    kind: pitfall                 # pitfall / parameter / preference
    trigger:                      # 确定性失败指纹
      event: debt_opened
      fingerprint: sha256(kind+target+reason)   # 事件 kind + 关键 payload 哈希
    counterfactual:               # 可证伪：什么情况下这条教训失效
      "repo has no tests directory and the objective is documentation-only"
    action:                       # 应用到下一次任务的行为
      kind: constraint            # 注入一条 constraint（确定性，不是提示词）
      text: "在无 tests/ 目录的文档型任务中，不要声称 test_passed"
    source_task: task-abc         # 从哪个任务学的
    applied: 0
    recurrences: 0
    retired: false
    created_at: ...
```

- **pitfall**：注入为 `constraint`（进 TaskSpec.constraints，确定性生效）；
- **parameter**：调整 `LoopProfile`/预算（如 `step_calls`、`max_stall_seconds`），
  只对同 profile 的下次任务生效；【实现】当前不会被 `match_for_task()` 自动
  携带（它是构建期决策，不进任务约束），仅存储并可经 `/memory` 查看；
- **preference**：【实现】与其他教训一样存为 LESSON 记录，用 `lesson:preference`
  标签区分；不另写 `preferences.yaml`。

## 3. 生效时机（不干预进行中的任务）

【实现】初版设想"构建 spec 时按指纹查库，命中才注入"。实现采用了更简单、
不依赖失败先验的形式：**每个任务携带一小批最近的活跃教训**。教训只影响
携带它的任务，事后不回改——任务已经锁定了约束，事后注入等于篡改证据。
`run_task` 中的实际顺序：

1. 内核运行**前**：`lesson_store.match_for_task()` 取最多 3 条最近应用的
   活跃教训（`parameter` 除外），以 `[lesson <id>] <action>` 追加进
   `TaskSpec.constraints`，并为每条写 `LESSON_APPLIED`、递增应用计数；
2. 内核运行：模型在固定前缀的约束区**必须**看到这些教训；
3. 内核只写 `LESSON_PROPOSED`（它是输入的纯函数，没有文件系统）；
4. 任务收尾、账本校验通过后：`store_proposals` 把提案落为记忆（写
   `LESSON_STORED`），`record_recurrences` 统计本次失败与已应用教训的
   指纹复发，达阈值 → 退休（写 `LESSON_RETIRED`）。

指纹的用途因此明确：**确定性身份**（同一失败幂等存储、复发匹配），而不是
任务开始时的检索条件；"这个任务该带哪些教训"由最近应用时间排序决定。

## 4. 复用现有机制，不重造

【实现】本节初版列了独立 `lessons.yaml` 与 `PITFALLS.md`，后被**"教训即
记忆"**决策取代——维持边界、单一模块拥有持久化（用户明确要求"尽量只单独
成为一个模块"，而记忆已经满足持久化需求）。实际复用：

- 存储：**零新文件**。教训是 project-local 的 `MemoryKind.LESSON` 记录，
  走记忆后端自己的写路径（`mutate_records`），原子写与 Markdown 投影与
  其他记忆完全相同；指纹、教训类别、复发计数以标签（`fp:` / `lesson:` /
  `recurrences:`）承载，不引入并行状态；
- 事件：`LESSON_PROPOSED` / `LESSON_STORED` / `LESSON_APPLIED` /
  `LESSON_RETIRED` 四个 EventKind（已存在）；
- 指纹：`xenon/kernel/lessons.py` 的确定性 `lesson_fingerprint`（事件 kind +
  规范化 payload 子集的哈希），与 compaction 同思路；
- 边界：`LessonStore` 不做任何文件系统工作；内核只提案、harness 才存储，
  与 kernel → harness 的既有分层一致；
- 产品内的激活点只有一处：`RuntimeAdapter.from_repl` 经 `_lesson_store_for`
  取 REPL 的共享记忆服务并包成 store；交互 REPL 与 TS 前端的协议 host 都走
  这条路。无记忆后端的最小宿主拿到 `None` = 不学习、不产生噪声。

## 5. 反设计（明确不做）

- ❌ 模型自我反思作文本教训（护栏 1）；
- ❌ 教训改变权重/模型（Xenon 无权重）；
- ❌ 教训自动应用到进行中的任务（篡改证据）；
- ❌ 用 LLM 判断"这条教训还对不对"（LLM 判断 = 不可证伪）；
- ❌ 跨用户/跨仓库的教训共享（工作区绑定，和 session grant 同规则）。

## 6. 验收

- [x] 失败事件 → 内核写 `LESSON_PROPOSED`（历史证据：曾有 621 个账本含
      提案、0 个含存储）；
- [x] 任务收尾 → `store_proposals` 落为 LESSON 记忆并写 `LESSON_STORED`；
- [x] 后续任务携带活跃教训为 constraint，写 `LESSON_APPLIED`、递增应用计数；
- [x] 同一指纹复发达阈值 → `LESSON_RETIRED`（记忆记录归档），不再应用；
- [x] 全链路事件在任务账本里可回放；指纹确定性有单测覆盖；
- [x] `/memory` 下人工核对教训记录的展示效果；
- [x] **真实端到端**（2026-11-09，`docs/evidence/self-evolution/`）：
      通过 TS 前端 + 真实 ConPTY 跑两轮——第 1 轮在审批面板选择拒绝，
      账本含 `lesson_proposed`/`lesson_stored`；第 2 轮账本含
      `lesson_applied`，三轮 id 集合完全一致；`MemoryKind.LESSON` 记录
      物理存在于项目记忆目录。

## 7. 分片（最终落地形态）

1. **事件 + Store**：4 个 EventKind；`LessonStore`（match / match_for_task /
   store / record_application / record_recurrence / retire），记忆后端持久化。
2. **提取器**：内核在 harness 判定的失败点生成提案、写 `LESSON_PROPOSED`；
   指纹 = 事件 kind + 规范化 payload 子集哈希。
3. **应用器**：`run_task` 开跑前按 `match_for_task()` 把活跃教训追加为
   constraints + `LESSON_APPLIED`；收尾存储提案、复发计数、退休。
4. **激活注入**：`RuntimeAdapter.from_repl` 单点构造共享 store，交互 REPL
   与协议 host（TS 前端）同时生效；项目刷新时随记忆服务一起重置。
5. ~~`/lessons` + PITFALLS.md~~ —— **已删除**：`/memory` 承担全部查看/编辑。
6. **真实验证**：**已完成**（2026-11-12）。真实 ConPTY + TS 前端 + `deepseek/deepseek-v4-flash`：
   第 1 轮拒绝 shell 审批 → 账本出现 `lesson_proposed` / `lesson_stored`；第 2 轮只读任务 →
   `lesson_applied`，且同指纹；项目本地记忆里存在 `lsn-…` 记录。证据：
   `docs/evidence/self-evolution/`（脚本 `tty_lesson_double_run.py`，账本
   `repl-24745364d6bd` / `repl-2a05429773f2`）。
