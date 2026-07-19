# 从零构建 AI Agent：6 个让我重新理解"智能体"的工程决策

> 19K 行 Python，12 家模型商，8 种推理范式，1000+ 测试。这不是一篇"我做了个 AI 编程助手"的软文——这是我在构建过程中遇到的 6 个非直觉工程问题，以及为什么标准答案不总是对的。

---

## 为什么写这篇文章

2026 年，AI Agent 框架已经多到让人麻木。LangChain、CrewAI、AutoGPT……每个都号称"几行代码搭 Agent"。但当你真正想理解一个 Agent 的**内部机制**——不是怎么用，而是怎么造——会发现大多数框架把复杂度藏在了抽象层后面。

我花了几个月从零写了一个终端 AI Agent（[Xenon](https://github.com/xianyu-sheng/Xenon)），不是为了跟 Claude Code 竞争，是为了把 Agent 核心机制**做透、看懂**。这篇文章分享 6 个关键设计决策。

---

## 决策 1：引擎是类，不是 Prompt

**直觉做法**：切换 Agent 行为 = 换 system prompt。加一句"你要先做计划再执行"就行。

**实际问题**：Prompt 只能影响 LLM **输出内容**，不能改变**控制流**。

ReAct 需要 `observe → think → act → observe` 循环，Plan-Execute 需要 `decompose → topological_sort → parallel_execute → synthesize`，Reflection 需要 `execute → critic_review → revise → critic_review` 双模型多轮。

这三个的**代码结构完全不同**。把它们压在 prompt 里，等于让 LLM 自己管理控制流——而 LLM 最不擅长的就是保持状态一致性。

```
xenon/engine/
├── direct_engine.py           # 一条路走到黑
├── react_engine.py            # 循环直到 finish
├── plan_execute_engine.py     # DAG + 拓扑排序
├── reflection_engine.py       # 双模型互审
├── novel_engine.py            # 创意写作专用
├── plan_react_engine.py       # 计划 → ReAct
├── plan_reflection_engine.py  # 计划 → 审查
└── react_reflection_engine.py # ReAct → 审查
```

每个引擎是**独立的类**，有自己的 `run()` 方法。切范式不是改 prompt 文本——是 `EngineFactory.create(mode)` 换一个类。

---

## 决策 2：断路器不应该"一次挂、次次挂"

**直觉做法**：工具调用失败 → 捕获异常 → 返回错误给 LLM → LLM 决定下一步。

**实际问题**：LLM 会反复用同样的参数调同一个失败的工具。你见过 LLM 在循环里连续 5 次 `read_file("不存在的路径")` 吗？我见过。

标准答案是加个重试装饰器。但 Agent 的真正问题是：**有些失败是可恢复的（网络抖动），有些是终局性的（文件不存在）。重试机制不分青红皂白统一退避，浪费 token 和时间。**

我做的：

```python
# 每个工具有独立的断路器实例
breaker = CircuitBreaker(
    failure_threshold=3,      # 3 次连续失败 → 熔断
    cooldown_seconds=30,      # 30s 后进 half_open
    backoff_multiplier=2,     # 再失败冷却翻倍，上限 600s
)
```

关键不是"熔断"本身——是这个状态**跨 LLM 轮次持久化**。`GLOBAL_BREAKERS` 字典在 Agent 的整个生命周期存活。工具 A 在第 2 轮被熔断，第 3 轮 LLM 想再调它时直接返回"工具不可用"，不浪费 API 调用。

---

## 决策 3：上下文压缩不是"取最后 N 条"

**直觉做法**：对话太长 → 只保留最近 10 条消息。

**实际问题**：最近 10 条可能全是工具输出（`read_file` 返回的 200 行代码），而 15 条之前的 user prompt 才是真正需要的语义信息。

我实现的是 **6 步压缩流水线**：

```
1. 摘要     → LLM 生成对话摘要
2. 精简     → 工具输出超过 20 行部分用 [N lines truncated] 替换
3. 去重     → 连续相同的工具调用合并为 "[N repeated calls]"
4. 评分     → 按语义密度给每条消息打分
5. 裁剪     → 保留 Top-N 条高分消息
6. 重组     → 摘要 + 裁剪结果拼回 messages 列表
```

触发条件不是"消息数 > N"，而是 **Token 窗口达 80%**（实际用 tiktoken 计数）。这个阈值很重要——消息数跟模型无关（GPT-4o 128K vs Claude 200K），但 Token 比是通用指标。

---

## 决策 4：花钱也要分阶段

**直觉做法**：Agent 每轮都可以调所有工具。

**实际问题**：Agent 在任务尾声还在调 `list_files` 和 `search_files`——它在"探索"，但任务早已进入收尾阶段。Token 被浪费在无意义的浏览上。

解决：**三阶段软预算**。

| 阶段 | Token 配额 | 可用工具 |
|------|-----------|---------|
| EXPLORE | 25% | 全部 20 个 |
| EXECUTE | 50% | 全部 20 个 |
| CONVERGE | 25% | **禁用** 7 个纯探索工具 |

收束阶段禁用的 7 个工具包括 `list_files`、`search_files`、`code_index`、`ast_analyze` 等——都是"看"但不会"改"的工具。Agent 到这个阶段应该已经在执行最终方案，而不是继续翻文件。

---

## 决策 5：MCP 子进程管理不是 `subprocess.run`

**直觉做法**：MCP 服务器 = `subprocess.run` 起一个进程，stdio 通信。

**实际问题**：

1. `readline()` 会**无限阻塞**——MCP 服务器不发新消息时，Agent 主循环整个卡住
2. 进程崩溃后变**僵尸**——`subprocess.run` 不会自动回收
3. MCP 服务器挂了，Agent 不知道——下次调 MCP 工具才报错，但上下文已丢

解决：

```python
# 关键：select + 墙钟超时，不用 readline
ready, _, _ = select.select([process.stdout], [], [], timeout=1.0)
if ready:
    line = process.stdout.readline()
else:
    if process.poll() is not None:
        raise MCPProcessDied(process.returncode)

# 进程退出：terminate() + 兜底 kill()
process.terminate()
try:
    process.wait(timeout=3)
except subprocess.TimeoutExpired:
    process.kill()
    process.wait()
```

守护进程崩溃自动重启（最多 3 次），重启间隔指数退避。

---

## 决策 6：空输入检测不能只靠 LLM

**直觉做法**：LLM 返回了就展示给用户。

**实际问题**：LLM 有时候会返回一段**看起来完整、实则空洞**的回答。比如：

> "我已经完成了任务，文件已更新，代码运行正常。"

——但实际没改任何文件，也没跑任何命令。

HollowDetector 用 **15 个正则 + 组合判定**来识别空洞回答。不是靠 LLM 自检（让 LLM 检查 LLM 的回答是否空洞？），而是靠模式匹配 + 工具调用记录交叉校验。检出后强制重试，注入更具体的指令："请确认你实际执行了哪些操作，如果没有，现在执行。"

---

## 这些决策的通用性

上面 6 个设计决策**不绑定 Xenon**。无论你用什么框架——LangChain、CrewAI、自研——只要你的 Agent 需要：

- 多种推理模式 → 把范式差异放在控制流层，不要全压在 prompt 上
- 可靠执行 → 加断路器，且状态要跨轮次持久化
- 长对话 → 上下文压缩要按语义密度裁剪，不是简单截断
- 预算控制 → 分阶段限制可用工具，收束阶段裁掉探索型工具
- MCP 集成 → 用 select + 超时而不是 readline
- 输出质量 → 空洞检测基于模式匹配，不要靠 LLM 自检

---

## 项目地址

GitHub: <https://github.com/xianyu-sheng/Xenon>

19K 行 Python，MIT 开源，1000+ 测试。如果你也在学习 Agent 架构，欢迎阅读源码、提 issue、交 PR。

---

*这篇文章是"从零构建 AI Agent"系列的第一篇。下一篇计划写《ReAct 引擎的 7 阶段工具执行门面：为什么参数校验要放在 LLM 调用之前》。*
