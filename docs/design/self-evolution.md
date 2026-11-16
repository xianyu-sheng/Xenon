# 自进化（Self-Evolution）设计 —— 证据门控的在线改进

> 状态：**已落地**（2026-11-16 更新）。本文初版是设计稿；下文带「【实现】」
> 标记的段落是实现后对设计的修订。最重要的一条修订：**教训不单独建文件，
> 教训就是记忆**（见 §4），因此本文中所有 `lessons.yaml` / `PITFALLS.md` /
> `/lessons` 的描述均已废弃——它们曾经被实现，后按"只保留单一存储"的
> 决策删除。权威代码：`xenon/harness/learning.py`（唯一入口）、
> `xenon/harness/lessons.py`（存储）、`xenon/kernel/lesson_proposer.py`
> （提取）、`xenon/harness/runner.py`（调用方）。
>
> **2026-11-16 修订（总结 / 聚类 / 状态机）**：用户明确要求"不仅记下教训，
> 还要自主吸取和总结教训"。实现只增加**确定性聚类**，不让 LLM 参与任何
> 学习判断：结构相同的失败聚成一条 `rule`（总结），规则以
> `hypothesis → active → proven` 状态推进；应用后仍复发的规则退役，并开出
> **harness 设计债**——因为此时缺的是机制，不是又一句提示。详见 §1.4、§3。

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

### 护栏 1.4：总结必须是结构聚类，不是模型作文

【实现 2026-11-16】"取教训"与"总结教训"是两件事。总结由**结构族**
（`LessonShape = event|subject`）定义，代码在 `xenon/kernel/lessons.py`：

| 事件 | subject（来自代码字段，不是 prose） |
|---|---|
| `policy_decision` | capability（缺省回退 `scope[0]`） |
| `debt_opened` | obligation kind |
| `loop_detected` | loop signal |
| `user_approval` | capability / tool |

同一结构族中至少 2 条已存教训 → `LessonStore.summarize()` 生成**一条规则**
（`kind="rule"`，`derived_from` 保存全部成员 id，文本由模板生成）。规则不是
新的真相来源，而是族的摘要：可以审计、可以撤回、可以因复发而退役。

规则状态机（阈值全部代码所有）：`hypothesis`（刚聚类）→ `active`（被应用
≥2 次且同族失败未复发，`record_confirmation`）→ `proven`（≥5 次）。应用后
仍复发达阈值 → `retired`，**同时写 `DESIGN_DEBT_OPENED`**：同类失败在读过
这条规则后仍然发生，说明缺的是机制（工具设计/策略/验证），再加提示只会
制造噪声。已退役的族不会被 `summarize()` 复活。

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
- **parameter**：调整 `LoopProfile`/预算（如 `step_calls`、`max_stall_seconds`）
  属于构建期决策。【实现 2026-11-16】`match_for_task()` 现在把它作为**建议
  文本**携带（不再是"存储后永不再见"）：这条切片不自行修改预算，规则只能
  收紧调用方已经允许的边界；
- **preference**：【实现】与其他教训一样存为 LESSON 记录，用 `lesson:preference`
  标签区分；不另写 `preferences.yaml`；
- **rule**（2026-11-16 新增）：结构族的确定性摘要，见 §1.4。

### 护栏 1.5：唯一的集成边界 `LearningGateway`

【实现 2026-11-16】学习链路原先散落在 runner 的两个私有函数、store 和
kernel proposer 里，`run_task` 与 `resume_task` 各自复制了一遍调用。现在
任务只与一个门面交互（`xenon/harness/learning.py`）：

| 方法 | 输入 | 做什么 | 谁调用 |
|---|---|---|---|
| `recall(available, limit)` | 本任务 catalog 的 subject | 确定性打分、取最多 3 条 | `apply`（也可单独查询） |
| `apply(spec, ledger, runtime)` | 任务输入 + 账本 | 追加 `[lesson <id>]` 约束；写 `LESSON_APPLIED`；已在此账本应用过的教训不重复计数 | `run_task` / `resume_task` |
| `commit(ledger)` | 任务输出（账本） | 存储提案、复发计数（只数应用之后的事件）、聚类、确认；所有账本写入都做失败隔离 | `run_task` / `resume_task` |

