# Xenon 机制对齐清单（可靠性 / 可使用性 / 易使用性）

对照你列出的机制逐项自查，**只写代码里能查到的证据**。状态分四档：

- ✅ 已就绪：有实现、有测试、真实运行验证过
- ⚠️ 部分：实现了但偏弱/只覆盖一半
- ❌ 缺失：查不到实现
- 🐛 缺陷：能复现的问题

---

## A. 执行循环（最要紧的一类）

### A1. 步数约束 ⚠️ → 要改

| 事实 | 证据 |
|---|---|
| `Budget.max_steps` 默认 `None`（无限） | `xenon/kernel/contracts.py:231` |
| 但 `run_task` / `run_child_task` / CLI **默认传 20** | `xenon/harness/runner.py:136,218,286`；`xenon/harness/cli.py:74` |
| 所以实际永远是 20，`None` 从未生效 | 真实运行 `blocked reason: step budget exhausted` |

**问题不是"设了多少"，而是它成了第一道闸。** 真实运行里 18 次工具调用中有 6 次是 Windows shell 语法白烧（已修），任务本来能完成，却先被 20 截断。

**结论**：设 1024，并把 `None`（无限）作为可选。但**前提是下面 A2/A3 的兜底必须在位**，否则会跑飞。

### A2. Loop detector ⚠️（刚补齐，但不完整）

| 信号 | 状态 | 停机 |
|---|---|---|
| `identical_call` | ✅ `xenon/kernel/loop_signals.py` | 是（执行前） |
| `repeated_failure`（同一错误 ×4） | ✅ | 是 |
| `capability_cycle`（A→B→A→B ×3） | ✅ | 是 |
| `no_progress`（连续无新信息） | ⚠️ 只做诊断 | **否** |

**缺口**：`no_progress` 不停机是对的（真实运行证明误杀），但取消步数上限后，就**没有"温和的停滞兜底"**了。缺一个 **per-task 停滞超时**：连续 N 分钟没有新信息 → 停，原因写"停滞"而不是"预算耗尽"。

### A3. Loop verification（教 Agent 打破循环）❌

- 现在检测到循环 → `policy_decision{VETO}` + `BLOCKED`，**直接终止**。
- 缺"教它打破"：把循环诊断写进下一轮视图（"你已用同一命令失败 4 次，换一种方式"），给它一次纠偏机会，再终止。

### A4. 步数间校验（action 后替代 observation）✅

- `VerificationController.reconcile()`（`xenon/kernel/controller.py:373`）在每次回执后裁决 V0；
- `step_verdict` 进入模型视图（`xenon/kernel/view/base.py:34,224`），模型看到的是"回执 + 裁决"，不是裸 observation；
- 真实账本可见 `tool_receipt` → `step_verdict` 交替。

### A5. 结果校验 ✅

- V2 审计：`xenon/kernel/audit.py`（确定性 binder 裁定）+ `LLMAuditProposer` 只提案；
- 提案型裁决封顶 `PARTIAL`，永不升 `VERIFIED`。

### A6. Reflection ✅（位置与设计不同，效果等价）

- `LoopStrategy._reflect_or_finish`（`xenon/kernel/strategies/__init__.py:277`）在**下一次 action 之前**反思，能返回修正后的 `CallTool`；
- 反思输入包含上一轮的 `step_verdict`（视图里就有），所以"对校验后的结果反思"成立；
- 有重复修正检测（`_correction_signature` in `_seen`）→ 停止，避免反思自己成环。

### A7. 校验收敛到一处 ✅（kernel 侧）

- kernel 只有两个判定点：`controller.reconcile`（V0/V1）与 `audit.audit`（V2）；
- `engine/task_state.py` 里还有 `reconcile_satisfied_claims` 等**死方法**（全仓库外部零调用），是残留不是分叉。

---

**状态（#369，已落地）**：检测命中时不再直接 `BLOCKED`。写 `LOOP_DETECTED`
事件（kind/detail/tool 结构化事实），并把诊断注入下一轮视图的 `Notes:`
区段；给模型 `max_loop_corrections`（默认 1）次纠偏机会。同一信号重复命中
→ 终止，理由含 "after N correction attempt(s)"；模型换做法（无信号且产生
新信息）→ 计数重置。`max_loop_corrections=0` 完全恢复旧行为。纠偏只是提示，
证据/权限/交付判定不变。
## B. 上下文

### B1. 上下文压缩零 LLM ✅

- kernel：`xenon/kernel/compaction.py` `fold_dropped()` 纯结构化折叠，`CONTEXT_COMPACTED` 入账；
- REPL 手动：`/compact` 无参数时 `ctx_mgr.structured_summary()`，**明确注释 "no LLM call"**（`xenon/repl/command_groups/session.py:588`）。
- ⚠️ 但**两处压缩互不相识**：chat history 压缩（`ContextManager.compact`）与 kernel working set 折叠（`compaction.py`）是两套。数据不同（对话 vs 事件投影），可以接受，但**用户看到的是两个不同的"压缩"概念**，需要文档说清。

