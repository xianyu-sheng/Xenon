# OmniAgent

**不只是 AI 编程助手——是一个可学习、可定制的多模型 AI Agent 调度引擎。**

[![CI](https://github.com/xianyu-sheng/omniagent/actions/workflows/ci.yml/badge.svg)](https://github.com/xianyu-sheng/omniagent/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)]()
[![MIT License](https://img.shields.io/badge/license-MIT-green.svg)]()
[![Tests](https://img.shields.io/badge/tests-1110%2B-brightgreen.svg)]()
[![HumanEval](https://img.shields.io/badge/HumanEval_Pass@1-88.4%25_(official)-success.svg)](https://github.com/openai/human-eval)
[![v0.5.2](https://img.shields.io/badge/version-0.5.2-orange.svg)](https://github.com/xianyu-sheng/omniagent/releases)

![OmniAgent terminal demo](docs/assets/terminal-demo.svg)

> 12 家模型商、8 种推理范式、MCP 协议、断路器、上下文压缩——一个终端 Agent 该有的工程机制，这里都有。
> 23K 行 Python，1110+ 测试，MIT 开源。**适合想深入理解 Agent 架构的开发者阅读、修改、二次开发。**

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

## 目录

- [快速开始](#快速开始)
- [架构一览](#架构一览)
- [功能特性](#功能特性)
- [20 项内置工具](#20-项内置工具)
- [模型商支持](#模型商支持)
- [命令参考](#命令参考)
- [测试与评测](#测试与评测)
- [安装详解](#安装详解)
- [配置指南](#配置指南)
- [安全机制](#安全机制)
- [FAQ](#faq)
- [故障排查](#故障排查)
- [文档](#文档)
- [设计决策（源码导读）](#值得读源码的-6-个设计决策)
- [适合谁看](#适合谁看)
- [贡献](#贡献)
- [License](#license)
- [Credits](#credits)

---

## 快速开始

### 前置条件

- **Python 3.10+**
- **API Key**：至少一个模型商的 API Key（DeepSeek / OpenAI / Anthropic 等）

### 30 秒上手

```bash
git clone https://github.com/xianyu-sheng/omniagent.git
cd omniagent
pip install -e ".[dev]"
omniagent
```

进 REPL 后三步完成配置和首次使用：

```text
> /setup                              # 配 API key + 选模型（自动加入调用池）
> 你好                                 # 自动根据任务难度选模型
> 帮我写一个快速排序的核心算法              # 复杂任务自动切旗舰模型
```

### 一行命令模式

```bash
# 直接对话
omniagent chat -m deepseek/deepseek-v4-pro "review this diff"

# 查看可用模型
omniagent chat --list-models

# 指定引擎范式
omniagent chat --engine react "帮我排查这个 bug"
```

### 验证安装

```bash
# 确保一切正常
python3 -m pytest tests/ -q
# 1110 passed — 安装成功！
```

---

## 架构一览

```
┌─────────────────────────────────────────────────────────┐
│                    Terminal REPL                          │
│  prompt_toolkit 输入 · Rich 渲染 · 状态栏 · 斜杠命令       │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│                   Agent 调度层（8 种范式）                  │
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
│                   工具执行层（20 项工具）                    │
│  ┌──────────────────────────────────────────────────┐   │
│  │        ToolExecutor — 7 阶段门面 + 参数校验       │   │
│  │  command · read_file · write_file · edit_file    │   │
│  │  git · web_fetch · MCP · 20 项内置工具            │   │
│  └──────────────────────────────────────────────────┘   │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│                   模型调度层 (v0.5)                        │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐   │
│  │ 5级优先级 │ │ 工作窃取 │ │Benchmark │ │ 断路器   │   │
│  │ 队列 Q1-5│ │ 调度算法 │ │ Fetcher  │ │ 健康追踪 │   │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘   │
│  ┌──────────────────────────────────────────────────┐   │
│  │  DeepSeek · OpenAI · Claude · Ollama · 豆包 ...  │   │
│  │         12 家 provider · 动态注册 · 长连接池      │   │
│  └──────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

---

## 功能特性

### 推理范式（8 种）

| 范式 | 引擎文件 | 说明 |
|------|---------|------|
| **Direct** | `engine/base.py` | 单轮问答，无工具调用 |
| **ReAct** | `engine/react_engine.py` | Observe → Think → Act 循环 |
| **Plan-Execute** | `engine/plan_execute_engine.py` | 先规划 DAG → 再拓扑执行 |
| **Reflection** | `engine/reflection_engine.py` | 执行者 + 审查者双模型多轮 |
| **Novel** | `engine/novel_engine.py` | 小说创作引擎（长文本生成） |
| **Plan+React** | `engine/combined_engines.py` | 规划后逐步 ReAct |
| **Plan+Reflection** | `engine/combined_engines.py` | 规划后反思审查 |
| **React+Reflection** | `engine/combined_engines.py` | 执行后即时反思 |

切换方式：`Shift+Tab`（终端快捷键）或 `/mode <范式名>`

### 模型调度 (v0.5)

- **5 级优先级队列 (Q1-Q5)**：模型按能力自动分层，旗舰模型在 Q1，轻量模型在 Q5
- **工作窃取调度**：高优先级任务可借用低优先级队列的模型
- **AutoRouter**：根据任务难度自动选择合适模型（节省成本）
- **BenchmarkFetcher**：新模型注册时自动查 HuggingFace Leaderboard 定级
- **断路器感知降级**：模型 A 熔断 → 自动试模型 B → 试模型 C
- **长连接池**：per-provider httpx 连接池复用

### 工程可靠性

| 组件 | 文件 | 作用 |
|------|------|------|
| **Compactor** | `engine/context.py` | 6 步压缩流水线：摘要 → 精简 → 去重 → 评分 → 裁剪 → 重组 |
| **BudgetManager** | `engine/budget.py` | 三阶段预算：EXPLORE(25%) → EXECUTE(50%) → CONVERGE(25%) |
| **CircuitBreaker** | `engine/circuit_breaker.py` | 3 次连续失败熔断，30s 冷却，half-open 失败翻倍（上限 600s） |
| **HollowDetector** | `engine/hollow_detector.py` | 检测"空洞输出"——Agent 看似在工作但实际无进展 |

### MCP 协议 (Model Context Protocol)

- **双传输**：stdio（子进程）和 SSE（HTTP 长连接）
- **进程管理**：`select` + 墙钟超时，`terminate()` + 兜底 `kill()` 防僵尸
- **自动恢复**：守护进程崩溃自动重启（最多 3 次）
- **外部工具集成**：通过 `/mcp add` 注册外部 MCP 服务器

### REPL 体验

- **prompt_toolkit 输入**：`> ` 提示符 + 命令/路径/模型名三级补全
- **Rich 渲染**：Markdown 面板、语法高亮代码块、OSC-8 可点击路径
- **状态栏**：实时显示模型、Token 用量、消息数、延迟
- **斜杠命令**：38 条内置命令（见[命令参考](#命令参考)）
- **会话管理**：`/save` `/load` `/resume`，支持跨终端恢复

---

## 20 项内置工具

| 类别 | 工具 | 说明 |
|------|------|------|
| 文件读写 | `read_file` | 读取文件内容 |
| | `write_file` | 创建或覆写文件 |
| | `edit_file` | 精确字符串替换编辑 |
| | `batch_write` | 批量写入多个文件 |
| | `batch_edit` | 批量编辑多个位置 |
| | `create_directory` | 创建目录结构 |
| | `diff_preview` | 预览修改的 diff |
| 代码检索 | `search_files` | 按文件名/内容搜索 |
| | `list_files` | 列出目录结构 |
| | `code_index` | 代码符号索引查询 |
| | `ast_analyze` | Python AST 静态分析 |
| 代码变换 | `refactor` | 结构化代码重构 |
| 命令执行 | `command` | 终端命令执行（SSRF 拦截 + 命令注入收口） |
| 版本控制 | `git` | Git 操作（危险命令拦截） |
| 网络请求 | `web_fetch` | 网页抓取（SSRF 黑名单 + 安全域名白名单） |
| | `github_fetch` | GitHub API 请求 |
| 工具信息 | `weather` | 天气查询 |
| | `datetime` | 日期时间查询 |
| 动态扩展 | `register_tool` | 运行时注册新工具（安全白名单模式） |
| MCP 协议 | `mcp_call` | 调用外部 MCP 服务器工具 |

---

## 模型商支持

OmniAgent 内置了 12 家模型商的 API 适配器：

| Provider | 模型示例 | 注册方式 |
|----------|---------|---------|
| **DeepSeek** | v4-pro, v4-flash, chat, coder, reasoner | 内置 |
| **OpenAI** | gpt-4o, gpt-4o-mini, gpt-4-turbo, o1-preview | 内置 |
| **Anthropic** | claude-sonnet-4, claude-3.5-sonnet, claude-3-opus | 内置 |
| **Google** | gemini-2.0-flash, gemini-1.5-pro | 内置 |
| **智谱 (Zhipu)** | glm-4-plus, glm-4-flash | 内置 |
| **通义千问 (Qwen)** | qwen-max, qwen-plus, qwen-turbo | 内置 |
| **Moonshot (Kimi)** | moonshot-v1-128k, moonshot-v1-32k | 内置 |
| **百川 (Baichuan)** | Baichuan4, Baichuan3-Turbo | 内置 |
| **MiniMax** | abab6.5s-chat | 内置 |
| **小米 MiMo** | mimo-v2.5-pro | 内置 |
| **Ollama** | llama3, codellama, qwen2.5, mistral | 本地部署 |
| **自定义** | 任意 OpenAI-compatible API | `/setup` 菜单注册 |

---

## 命令参考

### 模型管理

| 命令 | 说明 |
|------|------|
| `/setup` | 交互式配置向导（API key + 模型选择） |
| `/models` | 列出所有已注册模型及角色分配 |
| `/pool` | 查看五级优先级模型调用池 |
| `/remove_model <alias>` | 移除一个模型 |

### 范式与模式

| 命令 | 说明 |
|------|------|
| `Shift+Tab` | 切换推理范式（ReAct / Direct / Plan-Execute / ...） |
| `/stream [on\|off]` | 切换流式输出 |
| `/verbose [on\|off]` | 切换详细输出（显示思考过程和工具调用） |
| `/optimize [on\|off]` | 切换输入指令自动优化 |

### 会话管理

| 命令 | 说明 |
|------|------|
| `/save <name>` | 保存当前会话 |
| `/load <name>` | 加载已保存会话 |
| `/sessions` | 列出所有已保存会话 |
| `/resume [name]` | 恢复上次自动保存的会话 |
| `/clear` | 清空对话历史 |
| `/undo` | 回退到上一个对话状态 |

### 上下文管理

| 命令 | 说明 |
|------|------|
| `/context` | 显示当前上下文状态（Token 用量 / 消息数） |
| `/compact [摘要]` | 手动触发上下文压缩 |
| `/history [N]` | 查看路由调度历史 |

### 配置与调试

| 命令 | 说明 |
|------|------|
| `/status` | 显示详细系统状态 |
| `/config [save <path>]` | 查看或导出当前配置 |
| `/run [workflow.yaml]` | 执行工作流文件 |
| `/mcp [add\|list\|tools\|remove]` | 管理 MCP 服务器 |
| `/permissions` | 查看当前权限模式 |

### 退出

| 命令 | 说明 |
|------|------|
| `/exit`, `/quit`, `/bye` | 退出 OmniAgent |
| `Ctrl+C` | 中断当前任务（不会退出 REPL） |

---

## 测试与评测

### 运行测试

```bash
# 全量单元测试（1110 个用例）
pytest tests/ -q

# 仅运行混沌测试（31 个用例：网络中断、格式错误、限流等）
pytest tests/chaos/

# 运行评测框架
python3 evals/runner.py --mode mock    # 冒烟测试（20 个场景）
python3 evals/runner.py --mode real    # 真实 LLM 评测
```

### HumanEval 基准

使用官方 [openai/human-eval](https://github.com/openai/human-eval) 框架评测代码生成能力：

```bash
# Step 1: 用 omniagent 生成 completions
python3 -c "
from evals.humaneval_runner import load_tasks, build_prompt, extract_code
from omniagent.utils.llm_client import chat_completion
import json
tasks = load_tasks()
with open('samples.jsonl', 'w') as f:
    for t in tasks:
        resp = chat_completion('deepseek/deepseek-v4-pro',
            [{'role':'user','content': build_prompt(t)}], temperature=0.0, max_tokens=1024)
        f.write(json.dumps({'task_id': t['task_id'],
            'completion': extract_code(resp, t['entry_point'])}) + '\n')
"

# Step 2: 官方评测框架评分
evaluate_functional_correctness samples.jsonl
```

| 评测 | 结果 | 框架 |
|------|------|------|
| HumanEval pass@1 (deepseek-v4-pro) | **145/164 (88.4%)** | 官方 `openai/human-eval` |
| 单元测试 | 1110 通过 | pytest |
| 混沌测试 | 31/31 通过 | pytest |

> **关于评测的说明**：HumanEval 评测的是模型的**代码生成能力**而非 Agent 的任务完成能力。
> Agent 级别的评测（多轮工具调用、文件编辑、错误修复）请参考 `evals/runner.py --mode real`。

---

## 安装详解

### 环境要求

| 组件 | 最低版本 |
|------|---------|
| Python | 3.10+ |
| pip | 21.0+ |
| 终端 | 支持 ANSI 真彩色的现代终端（iTerm2 / Windows Terminal / Kitty / Alacritty） |

### 从源码安装（推荐）

```bash
git clone https://github.com/xianyu-sheng/omniagent.git
cd omniagent
pip install -e ".[dev]"
```

### 依赖项

```
httpx>=0.27.0       # HTTP 客户端（多模型商 API 调用）
pyyaml>=6.0         # 凭证和配置文件解析
rich>=13.0.0        # 终端 Markdown 渲染
prompt-toolkit>=3.0 # 终端输入（补全、历史、键绑定）
```

### 验证安装

```bash
omniagent --help
# 应显示命令行帮助

python3 -m pytest tests/ -q
# 应显示 1110 passed
```

---

## 配置指南

### API Key 配置

所有凭证存储在 `~/.omniagent/credentials.yaml`（自动 `chmod 0600`）：

```yaml
# 方式一：运行 /setup 交互式配置（推荐）
# > /setup
# 选择模型商 → 输入 API Key → 自动加入调用池

# 方式二：直接编辑配置文件
# ~/.omniagent/credentials.yaml
providers:
  deepseek:
    api_key: "sk-xxxxxxxxxxxxxxxx"
  openai:
    api_key: "sk-xxxxxxxxxxxxxxxx"
  anthropic:
    api_key: "sk-ant-xxxxxxxxxxxxxxxx"
```

### 模型注册

通过 `/setup` 或直接编辑 `~/.omniagent/model_registry.yaml` 注册模型：

```yaml
models:
  - alias: deepseek-v4
    model_id: deepseek/deepseek-v4-pro
    provider: deepseek
    priority: Q1
    max_tokens: 131072
```

模型注册后可通过 `/pool` 查看调用队列。

### 项目级配置

在项目根目录创建 `.omniagent/rules.md` 可指定项目规则，Agent 每次对话会自动注入。

---

## 安全机制

OmniAgent 在工具执行层内置了多层安全防护：

| 防护层 | 机制 | 说明 |
|--------|------|------|
| **凭证隔离** | `chmod 0600` + YAML 文件 | API Key 不入仓库、不入环境变量 |
| **命令审查** | SSRF 拦截 + 命令注入收口 | `command` 工具对敏感操作进行拦截 |
| **Git 保护** | 危险命令黑名单 | `push --force`、`hard reset` 等需确认 |
| **文件保护** | 编辑前 diff 预览 | `edit_file` / `write_file` 修改前显示变更 |
| **网络安全** | SSRF 黑名单 | IPv4 私有网 / IPv6 ULA / 数字编码 IP / 重定向拦截 |
| | 公共 API 白名单 | 仅允许已知安全域名的 web_fetch |
| **RCE 收敛** | `register_tool` 安全白名单 | 仅允许指定前缀的 Python 模块导入 |
| **敏感路径** | 黑名单过滤 | `.env` / `credentials` / `.ssh` 等不可读取 |

---

## FAQ

### OmniAgent 和 Claude Code / Aider 有什么区别？

Claude Code 和 Aider 是面向**终端用户**的 AI 编程助手，目标是开箱即用、帮你写代码。OmniAgent 是面向**开发者**的 Agent 参考实现，目标是展示 ReAct、断路器、MCP、上下文压缩等机制的工程细节。你可以把它当作学习 Agent 架构的教科书，也可以把它作为二次开发的基座。

### 需要什么配置才能运行？

最低配置：Python 3.10+ + 1 个模型商的 API Key。不需要 GPU。Ollama 本地模型也完全支持。

### 支持哪些模型？

通过 OpenAI-compatible API 适配器，理论上支持所有主流模型商。内置了 DeepSeek、OpenAI、Anthropic、Google、智谱、通义千问等 12 家的预设。在 `/setup` 中选择"自定义模型商"可注册任意兼容 API。

### 怎么选择推理范式？

- **日常问答** → Direct
- **编程任务** → ReAct（默认推荐）
- **多文件重构** → Plan-Execute
- **需要代码审查** → Reflection
- **不确定选什么** → 让 AutoRouter 自动选择

### 上下文窗口不够用怎么办？

OmniAgent 在 Token 用量达到 80% 时自动触发 6 步压缩流水线。你也可以随时用 `/compact` 手动压缩。压缩后重要信息不会丢失。

---

## 故障排查

### `omniagent: command not found`

```bash
# 确认 pip 安装路径在 PATH 中
pip show omniagent-cli | grep Location
# 或直接 python3 -m omniagent.main
```

### API Key 配置后仍报认证错误

```bash
# 检查凭证文件权限和内容
cat ~/.omniagent/credentials.yaml
# 确认 provider 名称拼写正确（区分大小写）
# 运行 /setup 重新配置
```

### 模型无响应或超时

```bash
# 检查网络连通性
curl -I https://api.deepseek.com
# 检查断路器状态（/status 查看）
# 切换备用模型：/pool 查看可用模型
```

### 终端显示乱码

```bash
# 确认终端支持 UTF-8 和真彩色
echo $TERM        # 应为 xterm-256color 或类似
echo $LANG        # 应包含 UTF-8
# 推荐终端：iTerm2、Windows Terminal、Kitty、Alacritty
```

### 提示 prompt_toolkit 初始化失败

OmniAgent 会自动回退到内置输入模式。如果希望使用完整功能，确保 `prompt-toolkit>=3.0` 已安装且终端类型支持。

---

## 文档

| 文档 | 说明 |
|------|------|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 8 种引擎切换图 + 路由层 + 可靠性三件套 |
| [`docs/COMPARISON.md`](docs/COMPARISON.md) | vs Aider / Claude Code / OpenCode / Crush |
| [`docs/OPERATION_GUIDE.md`](docs/OPERATION_GUIDE.md) | REPL 命令手册 + 工作流示例 |
| [`docs/EVAL_RESULTS.md`](docs/EVAL_RESULTS.md) | 评测框架使用指南与详细结果 |
| [`docs/omniagent-design-spec-v1.1.pdf`](docs/omniagent-design-spec-v1.1.pdf) | 完整设计规范 PDF |
| [`docs/reports/v0.2.2/`](docs/reports/v0.2.2/) | 端到端测试报告 |

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

## 适合谁看

| 如果你…… | 你能从这里学到 |
|----------|--------------|
| 想理解 ReAct/Plan-Execute 的实现细节 | 8 个独立引擎类的控制流差异 |
| 在给自己的项目加 MCP 支持 | stdio + SSE 双传输的完整实现 |
| 想知道 Agent 怎么防止"跑飞" | 断路器 + BudgetManager + HollowDetector |
| 在做多模型路由 | 12 provider 的统一抽象 + 自动降级 |
| 想写一个终端 Agent | prompt_toolkit + Rich 的工程实践 |

---

## 贡献

欢迎提交 Issue 和 Pull Request。

- **Bug 报告**：请在 Issue 中附上 `omniagent --version` 输出 + 复现步骤
- **功能建议**：请先开 Issue 讨论设计方案
- **代码贡献**：请确保 `pytest tests/ -q` 全部通过后再提交 PR

---

## License

MIT — see [LICENSE](LICENSE).

## Credits

- [Rich](https://github.com/Textualize/rich) — terminal UI 渲染
- [prompt_toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit) — 终端输入框架
- [httpx](https://github.com/encode/httpx) — HTTP 客户端
- [PyYAML](https://github.com/yaml/pyyaml) — YAML 配置解析
