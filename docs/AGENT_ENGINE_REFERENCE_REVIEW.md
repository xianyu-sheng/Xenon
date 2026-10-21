# Agent 引擎参考实现审计

> 日期：2026-10-21
> 范围：Xenon 自研 `ReActEngine` / `PlanExecuteEngine` / `TaskState`
> 目的：对照成熟 Agent 框架的真实循环语义，找出我们“看起来能跑、实际会假完成/空转”的地方。
>
> 说明：以下实现是广泛使用的参考实现，不等于“绝对正确”；我们只借鉴可验证的**控制流、状态和失败语义**，不照搬代码。

参考源码：

- LangGraph: https://github.com/langchain-ai/langgraph/blob/main/libs/prebuilt/langgraph/prebuilt/chat_agent_executor.py
- LangChain AgentExecutor: https://github.com/langchain-ai/langchain/blob/master/libs/langchain/langchain_classic/agents/agent.py
- LangChain PlanAndExecute: https://pypi.org/project/langchain-experimental/ (`plan_and_execute/agent_executor.py`)
- smolagents: https://github.com/huggingface/smolagents/blob/main/src/smolagents/agents.py
- SWE-agent: https://github.com/SWE-agent/SWE-agent/blob/main/sweagent/agent/agents.py
- Aider: https://github.com/Aider-AI/aider/blob/main/aider/coders/base_coder.py
- OpenAI Agents SDK: https://github.com/openai/openai-agents-python/blob/main/src/agents/run.py

---

## 1. 参考实现

| 来源 | 关键文件 | 我们关注的点 |
|---|---|---|
| LangGraph | `libs/prebuilt/langgraph/prebuilt/chat_agent_executor.py` | `agent -> tools -> agent` 图、`should_continue`、`remaining_steps`、checkpointer、interrupt |
| LangChain Classic | `libs/langchain/langchain_classic/agents/agent.py` | `AgentExecutor` 的 `max_iterations=15`、`max_execution_time`、`early_stopping_method`、解析错误处理 |
| LangChain Experimental | `langchain_experimental/plan_and_execute/agent_executor.py` | 经典 Plan-and-Execute：planner 一次、逐 step 执行，**没有 replanner** |
| smolagents | `src/smolagents/agents.py` | `max_steps=20`、`planning_interval`、`final_answer_checks`、`AgentMaxStepsError`、memory steps |
| SWE-agent | `sweagent/agent/agents.py` | `max_steps`、`_run_stream`、达到上限后的 final answer、`AgentMaxStepsError` |
| Aider | `aider/coders/base_coder.py` | `max_reflections=3`、lint/test 反馈、auto commit |
| OpenAI Agents SDK | `src/agents/run.py` | `max_turns`、handoff/tool loop、guardrails、`MaxTurnsExceeded` |

### 1.1 LangGraph ReAct 的真实结构

LangGraph 的核心不是“一个 while 循环里塞满提示词”，而是显式图：

```text
agent (LLM)
  ├─ 无 tool_calls -> END / structured response
  └─ 有 tool_calls -> tools
                       └─ 回到 agent
```

关键点：

- `should_continue()` 只看最后一条 `AIMessage` 是否有 `tool_calls`；没有就结束。
- `remaining_steps` 是显式状态；工具调用存在但剩余步数不足时，返回
  `"Sorry, need more steps to process this request."`，而不是继续做工具。
- 通过 `checkpointer` / `interrupt_before` / `interrupt_after` 提供可恢复的状态和人工审批。
- 工具节点负责 `tool_call_id` 配对；会检测“有 tool_calls 但没有 ToolMessage”的坏历史。
- `recursion_limit` 由运行时提供，不是靠文本相似度猜循环。

### 1.2 LangChain AgentExecutor 的真实终止语义

`AgentExecutor` 默认：

```python
max_iterations = 15
max_execution_time = None
early_stopping_method = "force"
```

循环条件：

```python
def _should_continue(iterations, time_elapsed):
    if max_iterations is not None and iterations >= max_iterations:
        return False
    return max_execution_time is None or time_elapsed < max_execution_time
```

达到上限后，`force` 明确返回：

```text
Agent stopped due to iteration limit or time limit.
```