门面**不改变**确定性：分类仍是 `LessonShape`，聚类仍是按族分组，打分仍是
"subject 在 catalog 中"，召回仍是读存储。LLM 不在这条链上；将来若接措辞
建议，只能作为展示层 note，不得写状态。存储与提案的职责也没有上移：
kernel 只提案，`LessonStore` 只落盘，门面只编排。

**为什么需要这个门面**：plan 阶段与获批后的 resume 是同一个用户任务。
两处各自 apply/commit 会让同一条教训在一轮里被计数两次，并把 plan 阶段
已经判定过的失败再算一次复发。门面把"每任务一次"的语义收在一处。

## 3. 生效时机（不干预进行中的任务）

教训只影响**携带它的下一个任务**，事后不回改——任务已经锁定了约束，事后
注入等于篡改证据。`run_task` / `resume_task`（审批恢复走同一条链）的顺序：

1. 内核运行**前**：`lesson_store.match_for_task(available=runtime.tool_subjects())`
   取最多 3 条活跃教训，以 `[lesson <id>] <action>` 追加进
   `TaskSpec.constraints`，并为每条写 `LESSON_APPLIED`、递增应用计数。
   【实现 2026-11-16】不再按"最近应用"盲选，而是按**结构相关性**排序：

   | 教训族 | 何时算相关 |
   |---|---|
   | `policy_decision` / `user_approval` | subject（capability/tool）出现在本任务 catalog 中 |
   | `debt_opened` / `loop_detected` | 任何任务都可能踩到，视为通用 |
   | catalog 信息缺失 | 失败开放（fail open），绝不静默丢掉安全教训 |

   被规则覆盖的成员教训不再逐条携带——**摘要替代枚举**。

2. 内核运行：模型在固定前缀的约束区**必须**看到这些教训；
3. 内核只写 `LESSON_PROPOSED`（它是输入的纯函数，没有文件系统）；
4. 任务收尾、账本校验通过后，四步全部确定性：
   - `store_proposals` 把提案落为记忆（`LESSON_STORED`）；
   - `record_recurrences` 统计本次失败与已应用教训的复发（规则按**族**
     复发，而非按自身指纹），达阈值 → 退休（`LESSON_RETIRED`）；
   - 退休的教训开一条 `DESIGN_DEBT_OPENED`（族、subject、应用/复发次数、
     机制修复建议）；
   - `summarize_and_confirm`：同族 ≥2 条失败 → `LESSON_CLUSTERED`（新规则，
     `hypothesis`）；本次**应用过的规则**若同族没再复发 → `LESSON_CONFIRMED`
     并推进状态。

指纹的用途因此明确：**确定性身份**（同一失败幂等存储、复发匹配），而不是
任务开始时的检索条件；"这个任务该带哪些教训"由结构族决定。

## 4. 复用现有机制，不重造

【实现】本节初版列了独立 `lessons.yaml` 与 `PITFALLS.md`，后被**"教训即
记忆"**决策取代——维持边界、单一模块拥有持久化（用户明确要求"尽量只单独
成为一个模块"，而记忆已经满足持久化需求）。实际复用：

- 存储：**零新文件**。教训是 project-local 的 `MemoryKind.LESSON` 记录，
  走记忆后端自己的写路径（`mutate_records`），原子写与 Markdown 投影与
  其他记忆完全相同；指纹、教训类别、复发计数以标签（`fp:` / `lesson:` /
  `recurrences:`）承载，不引入并行状态；
- 事件：`LESSON_PROPOSED` / `LESSON_STORED` / `LESSON_APPLIED` /
  `LESSON_RETIRED`，以及 2026-11-16 新增的 `LESSON_CLUSTERED` /
  `LESSON_CONFIRMED` / `DESIGN_DEBT_OPENED`。后者三者都在
  `_INTERNAL_EVENT_KINDS` 中：规则通过约束区进入模型提示，不从事件回放；
