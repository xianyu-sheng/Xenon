# Steering 设计（待决）

目标：用户在 agent 干活的过程中能干预——追加约束、临时加限制、换策略——而不破坏"ledger 是唯一真相"。

本文不写代码，先把**注入点**和**语义**定下来，因为这两件事定错了，后面全是补丁。

## 一、先说清楚"不能怎么做"

旧引擎的做法是：一个后台线程读 stdin，通过 out-of-band 回调直接改 engine 的字段。

不能这么做，原因有三：

1. **账本分裂**：干预没有事件，回放出来的行为与当时不符；
2. **时序不定**：回调可能落在工具执行中间，读到半完成状态；
3. **无法验证**：事后无法回答"这条约束是谁在什么时候加的"。

所以 steered 输入必须是**事件**：`USER_INPUT`（事件类型已存在），由 runtime 在**安全边界**消费。

## 二、注入时机：三个候选

| 方案 | 语义 | 优点 | 代价 |
|---|---|---|---|
| **A. 动作边界**（Claude Code / Codex 风格） | 当前工具调用完成后、下一次 `propose` 之前注入 | 反馈快，用户等 1 个动作就能转向 | 需要模型每轮都看新约束；提示词里约束位置会变，可能影响决策稳定性 |
| **B. 步骤边界** | 当前 plan step 结束后注入 | 与 `step_verdict` 天然对齐，改动最小 | 一个 step 内可能已经做了很多无用功 |
| **C. 任务边界**（pi 风格） | 当前任务收尾后，作为下一轮输入 | 最干净：任务语义完整、账本清晰 | 用户想喊停时要等整个任务结束，产品体验差 |

推荐：**A 为默认，C 作为"新任务"的口子**。

理由：约束的本质是"别再做 X"，晚一个 step 才生效往往已经浪费了预算；而"这是一件新事情"本来就该是新任务（新 `task_id`、新账本段），不该塞进当前任务。

步骤边界（B）不需要单独实现：它天然是 A 的子集（在 step 关闭时不注入即可），由 profile 决定。

## 三、注入语义：三种干预，三种落账

| 用户意图 | 落账 | 对 view 的影响 | 是否可撤销 |
|---|---|---|---|
| **追加约束**（"别改 tests/"） | `USER_INPUT{kind:"constraint"}` | 进 `view.task.constraints`，system prompt 的 Constraints 段多一行 | 是（`constraint_retracted`） |
| **临时约束**（"这次先别跑 e2e"） | `USER_INPUT{kind:"constraint", scope:"turn"}` | 同上，但只活到当前 step 关闭 | 自动 |
| **换策略**（"别计划了，直接干"） | `USER_INPUT{kind:"strategy"}` | 影响下一轮的 `LoopProfile` 选择 | 是 |

三条硬规则：

1. **干预不能缩短义务**：`constraint` 只能加限制，不能删 obligation。删义务必须走 `user_accepted_debt`。
2. **换策略不等于换权限**：`permission` 只能由显式的 approval 改，steering 不能提权。
3. **注入是确定性的**：runtime 在每个动作边界把 `input_seq` 之后出现的 `USER_INPUT` 一次性消费，写进 `self._steering`，view 构建时读它。没有回调、没有线程。

## 四、它怎么和现有机制咬合

- `AskUser` 已经是 kernel 的动作（`USER_QUESTION` → `BLOCKED`）。steering 与它的区别：`AskUser` 是 agent 主动停下等人，steering 是人不等 agent 停下来就插话。两者共用 `USER_INPUT` 事件。
- 重复调用守卫（`RepeatedCallGuard`）解决"模型自己打转"，steering 解决"人知道它在打转"。二者互补：守卫触发时，如果账上有未消费的 `USER_INPUT`，优先消费它再决定是否 BLOCKED。
- 压缩：`USER_INPUT` 属于高优先级事件，不进摘要（否则用户的干预会被折叠掉）。

## 五、开放问题（需要你定）

1. **默认时机**：A（动作边界）还是 C（任务边界）？我倾向 A 但把"打岔"和"新任务"分开。
2. **是否允许 steering 撤销已发出的工具调用**：我倾向不允许（已执行的动作是事实），只影响后续。
3. **多轮 steering 的合并**：连续三条约束是合并成一条，还是逐条保留？我倾向逐条保留（可追溯），显示时合并。
4. **CLI 入口**：`/add-constraint` 显式命令，还是"直接打字就算 steering"？显式命令更可控，直接打字更像聊天式交互。

## 六、落地顺序（确认后再做）

1. `USER_INPUT` 事件的 payload 契约 + `steering` 解析（纯函数，可测）；
2. runtime 在动作边界消费（一个 `_consume_steering(view)` 挂点）；
3. view 的约束段与 profile 选择各接一处；
4. REPL 入口 + 证据脚本（真实模型跑一次中途改约束）。