它不是“无限循环 + 文本循环检测”。解析错误有独立的
`handle_parsing_errors` 策略，不会和工具执行错误混在一起。

### 1.3 smolagents 的真实循环

`MultiStepAgent._run_stream()`：

```python
while not returned_final_answer and self.step_number <= max_steps:
    if self.interrupt_switch:
        raise AgentError("Agent interrupted.")
    # 可按 planning_interval 生成/更新计划
    # 执行一个 action step
    # final_answer 必须经过 final_answer_checks
if not returned_final_answer:
    final_answer = self.provide_final_answer(task)
    # 记录 AgentMaxStepsError
```

值得借鉴的不是 `max_steps=20` 这个数字，而是：

1. **步数是显式的执行状态**，不是提示词里的隐含约束；
2. **计划可以按 interval 更新**；
3. **final answer 有可执行校验回调**；
4. 达到上限仍然给出可解释的 final answer，并标记 `max_steps_error`。

### 1.4 SWE-agent 的真实循环

`MultiStepAgent.run()` / `_run_stream()` 同样是有限 `max_steps` 循环：

```python
while not returned_final_answer and self.step_number <= max_steps:
    ...
if not returned_final_answer and self.step_number == max_steps + 1:
    final_answer = self._handle_max_steps_reached(task)
```

它把“模型生成错误”和“工具执行错误”分开记录到 memory step，而不是把一切失败都变成一句 warning。

### 1.5 Aider 的真实反馈闭环

Aider 不是全自动工具循环，但它的编辑后反馈非常值得借鉴：

```python
run_one():
    while message:
        send_message(message)
        if not reflected_message:
            break
        if num_reflections >= max_reflections:
            return
        num_reflections += 1
        message = reflected_message
```

编辑后：

```python
if edited and auto_lint:
    lint_errors = lint_edited(edited)
    if lint_errors:
        reflected_message = lint_errors

if edited and auto_test:
    test_errors = cmd_test(test_cmd)
    if test_errors:
        reflected_message = test_errors
```

也就是说：

- **lint/test 的真实输出**驱动下一轮；
- 反思有上限；
- 自动 commit 是编辑后的独立动作，不是“命令字符串里出现 commit 就算提交”。

### 1.6 经典 Plan-and-Execute 其实也很简单

`langchain_experimental` 的 `PlanAndExecute`：

```python
plan = planner.plan(inputs)
for step in plan.steps:
    response = executor.step(...)
    step_container.add_step(step, response)
return step_container.get_final_response()
```

它**没有 replanner**，也不在失败后重新规划。  
所以：

> Plan-and-Execute 不是“更高级、更可靠”的架构；它只是把一次规划的结果顺序执行。
> 对真实编码任务，单次计划 + 失败跳过，比 ReAct 更容易空转/空补丁。

Xenon 的 Plan-Execute 需要面对这个事实，而不是假设“计划模式天然更强”。

---

## 2. Xenon 对照结果

### 2.1 终止条件

| 维度 | 参考实现 | Xenon 当前 | 风险 |
|---|---|---|---|
| 默认步数 | LangChain 15、smolagents/SWE-agent 20、LangGraph remaining_steps 约 25 | `max_iterations=0`，无步数上限 | 没有外部硬预算时，只能靠 LoopDetector/墙钟；真实长任务可能烧 token |
| 墙钟 | LangChain `max_execution_time`；Xenon 有 1800s deadline | 有，但默认很长 | 需要显式记录终止原因 |
| 循环 | 主流靠硬预算/递归限制；图状态显式 | 文本哈希 + 工具模式 + 错误/thought 哈希 | 不同文本但同一工作区状态可能绕过 |
| 终止输出 | `Agent stopped...` / `max_steps_error` / final answer | 多种 warning + 文本 | 调用方难以判断“完成 / 超时 / 循环 / 交付缺失” |

**结论**：Xenon 的 unlimited 是产品选择，但不能把“没有步数上限”当成“有终止保证”。  
至少需要一个显式的 `TerminationReason`，并让所有引擎统一返回。

### 2.2 Plan-Execute

