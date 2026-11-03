# 自进化（Self-Evolution）设计 —— 证据门控的在线改进

> 状态：**设计稿**。按约定先设计、后代码。任何部分未实现前，这条文档就是
> 唯一权威——它约束实现，不许实现反过来改写它。

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

| 产物 | 内容 | 谁读 |
|---|---|---|
| `~/.xenon/lessons.yaml` | 全部教训（活动 + 退休 + 计数） | 用户 / `/lessons` 命令 |
| `<repo>/.xenon/PITFALLS.md` | 该仓库命中的 pitfall 教训（人类可读） | 用户 / 模型（参考，非指令） |
| `LESSON_APPLIED` 事件 | 教训在某次任务中被应用（进该任务账本） | 回放 / 证据链 |

一条教训从提取到生效的完整链路都可见：`LESSON_PROPOSED` → （人类或 harness
确认）→ `LESSON_STORED` → `LESSON_APPLIED` → `LESSON_RETIRED`。

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
  只对同 profile 的下次任务生效；
- **preference**：写入 `~/.xenon/preferences.yaml`（现有 MemoryKind.PREFERENCE），
  仅当用户连续拒绝同一类调用两次以上。

## 3. 生效时机（不干预进行中的任务）

教训只影响**下一次**同指纹任务的构建，从不改写当前任务的 spec——正在跑的任务
已经锁定了约束/预算，事后注入等于篡改证据。应用点：

1. `run_task` 构建 `TaskSpec.constraints` 时，查教训库匹配指纹；
2. 命中 → 追加 constraint + `LESSON_APPLIED` 事件（该任务的账本）；
3. 任务结束时，harness 判定（护栏 1 的事件源）复发 → `recurrences += 1`；
   ≥ 阈值 → `LESSON_RETIRED`。

## 4. 复用现有机制，不重造

- 存储：`~/.xenon/lessons.yaml` 新文件（记忆系统的 `lessons.md` 是自由文本，
  教训需要结构化字段：指纹/counter/退休标记——不塞进自由文本）；
- 事件：`LESSON_PROPOSED` / `LESSON_STORED` / `LESSON_APPLIED` /
  `LESSON_RETIRED` 新增 4 个 EventKind；
- 指纹：复用 `_payload_digest`（compaction 同款确定性哈希）；
- PITFALLS.md：从 lessons.yaml 投影（确定性生成，可 `git diff`）。

## 5. 反设计（明确不做）

- ❌ 模型自我反思作文本教训（护栏 1）；
- ❌ 教训改变权重/模型（Xenon 无权重）；
- ❌ 教训自动应用到进行中的任务（篡改证据）；
- ❌ 用 LLM 判断"这条教训还对不对"（LLM 判断 = 不可证伪）；
- ❌ 跨用户/跨仓库的教训共享（工作区绑定，和 session grant 同规则）。

## 6. 验收

- [ ] 一个 `blocked`（VETO）任务 → `LESSON_PROPOSED` + 指纹匹配的下次任务
      获得对应 constraint + `LESSON_APPLIED`；
- [ ] 同一指纹复发 3 次 → `LESSON_RETIRED`，`retired: true`，不再应用；
- [ ] `/lessons` 列出活动/退休/计数；PITFALLS.md 确定性生成；
- [ ] 全链路事件在任务账本里可回放；
- [ ] 真实模型：同一个会触发 VETO 的错误任务跑两次，第二次带教训约束
      → 不再踩坑（或诚实记录失败）。

## 7. 分片

1. **存储 + 事件**：`lessons.yaml` schema、4 个 EventKind、`LessonStore`
   （load/save/apply/retire/retire threshold）。
2. **提取器**：从 harness 判定（blocked/debt/loop/denied）生成 `LessonProposal`，
   `LESSON_PROPOSED` 事件；指纹 = 事件 kind + 关键 payload 哈希。
3. **应用器**：`run_task` 构建 constraints 时查库、注入 + `LESSON_APPLIED`；
   任务结束复发计数。
4. **产物**：`/lessons` 命令 + PITFALLS.md 投影 + `LESSON_RETIRED` 阈值。
5. **真实验证**：探针（同一失败任务两次，第二次带教训）。
6. **产物**（切片 4，已落地）：`/lessons` 列出活动/退休/计数；
   `/lessons write` 显式生成 `<repo>/.xenon/PITFALLS.md`（pitfall 教训的
   确定性投影，**绝不自动写**）。

每条分片独立可合并、独立测试、独立证据。