### B2. 记忆分层 ✅

- `MemoryScope`：`USER` / `PROJECT_LOCAL` / `PROJECT_SHARED` / `SESSION`（`xenon/memory/models.py:27`）；
- `MemoryKind`：`PREFERENCE` / `FACT`；
- kernel 轮次通过 `session_context_blocks()` 注入（`#340`），标注为"参考，不是指令，不是证据"。

### B3. 树感知 ✅（#372 已落地）

- `TurnTree` 存在（`xenon/session/tree.py`），Pi 风格，可从事件流重建；
- 接入了 `rewind` / `session_ops`；#372 起 `TurnTree.turn_path()` 在 rewind/fork 后
  把分支路径送进 kernel 视图（`Turn path:` 行，`ModelView.turn_path`），线性对话不渲染。
- 普通对话仍是 `working_set`（事件投影），树只补分支上下文，不替代投影。

---

## C. 工具与能力

### C1. 工具收敛 ✅

- core 只有 5 个：`read`、`search`、`apply_patch`、`bash`、`submit_plan`；
- `verify`/`vcs` 是 `bash` 的 preferences，不是独立工具；
- 真实运行工具调用从 18 降到 7（修 shell 描述后）。

### C2. 渐进式加载 ❌

- 查不到实现（只有 `xenon/integration_cli.py:152` 一个 JSON 字段 `"progressive_loading": True`，无实现）；
- 现状：`core+mcp` profile 时 **MCP 全部工具全量注入**（`xenon/catalog/catalog.py:175`）；
- skill 也没有"只拼名字+一句话，用时再加载"的机制。

### C3. MCP 惰性连接 ✅（连接层）

- `add_server_pending()` 只存配置，首次 `discover_tools()` 才起子进程（`xenon/mcp/registry.py:217`）；
- ⚠️ 但**发现之后全量进提示词**，这是 C2 的问题。

### C4. 沙箱 ❌

- 只有**工作区边界**：`edit outside workspace or without target` → VETO（`xenon/kernel/policies/__init__.py:398`）；
- **无容器**。超出工作区的读取/执行直接变成危险命令 —— 正如你说的，这与 Claude Code / Codex / pi 的体验差距很大。
- 轻量容器隔离是独立工程（#123 已开，未做）。

---

## D. 模型调用侧

### D1. 缓存前缀优先 ✅（#372 已落地）

- `cache_context` / `cache_lane_registry` 存在（`xenon/utils/llm_client.py:165`）；
- kernel 视图天然前缀稳定（system prompt 确定性生成 + working set 按 seq 排序）；
- ⚠️ 但**没有"前缀命中率"的可观测性**，也没有测试锁"同一任务两次调用的前缀必须一致"。改视图容易悄悄破坏缓存。

### D2. 多模型路由 ✅

- `xenon/repl/auto_router.py`，`LLM 意图识别 + 正则约束`（`turn_contract.py`：LLM leads，regex owns constraints）。

---

## E. 交互与可靠性

### E1. 状态栏与 loop 接轨 ✅（#372 已落地）

- `status_bar.set_active_loop()`（`xenon/repl/status_bar.py:71`），`_display_mode()` 优先显示正在跑的 loop（`#345`）。
- ⚠️ 但只显示 loop **名字**，不显示步数/停滞状态/当前阶段。

### E2. 审批卡住 🐛（部分已修，需复审）

- `#344` 修了"拒绝但不说原因"；`#345` 修了 stdin 争用（rich `Prompt.ask` 与 prompt_toolkit 抢同一个句柄）；
- ⚠️ **信号量未验证**：`repl._active_runtime` 决定走哪条审批路径，异常路径下是否会残留导致后续审批永久失败，需要专项测试。

### E3. 无限步数的必要兜底 ⚠️

取消步数上限后，剩下的闸只有：

| 闸 | 存在 | 说明 |
|---|---|---|
| token budget | ✅ | `Budget.max_tokens` 真实生效 |
| wall clock | ✅ | `Budget.max_wall_seconds` |
| loop detector 硬信号 | ✅ | 刚补齐 |
| **停滞超时** | ❌ | 需要新增（A2 缺口） |
| 人工中断 | ✅ | SIGINT → `ABORTED` |

---

## 优先顺序（我建议）

1. **A1 + A2 缺口**：步数 20 → 1024，同时补"停滞超时"。**必须先有兜底再放开**，否则等于拆掉刹车。
2. **A3 loop verification**：循环时先教一次再终止，而不是直接杀。
3. **E2 审批复审**：卡住是最伤体验的一类。
4. **C2 渐进式加载**：直接决定"易使用性"（工具多起来提示词会爆）。
5. **D1 前缀命中测试**：锁住缓存不退化。
6. **C4 容器沙箱**：工程量最大，独立推进。
7. **B3 树感知接视图**：让 kernel 用树而不是平铺 working set。

---

## 附：本清单没覆盖、但我查到的其它问题