| 维度 | 成熟实现 | Xenon 当前 | 真实观察 |
|---|---|---|---|
| 计划生成 | 一次或按 interval 更新 | `_plan()` 一次；仅“无写步骤”时重规划 | 真实 #26 切片三轮都没有生成有效测试/提交 |
| 失败步骤 | 外层循环/reflection 继续修 | `_ensure_task_completed()` 事后补救；工具步骤失败才 mini-ReAct | 计划步骤失败后仍能走到“总结” |
| 计划参数 | 执行期由模型生成 | 计划阶段预生成 params；`edit_file` 缺 old/new 时转 mini-ReAct | 预绑定参数经常错 |
| 计划状态 | memory/step 状态 | 本轮新增 `TaskState` 同步 | 修正前空计划也能 completed |
| 最终验收 | lint/test/final checks | 命令字符串 + TaskState 启发式 | 真实空测试文件 + 空提交曾被当成完成 |

**结论**：Plan-Execute 目前是“规划一次 + 批量执行 + 事后补救”，不是闭环。  
要么加 **replanner**（失败/无进展后重规划，最大 2-3 次），要么不要把 Plan-Execute 作为代码交付默认引擎。

### 2.3 TaskState / 交付

参考实现中，验证通常是**结构化工具结果**：

- Aider 的 `lint_outcome` / `test_outcome`；
- smolagents 的 `final_answer_checks`；
- LangGraph 的 tool message / structured response。

Xenon 目前仍有一部分“从命令/输出文本推断”：

- `_VERIFY_RE` 判断命令像不像测试；
- `_COMMIT_OUTPUT_RE` 从 commit 摘要推断哈希；
- `record_tool("write_file")` 只记录路径，不记录写入前后的内容/hash。

这些启发式已经被真实任务打穿过：

- `git commit` + `git log` 同时出现时取到旧哈希；
- 测试输出 `1 failed, 2 passed` 仍可能被记录；
- 只有测试文件的 commit 曾满足“源码交付”；
- 空测试文件 + 空提交曾让 TaskState completed。

**建议**：把“验证/提交”从 `TaskState` 的字符串解析，改为接收 `ToolResult` 的结构化字段：

```text
ToolResult:
  tool, params, success, exit_code, stdout, stderr,
  changed_paths, result_id, metadata, duration
```

`TaskState` 只消费这些字段，不再自己猜命令语义。

### 2.4 工具协议

LangGraph 的 `ToolNode` 强制：

- `tool_call_id` 与 `ToolMessage` 配对；
- 并行工具调用逐个返回；
- 缺失结果会报坏历史，而不是静默把前一个 observation 塞给下一个工具。

Xenon 最近修了：

- 数值参数字符串强转；
- `git action` list；
- `git add` 多路径；
- `commit -m` 字面量；
- `search_files` 的 `pattern` 别名。

这些不是“模型不听话”，而是工具边界没有做**参数规范化和协议校验**。参考实现把这类问题放在工具层，而不是在 prompt 里反复请求模型“请用正确参数”。

### 2.5 上下文压缩

- LangGraph 用 state + checkpointer；压缩后仍然保留消息协议。
- Aider 用 repo map 控制上下文，而不是把所有历史塞进去。
- Xenon 的 0-LLM 结构化压缩方向是对的，但压缩必须保证：
  - tool_call / tool_result 原子组不拆开；
  - `result_id` 可回查；
  - TaskState 不属于对话历史，不被压缩掉。

`compact` 后 TaskState 的恢复现在已有测试覆盖；下一步应验证压缩后 **native tool call 协议**仍完整。

---

## 3. 我们最可能错的地方（按优先级）

### P0：Plan-Execute 没有 replanner

真实证据：

- 3 轮 #26 切片，工具 60/51/64 次，失败 18/20/15 次；
- 第 1 轮空测试提交；
- 第 2 轮只改 JSON；
- 第 3 轮源码有改但测试不存在、未提交；
- 修正后 TaskState 保持 active，不再假完成，但任务仍未完成。

参考实现中，Plan-and-Execute 本身也没有 replanner，所以不能拿 LangChain 当“成功案例”；  
真正的编码 Agent（ReAct 主循环 + planning interval / reflection）会在失败后继续迭代。  
**建议**：给 Plan-Execute 加 `max_replans=3`：某一步失败或连续无进展时，用已完成结果 + 失败 Observation 重新规划剩余步骤；不要把“事后补救”当唯一机制。