- 查看：`/memory lessons`（`/memory lessons --all` 含退役）按状态、族、
  应用/复发次数、`derived_from` 展示整条学习链；
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
- ❌ 让 LLM 写"总结教训"（总结 = 结构族的确定性聚类；LLM 至多提供措辞模板）；
- ❌ 跨用户/跨仓库的教训共享（工作区绑定，和 session grant 同规则）。

## 6. 验收

- [x] 失败事件 → 内核写 `LESSON_PROPOSED`（历史证据：曾有 621 个账本含
      提案、0 个含存储）；
- [x] 任务收尾 → `store_proposals` 落为 LESSON 记忆并写 `LESSON_STORED`；
- [x] 后续任务携带活跃教训为 constraint，写 `LESSON_APPLIED`、递增应用计数；
- [x] 同一指纹复发达阈值 → `LESSON_RETIRED`（记忆记录归档），不再应用；
- [x] 同族 ≥2 条失败 → `LESSON_CLUSTERED` 生成一条 `rule`，成员不再逐条携带；
- [x] 规则被应用且同族未复发 → `LESSON_CONFIRMED`，状态
      `hypothesis → active → proven`；
- [x] 规则应用后仍复发 → 退役 + `DESIGN_DEBT_OPENED`，且不会被重新聚类复活；
- [x] 全链路事件在任务账本里可回放；指纹确定性有单测覆盖；
- [x] `/memory` 下人工核对教训记录的展示效果；
- [x] **真实端到端**（2026-11-09，`docs/evidence/self-evolution/`）：
      通过 TS 前端 + 真实 ConPTY 跑两轮——第 1 轮在审批面板选择拒绝，
      账本含 `lesson_proposed`/`lesson_stored`；第 2 轮账本含
      `lesson_applied`，三轮 id 集合完全一致；`MemoryKind.LESSON` 记录
      物理存在于项目记忆目录。
- [x] **真实端到端（2026-11-16，确定性总结之后）**：同一脚本扩展第三段
      `/memory lessons`，真实 ConPTY + TS 前端 + `deepseek/deepseek-v4-flash`
      8 项断言全过；账本 `repl-50cd26e287cd`（提出/存储）与
      `repl-bf5d5cbd1c88`（应用）覆盖 `user_approval|shell` 与
      `debt_opened|requirement_met` 两族；`/memory lessons` 逐条渲染
      id、状态、计数与 `族:`。同族聚类由
      `test_a_family_of_failures_summarizes_into_a_rule_end_to_end`
      的确定性 runner 用例覆盖（真实探针自然产生的两族 shape 不同）。
- [x] **埋坑回放（2026-11-16，跨进程）**：`tty_lesson_pitfall_replay.py`
      在项目 A 拒绝 shell 审批 → **杀进程** → 新进程执行"列文件 + 读 README"
      埋坑任务：3 条教训从磁盘加载并全部进入 `TASK_STARTED.constraints`
      （`lsn-3ffd045bb4383d75` / `lsn-5edf0e60348b279f` /
      `lsn-6c351d7d4c44ec49`），本轮 `需要授权` 0 次，回答来自
      `read` + 工作区布局探测；项目 B 的全新进程无教训、无约束，同一任务
      照常完成。7 项断言全过；账本 `repl-7486403ef783`（提出/存储）、
      `repl-38f5d2d51f55`（应用）、`repl-1cc3ce9dbf45`（阴性对照）。

### 代码审查修正（2026-11-16）

对切片 1–7 的只读审查发现并修复了以下系统性问题（每项都有回归用例）：

