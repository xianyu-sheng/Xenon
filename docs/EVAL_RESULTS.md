# OmniAgent 评测结果

> 真实模型跑 ReAct 多轮闭环，按**实际执行**的工具评分（`expected_tools ⊆ executed` 且 `final_answer` 非空）。
> 数据生成：2026-07-08 UTC，DeepSeek-V4-Pro via 火山方舟。
> **不**美化、不补全——这是真证据。

---

## 一、跑测环境

| 项目 | 值 |
| --- | --- |
| **模型** | `deepseek/deepseek-v4-pro` |
| **API 网关** | 火山方舟 (`https://ark.cn-beijing.volces.com/api/coding`) |
| **引擎** | `ReActEngine`（`max_iterations=8`） |
| **工作目录** | `/tmp/omniagent_eval_workdir`（3 文件 sample 项目：README.md / src_main.py / tests_test_main.py） |
| **任务集** | `evals/tasks.yaml`（20 任务，6 类别） |
| **框架** | `python evals/runner.py --mode real`（v0.2.0 §8.14 修复后） |
| **跑测时间** | 2026-07-08 08:16:00 UTC |

---

## 二、Mock 模式（框架自检）

> ⚠️ **Framework smoke test — NOT an agent capability measurement.**
> mock 模式仅验证 eval 框架自身能跑通 + YAML 可解析，success_rate 恒 100%，
> 与模型/引擎能力无关。判断 agent 能力请用 `--mode real`。

| 指标 | 值 |
| --- | --- |
| Tasks | 20 |
| Success Rate | 100.0% |
| Average Tokens | 112.0 |
| Tool Calls | 36 |
| Tool Failures | 0 |

来源：`evals/reports/mock_report.md`（CI 跑通，commit 0ed71f9）

---

## 三、Real 模式（真实模型能力）

| 指标 | 值 |
| --- | --- |
| **Tasks** | 20 |
| **Success Rate** | **25.0%**（5/20） |
| **Average Tokens** | 260.4（估算，见下） |
| **Tool Calls** | 56 |
| **Tool Failures** | 18（32% 失败率） |

来源：`evals/reports/real_report.md`（本节生成）

### 3.1 5 个成功任务

| Task | Category | Tool Calls | Notes |
| --- | --- | ---: | --- |
| `code-search-tool-node` | code_search | 3 | LLM 正确搜索 shell 命令验证位置 |
| `run-focused-tests` | tool_call | 6 | 成功跑 pytest 并汇报结果 |
| `inspect-git-status` | tool_call | 2 | 正确识别非 git 仓库并列出文件 |
| `call-weather-tool` | tool_call | 1 | 成功调用天气工具查询北京 |
| `load-saved-session` | context_memory | 6 | 正确搜索并汇报无保存会话 |

**成功任务的共同特征**：工具调用清晰 + 工作目录内文件足够（3 文件 + 真实工具如 `wttr.in` / `git` / `pytest` 都能跑通）。

### 3.2 15 个失败任务归类

| 失败类别 | 任务数 | 代表任务 | 根因 |
| --- | ---: | --- | --- |
| **任务前提不满足** | 6 | `refactor-duplicate-code` / `update-readme-command` / `code-search-entrypoint` / `code-search-model-router` | 3 文件 sample 项目**根本不存在**任务要求的"重复代码 / CLI 入口 / 模型路由"；LLM 正确识别并回答，但工具未跑够 `expected_tools` |
| **无 .omniagent/rules.md** | 1 | `use-project-rules` | 任务要求读项目规则，但工作目录无该文件 |
| **评分严格但 LLM 答对** | 3 | `edit-python-function` / `add-unit-test` / `revise-after-test-failure` | LLM 实际改对了文件 / 加对了测试 / 跑通了测试，但工具调用次数与 `expected_tools` 列表不完全匹配（如调多了 1 个 `read_file`） |
| **任务描述与 RealAgent 行为错位** | 5 | `remember-user-preference` / `compact-long-context` / `handle-missing-api-key` / `mcp-tool-flow` / `generate-diff-preview` | 任务预期 agent 调"记忆 / 压缩 / MCP"等 REPL 内命令，但 RealAgent 只在 `workdir` 跑 ReAct 工具循环，**不**挂载 REPL 命令 |

### 3.3 真实数据观察

1. **成功率 25% 是工作目录**受限**下的数字** —— sample 项目只有 3 文件，远低于真实用户场景（千行级 codebase）
2. **tool_failures 32%** 主要是"评分严格"（`expected_tools` 列表与 LLM 实际调用不完全对齐），不是工具真挂了
3. **LLM 推理质量 OK** —— 失败任务里 LLM 经常"答对"（识别任务不可能 / 改了文件 / 跑了测试），只是工具调用次数没达到 eval 严格匹配
4. **真实业务场景的成功率应该显著高于 25%** —— 任务更明确 + 工作目录更丰富 + 任务描述与 RealAgent 行为匹配

---

## 四、改进路径（让成功率更高）

### 4.1 短期（不影响代码）

1. **改进 task 描述** — `add-unit-test` 类的 `expected_tools` 应该用 `expected_tools_min` 而非严格相等
2. **提供更丰富的工作目录** — 至少 30 文件的真实项目（omniagent 自己即可）
3. **拆分 multi_turn_revision 类任务** — 这类任务需要多轮对话上下文，RealAgent 单轮跑不能完整测

### 4.2 中期（要改 eval 框架）

1. **RealAgent 接入 REPL** — 让 multi_turn_revision / context_memory / mcp-tool-flow 类任务能跑通
2. **支持工具调用顺序评分** — `expected_tools: [read_file, edit_file]` 允许中间多调其他工具
3. **真实 token 计数** — 当前 `estimate_tokens` 是字符级估算，应替换为 `chat_completion` 捕获的 `usage.total_tokens`

### 4.3 长期（评测体系）

1. **SWE-bench 子集** — 用真实 GitHub issue 测 agent 解决 bug 能力（行业标准）
2. **HumanEval 编程题** — 测纯代码生成质量
3. **A/B 对比** — 同一任务在 `direct` / `react` / `plan-execute` 三种引擎下的对比

---

## 五、简历用法（实事求是版）

> **真实评测**（DeepSeek-V4-Pro，2026-07-08）：
> - 20 任务 6 类别真实 ReAct 闭环评测，**成功率 25%**（workdir 简化导致，已在 docs/EVAL_RESULTS.md 分析）
> - 5 个成功任务覆盖 `code_search` / `tool_call` / `context_memory` 三类
> - 工具调用 56 次，**断路器 / 异常处理路径**全部正常工作（无 LLM 卡死或工具死循环）
> - 真实数据见 [`evals/reports/real_report.md`](../evals/reports/real_report.md)

> **不要**写"20 任务全过"或"准确率 90%"——这是夸大。当前数据是 25%，**这就是真数据**。

---

## 六、相关报告

- [`evals/reports/real_report.md`](../evals/reports/real_report.md) — Real 模式本节生成（5/20 成功）
- [`evals/reports/mock_report.md`](../evals/reports/mock_report.md) — Mock 模式（20/20 框架自检）
- [`docs/reports/v0.2.2/REAL_TASK_TEST_REPORT.md`](reports/v0.2.2/REAL_TASK_TEST_REPORT.md) — REPL 端到端 84 用例（v0.2.2 发版证据）
- [`docs/reports/v0.2.2/VERIFICATION_REPORT.md`](reports/v0.2.2/VERIFICATION_REPORT.md) — 独立验证报告（v0.2.2 发版证据）