### P0：终止结果没有统一类型

现在 ReAct/PlanExecute 返回字符串，失败、循环、deadline、交付未完成都靠 warning 判断。  
**建议**新增：

```python
class TerminationReason(Enum):
    FINAL = "final"
    MAX_STEPS = "max_steps"
    DEADLINE = "deadline"
    LOOP = "loop"
    NO_PROGRESS = "no_progress"
    USER_INTERRUPT = "user_interrupt"
    DELIVERY_INCOMPLETE = "delivery_incomplete"
    ERROR = "error"
```

引擎返回 `AgentResult(answer, reason, steps, tools, artifacts, tests, commit)`；  
字符串 API 保留为兼容包装。这样调用方/测试/UI 才能知道“到底是完成还是失败”。

### P0：验证必须来自工具结果，不是命令正则

把 `test/lint` 的 `exit_code`、stdout/stderr 摘要、执行前后的文件 hash 存进 ToolResult；  
`TaskState` 只接受：

- 成功执行且退出码为 0 的测试命令；
- 实际写入且内容 hash 变化的源码文件；
- commit 输出中的当前 commit hash + 该 commit 包含的路径。

不再用 `_VERIFY_RE` / `_COMMIT_HASH_RE` 猜。

### P1：循环检测增加“状态指纹”

当前 LoopDetector 只看文本哈希/工具名模式。  
建议每轮增加：

```text
workspace_diff_hash
tool_result_ids
new_artifacts
test_status
```

如果连续 N 轮这些状态都没变化，才算 no-progress；文本不同但状态相同也应熔断。  
这比提高文本相似度阈值更可靠。

### P1：最终答案校验

参考 smolagents `final_answer_checks` / Aider lint/test：

- 对代码任务，final 前必须有成功的测试结果；
- 对文件声称，必须有实际写入路径；
- 对 commit 声称，必须有当前 commit 内的源码路径；
- 校验失败时进入 reflection/replan，而不是直接输出。

### P1：默认预算需要重新决策

参考实现默认都是有限步数（15/20/25）。  
Xenon 为了长任务改成 unlimited，但代价是没有 token 预算和明确的兜底。  
建议至少：

- `max_iterations=0` 保留为显式 opt-in；
- 默认给一个较大的软预算（例如 60-80）+ 1800s + token/cost 上限；
- UI 显示当前终止原因和预算消耗。

这一步涉及产品取舍，应先确认，不直接改默认。

### P2：计划与执行解耦

参考实现中，计划步骤通常只描述“做什么”，参数在运行时由执行器生成。  
Xenon 的 Plan-Execute 预生成 `params`，再靠 mini-ReAct 修复，属于结构性摩擦。  
建议：

- 计划步骤只保留 `tool`（可为空）和 `task`；
- `params` 为空时由执行器现场决定；
- 只有确定性步骤才允许预绑定参数。

---

## 4. 建议的改造顺序

1. **先加 `AgentResult` / `TerminationReason`**，不改行为，只把现有退出路径标注清楚。
2. **Plan-Execute 加 replanner**，用真实 #26 三轮失败做回归；要求“计划失败 -> 重规划 -> 实际写入/测试 -> 才允许 complete”。
3. **TaskState 改为消费结构化 ToolResult**，删掉命令正则推断。
4. **LoopDetector 加状态指纹**，保留文本检测作为辅助。
5. **final_answer_checks**：测试/文件/commit 三类硬校验。
6. **再讨论默认预算**是否从 unlimited 改为有限软预算。

---

## 5. 不要照搬的地方

- 不要直接引入 LangGraph 作为运行时依赖来“换皮”；我们的问题在状态契约和失败语义，不在缺少图库。
- 不要把 LangChain 的经典 `PlanAndExecute` 当成功模板：它同样没有 replanner。
- 不要把“无限步数 + 文本循环检测”当成生产级终止策略；成熟实现都有显式预算/递归限制。
- 不要用 prompt 修复工具参数类型问题；工具边界必须自己规范化。

---

## 6. 一句话结论

> 我们的引擎不是“差一点提示词”，而是缺少成熟 Agent 的三样东西：
> **显式状态机、显式终止原因、基于真实工具结果的验收。**
>
> 先补这三样，再谈模型能力和提示词。