- `engine/task_state.py` 死方法（`reconcile_satisfied_claims` 等）零外部调用，目录名仍误导；
- `xenon/repl/kernel_host.py` 名字是 legacy，身体已经是 kernel（`_run_react_engine` → `run_kernel_turn`）；
- `turn_flow.py` 里 `pending`/`recipe` 两条 shim 仍走旧 `TaskState` 路径。
---

## 职责边界表（防重复添加：每个机制只管一件事）

| 机制 | 生命周期 | 注入时机 | 匹配维度 | 视图通道 | 一句话职责 |
|---|---|---|---|---|---|
| 自进化 lessons | 跨任务/跨会话 | 任务开始（预置） | 指纹（事件 payload） | `Constraints: - [lesson L-x]` | 从历史学：上次同指纹任务失败 → 这次带上教训 |
| 循环验证 loop notes | 单任务内 | 信号命中后下一轮 | 信号（kind+window） | `Notes:` 区段 | 当场教：本次陷入循环 → 诊断给一次纠偏 |
| steering / operator | 单任务内 | 实时 USER_INPUT | seq | `Constraints: - … [operator, seq N]` | 人在环里：operator 实时指令/纠正 |
| memory context_blocks | 跨任务会话 | 会话注入 | label | context blocks | 参考材料：检索/设置的背景，非指令非证据 |
| session grants | 跨任务会话 | 授权后 | capability+target | （不写视图） | 权限记忆：为什么不再问（可撤销） |
| debt/obligations | 单任务内 | 未验证即记 | verdict | `Open obligations` | 未验义务：不可静默丢弃 |
| reflection | 单任务内 | 每次 action 前 | 上轮 verdict | （策略内） | 模型自省：对校验结果反思，防自环（`_seen`） |
| audit proposer | 单任务内 | finalize 阶段 | 语义义务 | （提案） | 收敛保证：提案封顶 PARTIAL，轮数上限 |

**防环三保险（同原则、不同对象，不是重复）**：loop detector 防**模型**循环
（#369 教一次再杀）；reflection `_seen` 防**反思**循环；finalizer
`_audit_rounds` 防**审计**循环。若未来某处需要重复实现"循环计数"，应提取
共享组件而不是再写一份。

**已知的重叠风险（已记录）**：
- A2 `no_progress` 信号与 E3 停滞超时是同一个洞的两种填法——停滞超时应做
  **no_progress 的 wall-clock 触发器**（同一 StuckDetector），继承"教一次"
  逻辑，不新增机制。
- B1 两套 compaction（chat history vs kernel working set）：数据不同可接受，
  但用户可见的"压缩"概念要文档说清。

## 执行顺序：先做，后验，补不上才披露（#414，产品原则）

Xenon 的立意是「Agent 的输出不可信」。这个问题的**前提是 Agent 确实做了**——没有执行就没有
可校验的对象。因此内核的职责边界是：

> **内核不替人决定「能不能做」，只决定「做完了算不算数」。**
> 许可由人决定（一次，或一条可复查的规则）；证据由内核决定；补救与披露是最后一步。

按这个原则，所有「做之前」的代码只有三种合法形态：

| 形态 | 判据 | 允许的处理 | 现在的例子 |
|---|---|---|---|
| **A 物理不可能** | 该动作执行也必然失败 | 提前拒绝，**理由必须含可执行下一步**，并计数 | 无 target 的 read、拿目录当文件、参数名不对 |
| **B 未授权副作用** | 能成功，但需要人点头 | **问 → 记住答案 → 执行**；无审批通道时 fail-closed | 越界写、`command`、`mcp_call`、push、破坏性命令 |
| **C 已检测到失败后的止损** | 失败已发生 | 补救（教一次 / 收尾 / 允许一次例外），有界、记录、可解释 | 循环检测 → `force_finish`（+1 次工具额度）、修复预算、停滞超时 |

**明确不允许**：把 B 类当 A 类处理（替人决定），或在 C 类里无限收紧（把补救变成惩罚）。

**校验侧永远不拦执行**：V0/V1/V2、claim→fact 绑定、coverage 审计的产物是 fact / debt /
abstain，它们只决定「这次交付算不算数」，不决定「这次能不能试」。

### 已验证的偏离（2026-11-04 修正）

| 曾有的行为 | 类别误判 | 修正 |
|---|---|---|
| 越界写直接 `SecurityError`，批准了也照拒 | B 当 A | 面板批准 → 一次性路径授权 → 围栏服从（`consume_grant`） |
| `git push` / 破坏性命令「永不可批准」 | B 当 A | 改为问人；无审批通道时 fail-closed |
| `kind=think` 的步骤禁止调工具 | B 当 A | 删除守卫；步骤类型只是给视图的提示 |
| 循环判死后工具永久关闭 | C 过严 | 允许一次收尾工具，记 `force_finish_allow_once` |
| 步数预算耗尽不写 `policy_decision` | 记录缺失 | 统一走 `_budget_block`，附循环诊断 |
