# OmniAgent

**不只是 AI 编程助手——是一个可学习、可定制的多模型 AI Agent 调度引擎。**

[![CI](https://github.com/xianyu-sheng/omniagent/actions/workflows/ci.yml/badge.svg)](https://github.com/xianyu-sheng/omniagent/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)]()
[![MIT License](https://img.shields.io/badge/license-MIT-green.svg)]()
[![Tests](https://img.shields.io/badge/tests-1000%2B-brightgreen.svg)]()
[![HumanEval](https://img.shields.io/badge/HumanEval-87.8%25_(144%2F164)-success.svg)](https://github.com/xianyu-sheng/omniagent/blob/ubutnu/evals/humaneval_runner.py)

![OmniAgent terminal demo](docs/assets/terminal-demo.svg)

> 12 家模型商、8 种推理范式、MCP 协议、断路器、上下文压缩——一个终端 Agent 该有的工程机制，这里都有。
> 19K 行 Python，1000+ 测试，MIT 开源。**适合想深入理解 Agent 架构的开发者阅读、修改、二次开发。**

---

## 这个项目是什么——以及不是什么

**OmniAgent 不是** Claude Code 或 Aider 的竞品。它不追求在 SWE-bench 上刷榜，也不试图说服你换掉现有的编程助手。

**OmniAgent 是**一个把 AI Agent 核心机制做齐、做透的参考实现。如果你想理解：

- ReAct / Plan-Execute / Reflection 到底怎么实现
- MCP 协议如何在真实 Agent 中集成
- 断路器和预算管理器如何防止 Agent "跑飞"
- 多模型路由和自动降级怎么设计

——那这个项目就是写给你的。

---

## 架构一览

```
┌─────────────────────────────────────────────────────────┐
│                    Terminal REPL (TUI)                    │
│  粘贴模式 · CJK 宽字符 · 状态栏 · 多行编辑 · 斜杠命令      │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│                   Agent 调度层                            │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐   │
│  │  Direct  │ │  ReAct   │ │  Plan-   │ │Reflection│   │
│  │          │ │          │ │ Execute  │ │          │   │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘   │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐   │
│  │Plan+React│ │Plan+Refl │ │React+Refl│ │  Novel   │   │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘   │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│                   工程可靠性层                             │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐   │
│  │Compactor │ │ Budget   │ │ Circuit  │ │  Hollow  │   │
│  │6步压缩器 │ │ Manager  │ │ Breaker  │ │ Detector │   │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘   │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│                   工具执行层                              │
│  ┌──────────────────────────────────────────────────┐   │
│  │        ToolExecutor — 7 阶段门面 + 参数校验       │   │
│  │  read_file · write_file · edit_file · command    │   │
│  │  git · web_fetch · search · MCP · 20 项内置工具   │   │
│  └──────────────────────────────────────────────────┘   │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│                   模型路由层                              │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌──────────┐      │
│  │DeepSeek │ │ OpenAI  │ │ Claude  │ │  Ollama  │ ...  │
│  └─────────┘ └─────────┘ └─────────┘ └──────────┘      │
│         12 家 provider · 断路器自动降级 · 长连接池复用     │
└─────────────────────────────────────────────────────────┘
```

---

## Quick Start

```bash
git clone https://github.com/xianyu-sheng/omniagent.git
cd omniagent
pip install -e ".[dev]"
omniagent
```

进 REPL 后三步上手：

```text
You: /setup                              # 配 API key
You: /set_model deepseek/deepseek-v4-pro # 选模型
You: 帮我检查 tests 失败原因并给出修复方案   # 开干
```

或一行命令直接跑：

```bash
omniagent chat -m deepseek/deepseek-v4-pro "review this diff"
```

---

## 值得读源码的 6 个设计决策

这些是你在"调 API 就行"的项目里看不到的东西：

### 1. 三种 Agent 范式，不是三种 Prompt

项目没有把"范式差异"压在 system prompt 上。`Direct`、`ReAct`、`PlanExecute`、`Reflection` 各自是独立的引擎类，有不同的控制流。切范式不是改 prompt 文本——是换引擎。

```python
# omniagent/engine/ 下 6 个独立引擎 + 2 个组合引擎
react_engine.py          # ReAct: observe → think → act 循环
plan_execute_engine.py   # PlanDAG: 拓扑排序 + 并行执行
reflection_engine.py     # 执行者 + 审查者双模型多轮
```

### 2. MCP 集成不是简单 wrapper

原生支持 stdio 和 SSE 双传输。子进程用 `select` + 墙钟超时（不是 `readline` 无限阻塞），进程退出用 `terminate()` + 兜底 `kill()` 防僵尸。守护进程崩溃自动重启（最多 3 次）。

### 3. 断路器不是装饰器

每个工具有独立的断路器实例。**3 次连续失败触发熔断，30s 冷却**（half_open 失败翻倍，上限 600s）。`GLOBAL_BREAKERS` 跨 run 累积——不是"这次挂了下次还让它挂"。

### 4. 上下文压缩是 6 步流水线

不是简单的"取最后 N 条消息"。在 Token 窗口达 80% 时触发，经历：摘要 → 工具输出精简 → 去重 → 评分 → 裁剪 → 重组，保留语义最密集的内容。

### 5. BudgetManager 分三阶段花钱

不是所有工具同等对待。`EXPLORE(25%) → EXECUTE(50%) → CONVERGE(25%)`，收束阶段禁用 7 个纯探索型工具，防止 Agent 在任务尾声无意义地翻文件。

### 6. 多模型路由带降级

模型调用失败时不是直接报错。断路器感知的自动降级：模型 A 熔断 → 试模型 B → 试模型 C。Provider 优先级可配，per-provider httpx 长连接池复用。

---

## 20 项内置工具

| 类别 | 工具 |
| --- | --- |
| 文件 | `read_file` / `write_file` / `edit_file` / `edit_with_llm` / `batch_write` / `batch_edit` / `diff_preview` |
| 检索 | `search_files` / `code_index` / `ast_analyze` / `list_files` |
| 命令 | `command`（SSRF 拦截、命令注入收口、敏感路径黑名单） |
| Git | `git`（危险命令拦截） |
| 网络 | `web_fetch`（SSRF 黑名单 + 安全域名白名单）/ `github_fetch` |
| 时间 | `datetime` |
| 动态 | `register_tool`（模式 2 only，RCE 收敛） |
| MCP | `mcp_call` — 调用通过 `/mcp add` 注册的外部 MCP 服务器 |

---

## 模型商支持

| Provider | 模型示例 |
| --- | --- |
| DeepSeek | v4-pro, v4-flash, chat, coder, reasoner |
| OpenAI | gpt-4o, gpt-4o-mini, gpt-4-turbo, o1-preview |
| Anthropic | claude-sonnet-4, claude-3.5-sonnet, claude-3-opus |
| Google | gemini-2.0-flash, gemini-1.5-pro |
| 智谱 | glm-4-plus, glm-4-flash |
| 通义千问 | qwen-max, qwen-plus, qwen-turbo |
| Kimi | moonshot-v1-128k, moonshot-v1-32k |
| 百川 | Baichuan4, Baichuan3-Turbo |
| MiniMax | abab6.5s-chat |
| 小米 MiMo | mimo-v2.5-pro |
| Ollama | llama3, codellama, qwen2.5, mistral（本地） |

---

## 测试与评测

```bash
# 全量单元测试（1000+ 用例）
pytest tests/ --ignore=tests/test_repl_real_usage.py

# 混沌测试（31 用例：网络中断、格式错误、限流、工具异常等）
pytest tests/chaos/

# Eval 框架（mock 模式 100%，real 模式需 API key）
python evals/runner.py --mode mock
python evals/runner.py --mode real --model deepseek/deepseek-v4-pro

# HumanEval 基准（164 题 Python 函数补全）
python evals/humaneval_runner.py --model deepseek/deepseek-v4-pro --num-tasks 164
```

| 评测 | 结果 |
|------|------|
| HumanEval pass@1 (deepseek-v4-pro) | **144/164 (87.8%)** |
| 单元测试 | 1000+ 通过 |
| 混沌测试 | 31/31 通过 |


---

## 安全

- API key 存 `~/.omniagent/credentials.yaml`（`chmod 0600`），不入仓库
- 文件编辑前 `Confirm.ask` 显示 diff
- `command` / `git` 危险操作拦截或显式确认
- 敏感路径 / 凭证文件名黑名单
- `web_fetch` SSRF 黑名单（IPv4 私有网 / IPv6 ULA / 数字编码 IP / 重定向）+ 公共 API 白名单
- `register_tool` 模式 1（任意 Python 导入）= RCE 收敛；模式 2（结构化参数）保留

---

## 文档

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — 8 种引擎切换图 + 路由层 + 可靠性三件套
- [`docs/COMPARISON.md`](docs/COMPARISON.md) — vs Aider / Claude Code / OpenCode / Crush
- [`docs/OPERATION_GUIDE.md`](docs/OPERATION_GUIDE.md) — REPL 命令手册
- [`docs/omniagent-design-spec-v1.1.html`](docs/omniagent-design-spec-v1.1.html) — 设计文档 v1.1
- [`docs/reports/v0.2.2/`](docs/reports/v0.2.2/) — 端到端测试报告

---

## 适合谁看

| 如果你…… | 你能从这里学到 |
| --- | --- |
| 想理解 ReAct/Plan-Execute 的实现细节 | 6 个独立引擎类的控制流差异 |
| 在给自己的项目加 MCP 支持 | stdio + SSE 双传输的完整实现 |
| 想知道 Agent 怎么防止"跑飞" | 断路器 + BudgetManager + HollowDetector |
| 在做多模型路由 | 12 provider 的统一抽象 + 自动降级 |
| 想写一个 TUI | 粘贴模式 + CJK 宽字符 + 状态栏的终端处理 |

---

## License

MIT — see [LICENSE](LICENSE).

## Credits

- [Rich](https://github.com/Textualize/rich) — terminal UI
- [httpx](https://github.com/encode/httpx) — HTTP client
- [PyYAML](https://github.com/yaml/pyyaml) — YAML parsing