| 问题 | 根因 | 修正 |
|---|---|---|
| 循环教训永不复发/退役 | 事件写 `kind`，规范载荷只读 `signal`，同一失败出现两个指纹 | `canonical_failure_payload` 回退读 `kind`；用真实事件载荷断言 |
| 教训文案的 subject 与它匹配的 subject 不一致 | proposer 从原始 payload 读 `capability`/`signal`，而指纹/族已回退 `scope`/`kind` | proposer 的措辞也从规范载荷取 subject/reason；补两种真实事件载荷的用例 |
| plan + resume 重复计数 | 两个入口各自 apply/commit，复发扫描按"是否在账本中出现"计数 | `LearningGateway` 单入口；复发只数**最近一次应用之后**的事件；同一账本内重复应用只计一次 |
| 跨项目泄漏 | 读取合并 USER/PROJECT_SHARED/SESSION，写入只去 PROJECT_LOCAL | 读写都限定 PROJECT_LOCAL；坏 metadata 只跳过该 scope |
| 30 天 stale 是死代码 | 只有无人调用的 `match()` 检查 stale | `match_for_task` 同样执行 stale 截止 |
| 归档教训被复活 | 进程内缓存过期；应用时不检查 retired | 去掉缓存（每次读后端）；`record_application` 拒绝已归档 |
| pin/expiry/检索计数被覆盖 | 每次应用都从默认值重建记录 | 用 `replace(existing, ...)` 只改教训拥有的字段；`counterfactual` 随 `cf:` 标签持久化 |
| 退役规则的成员重新逐条出现 | 只把**活跃**规则当作族代表 | 退役规则同样抑制成员；设计债而不是旧提示回来了 |
| 聚类来源不确定 | `source_task` 取后端插入顺序的第一个成员 | 成员按 id 排序后再取 |
| 状态可回退 | 只按生命周期 `applied` 计算目标状态 | 状态只前进（`hypothesis → active → proven`） |
| 学习事件写失败会毁掉已完成任务 | `commit` 的 append 未隔离 | `commit` 内所有账本写入 best-effort |
| 内部 `TypeError` 被误读为"旧 store"，绕过相关性过滤 | 靠捕获异常判断接口版本 | 用 `inspect.signature` 判断是否支持 `available` |
| 计划阶段 + 恢复执行把一次任务记成两次应用 | 去重集合在追加 `LESSON_APPLIED` 之后才计算 | 先读账本去重，再写事件与递增计数 |
| #420 的 `claim_fact_mismatch` 提案静默丢弃 | proposer 无该分支 | 删除死调用；`_sync_debt` 的 `DEBT_OPENED` 才是教训来源 |

## 7. 分片（最终落地形态）

1. **事件 + Store**：4 个 EventKind；`LessonStore`（match / match_for_task /
   store / record_application / record_recurrence / retire），记忆后端持久化。
2. **提取器**：内核在 harness 判定的失败点生成提案、写 `LESSON_PROPOSED`；
   指纹 = 事件 kind + 规范化 payload 子集哈希。
3. **应用器**：`LearningGateway.apply()` 在 `run_task` 开跑前按
   `recall()` 把活跃教训追加为 constraints + `LESSON_APPLIED`；
   `LearningGateway.commit()` 在收尾存储提案、复发计数、聚类、确认与退休。
4. **激活注入**：`RuntimeAdapter.from_repl` 单点构造共享 store，交互 REPL
   与协议 host（TS 前端）同时生效；项目刷新时随记忆服务一起重置。
5. ~~`/lessons` + PITFALLS.md~~ —— **已删除**：`/memory` 承担全部查看/编辑。
6. **真实验证**：**已完成**（2026-11-12）。真实 ConPTY + TS 前端 + `deepseek/deepseek-v4-flash`：
   第 1 轮拒绝 shell 审批 → 账本出现 `lesson_proposed` / `lesson_stored`；第 2 轮只读任务 →
   `lesson_applied`，且同指纹；项目本地记忆里存在 `lsn-…` 记录。证据：
   `docs/evidence/self-evolution/`（脚本 `tty_lesson_double_run.py`，账本
   `repl-24745364d6bd` / `repl-2a05429773f2`）。
7. **总结 / 聚类 / 状态机**（2026-11-16）：`LessonShape`、`summarize`、
   `record_confirmation`、`record_recurrences` 的规则族复发、
   `DESIGN_DEBT_OPENED`、`/memory lessons`；策略 veto 现在也会提案教训
   （此前它是唯一"永不成为教训"的失败类别）。
