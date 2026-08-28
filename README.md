<p align="center">
  <img src="docs/logo.svg" width="168" alt="Xenon Star Core logo">
</p>

<h1 align="center">Xenon</h1>

**Agent Harness for AI coding agents** — 可信、可验证、可评测的 AI Agent 运行时

Xenon 不是又一个 AI 编程助手，而是让 Agent **可信地运行**所需的基础设施。

**核心差异：**
- 🔒 **证据导向架构**：工具输出是 Evidence，LLM 输出只是 Claim — 验证闭环抑制幻觉
- 🛡️ **执行隔离边界**：路径围栏、命令注入拦截、权限门 — 所有副作用经同一收敛点
- 🔄 **7 种推理范式**：ReAct / Plan-Execute / Reflection 及其组合 — 可替换、可观察
- 📊 **可复现评测**：SWE-bench Lite **40.0%** 通过率（同模型 +6.7pp），交互与评测共享约束
- 🎯 **生产就绪**：v0.8.5 经系统性边界探测，所有逃逸路径均已修复并锁定回归测试

可以当命令行工具直接用，也可以当库嵌入你的 Agent 评测流程。Python 3.10+。

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![MIT License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![CI](https://github.com/xianyu-sheng/Xenon/actions/workflows/ci.yml/badge.svg)](https://github.com/xianyu-sheng/Xenon/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/xianyu-sheng/Xenon/branch/main/graph/badge.svg)](https://codecov.io/gh/xianyu-sheng/Xenon)
[![release v0.8.5](https://img.shields.io/badge/release-v0.8.5-orange.svg)](https://github.com/xianyu-sheng/Xenon/releases/tag/v0.8.5)

代码托管：
[GitHub](https://github.com/xianyu-sheng/Xenon) ·
[Gitee 镜像](https://gitee.com/xianyu-sheng123/Xenon)

---

## 架构哲学：五层职责

Xenon 要回答的核心问题不是「模型能不能写代码」，而是「**这个系统的输出能不能被信任**」。

| 层 | 职责 | 实现 | 为什么重要 |
|---|---|---|---|
| **推理** | 如何推进任务 | 7 种范式 + `register_engine()` | 不同任务需要不同策略 |
| **工具** | 副作用如何发生 | 7 阶段管线 + 10 个 tool_families | 所有操作有迹可循 |
| **约束** | 什么不允许发生 | 路径围栏 + 命令拦截 + 权限门 | 防止逃逸和破坏 |
| **验证** | 凭什么相信结果 | Evidence vs Claim 闭环 | 抑制幻觉和错误传播 |
| **度量** | 改动有没有效 | SWE-bench 官方 harness | 数字可复现 |

**约束层**和**验证层**是 Xenon 与其他 Agent 框架的本质区别 — 它们决定了前两层的输出是否可信。所有文件副作用都经由 `ToolExecutor` 这一个收敛点，因此 v0.8.5 的两个安全修复各自只改一处，就覆盖了 12 个调用点和全部 7 种范式。

---

## 为什么选择 Xenon

### 实测效果

| 评测维度 | 结果 | 说明 |
|---------|------|------|
| **SWE-bench Lite** | **40.0%** 实例级通过率 | 30 实例，同模型 A/B 对比 **+6.7pp** |
| **多引擎矩阵** | **45.8%** cell 级通过率 | 5 引擎 × 30 实例 = 150 cells，**+11.0pp** |
| **缓存效率** | 97.35% 命中率 | Cache Rails 节省 **93% token** 成本 |

*所有数字来自可复现的真实运行，官方 SWE-bench Docker 容器 + 同模型基线对比。*

### 架构优势

- **证据导向**：工具输出是 Evidence，LLM 输出是 Claim — 验证闭环抑制幻觉传播
- **边界一致**：交互与评测共享同一组隔离约束，评测数字对日常使用同样成立
- **可观察性**：每个推理步骤、工具调用、缓存命中都有完整的审计日志
- **可扩展性**：引擎、工具、Provider 通过注册机制解耦，无需修改核心代码

### 安全加固（v0.8.5）

经系统性边界探测，以下逃逸路径均已修复并锁定回归测试：
- ✅ 符号链接跟随：写入前校验真实目标，防止跳出工作区
- ✅ cwd 覆盖攻击：模型提供的 `cwd` 被可信根强制覆盖
- ✅ 命令注入：拦截 `$()`、反引号、进程替换及危险命令组合
- ✅ 敏感文件保护：凭证文件、系统路径全部拦截

详见 [v0.8.5 Release Notes](https://github.com/xianyu-sheng/Xenon/releases/tag/v0.8.5)

---

## 架构

```
用户输入 / 评测 harness
   │
   ▼
┌─────────────────────────────────────────────────┐
│  REPL / CLI  终端交互层                          │  ← 输入解析、会话管理、命令系统
│  (repl/)     11 个命令组（/mode /cache /mcp ...）│
├─────────────────────────────────────────────────┤
│  引擎层  7 种推理范式（BaseEngine + registry）     │  ← 推理策略
│  工具层  10 个 tool_families + 7 阶段管线         │  ← 文件/代码/网络/搜索/MCP...
│  约束层  ToolRuntime 工作区绑定 + 路径围栏         │  ← 副作用边界（交互/评测同源）
│           + 权限门 + 命令注入拦截                  │
│  验证层  Evidence Runtime（Claim vs Evidence）    │  ← 结果凭什么可信
│  记忆层  4 作用域 (user/project-local/project-    │  ← 跨会话状态管理
│           shared/session)                        │
│  MCP 层  客户端（stdio/HTTP/SSE）                │  ← 外部工具协议
├─────────────────────────────────────────────────┤
│  Provider 层  llm_client.py + 12 厂商预设        │  ← 模型调用、缓存、用量追踪
└─────────────────────────────────────────────────┘
```

约束层与验证层是 Harness 的承重墙：**所有文件副作用都经由 ToolExecutor 这一条
通道**，因此一处加固即全局生效——v0.8.5 的两个逃逸修复各自只改一个收敛点，
就覆盖了 12 个调用点与全部 7 种范式。

### 推理引擎

| 引擎 | 策略 | 适用场景 |
|------|------|---------|
| direct | 单次 LLM 调用 | 无工具需求的简单任务 |
| react | ReAct 循环 (Thought→Action→Observation) | 需要工具交互的通用任务 |
| plan-execute | 计划→步骤执行→验证闭环 | 多步骤代码修改 |
| reflection | 执行→审查→反馈→再执行 | 需要质量审查的复杂任务 |
| plan-react | 计划→ReAct 步骤执行 | 结构化分解后的工具执行 |
| plan-reflection | 计划→执行→审查→修复 | 完整开发流程 |
| react-reflection | ReAct→审查→ReAct 修复 | 已有结果后审查改进 |

引擎通过 `register_engine()` 注册（见 `xenon/engine/registry.py`），新增范式
无需修改 REPL、evals 或 setup wizard 的硬编码清单。

---

## 快速开始

### 安装

```bash
# 从 PyPI 安装（推荐）
pip install -U xenon-agent

# 或从 GitHub 最新版本
pip install -U "git+https://github.com/xianyu-sheng/Xenon.git@v0.8.5"
```

需要 Python 3.10+。

### 首次使用

```bash
# 启动交互式终端
xenon

# 首次运行会引导配置
xenon> /setup
# 按提示选择模型、输入 API Key

# 然后直接描述任务
xenon> 帮我重构这个函数，提取出公共逻辑

# 查看可用命令
xenon> /help

# 切换推理引擎
xenon> /mode plan-execute
```

### 开发环境

```bash
git clone https://github.com/xianyu-sheng/Xenon.git
cd Xenon
pip install -e ".[dev]"

# 代码检查
ruff check xenon tests evals

# 运行测试（跳过需要真实 API 的测试）
pytest -q -m "not live"
```

---

## 核心特性

| 特性 | 说明 |
|------|------|
| **7 种推理引擎** | ReAct / Plan-Execute / Reflection / Plan-ReAct / Plan-Reflection / ReAct-Reflection / Direct，通过 `register_engine()` 扩展 |
| **10 个工具族** | 文件读写、代码搜索、git 操作、shell 命令、网络请求、MCP 集成等，`register_tool_handler()` 注册新工具 |
| **12 家 LLM Provider** | OpenAI / Anthropic / DeepSeek / Google / Ark / SiliconFlow 等，OpenAI 兼容协议自动适配 |
| **4 作用域记忆** | user / project-local / project-shared / session，加权检索 + token 预算自动压缩 |
| **MCP 客户端** | stdio / HTTP / SSE 三种传输方式，自动发现外部工具并注入 Agent |
| **Cache Rails** | 按模型和执行契约维护追加式提示词轨道，97%+ 缓存命中率（`/cache` 和 `/cost` 查看详情） |
| **工具安全层** | 权限确认、超时控制、断路器、证据闸门、结构化结果、中断恢复 |
| **终端界面** | 多行输入、固定状态栏、`Ctrl+O` 折叠详情、运行状态实时展示 |
| **Agent Skills** | `SKILL.md` 驱动的技能系统，支持导入和自定义 |

### 推理引擎对比

| 引擎 | 策略 | 最佳场景 | SWE-bench Lite 解决数 |
|------|------|---------|---------------------|
| **react** | Thought→Action→Observation 循环 | 需要工具交互的通用任务 | 9/30 |
| **plan-execute** | 先规划后执行 | 多步骤结构化任务 | 2/30 |
| **plan-react** | 规划 + ReAct 步骤执行 | 复杂任务的分解执行 | 8/30 |
| **plan-reflection** | 规划→执行→审查→修复 | 需要质量保证的完整流程 | 7/30 |
| **react-reflection** | ReAct→审查→修正 | 已有结果需改进的场景 | 7/30 |
| **reflection** | 执行→审查→反馈→再执行 | 需要反复优化的任务 | - |
| **direct** | 单次 LLM 调用 | 无工具需求的简单任务 | - |

引擎通过 `register_engine()` 注册后，自动被 `/mode` 命令、setup wizard 和 evals 识别。

---

## 项目结构

```
xenon/
├── engine/            # 推理引擎（7 种范式 + registry）
│   ├── base.py       # BaseEngine ABC
│   ├── registry.py   # EngineSpec + register_engine()
│   ├── verification_loop.py  # 跨轮次验证循环（v0.8.3）
│   ├── react_engine.py
│   ├── plan_execute_engine.py
│   ├── reflection_engine.py
│   └── combined_engines.py
├── nodes/             # 工具层
│   ├── tool_families/ # 10 个工具族（file_mutation / search / git / command / web / mcp...）
│   ├── tool_registry.py
│   ├── tool_executor.py
│   └── tool_node.py
├── memory/            # 记忆系统
│   ├── service.py    # 4 作用域 CRUD
│   ├── retrieval.py  # 加权检索
│   └── compiler.py   # token 预算压缩
├── mcp/               # MCP 客户端（transport / client / registry）
├── repl/              # 终端交互层
│   ├── command_groups/ # 11 个命令组
│   ├── provider_registry.py
│   └── model_pool.py
├── utils/             # LLM 客户端、缓存、原子写入
└── repl.py            # 入口
evals/
├── swebench_xenon.py  # SWE-bench 评测适配器
├── swebench_runtime.py # 官方运行时（Docker 容器）
└── results/           # 评测结果存档
```

## 使用场景

**研究者**：比较不同推理范式的效果，复用可复现的评测链路  
**工程师**：需要对 Agent 行为和副作用做精细控制，确保生产环境安全  
**开发者**：在本地终端完成代码任务，支持多种 LLM Provider  
**团队**：评测自研 Agent 的效果，借助 `evals/` 的 SWE-bench 适配器

---

## 文档

| 文档 | 内容 |
|------|------|
| [架构设计](docs/ARCHITECTURE.md) | 缓存、路由、工具、记忆与恢复机制 |
| [快速上手](docs/GUIDE.md) | 安装、配置、模型与执行模式 |
| [DeepSeek 缓存](docs/deepseek-guide.md) | Cache Rails、usage、费用与诊断 |
| [记忆系统](docs/MEMORY_SYSTEM_SPEC.md) | 作用域、用户确认、容量治理与回滚 |
| [Agent Skills](docs/AGENT_SKILLS.md) | `SKILL.md` 发现、加载与安全边界 |
| [TUI 操作](docs/TUI.md) | 输入区、状态栏与快捷键 |
| [贡献指南](CONTRIBUTING.md) | Issue / PR 流程、代码规范 |
| [更新日志](CHANGELOG.md) | 各版本功能与验证结果 |

---

## 扩展 Xenon

Xenon 提供五条注册式扩展路径，无需修改核心代码：

### 1. 注册 MCP 服务器（终端命令）

```bash
# 文件系统 MCP 服务器
xenon> /mcp add fs npx -y @modelcontextprotocol/server-filesystem .
✅ MCP 服务器 'fs' 已连接  发现 10 个工具

# HTTP（SSE）传输
xenon> /mcp add web http://localhost:3000/sse

# 查看已连接的服务器
xenon> /mcp list
```

出处：`xenon/repl/command_groups/resources.py`，`xenon/mcp/registry.py`

### 2. 注册 Agent Skill（终端命令）

```bash
# 交互式创建
xenon> /skill create

# 从 GitHub 导入
xenon> /skill import https://github.com/user/repo/tree/main/skills/my-skill

# 查看已安装技能
xenon> /skill list
```

Skill 存储在 `.xenon/skills/<name>/SKILL.md`，启动时自动加载。
出处：`xenon/repl/skill_manager.py`

### 3. 注册工具处理器（Python API）

```python
from xenon.nodes.tool_registry import register_tool_handler

def my_search_handler(node, context):
    query = getattr(node, "search_pattern", "")
    # 执行搜索逻辑
    return {
        "action_type": "my_search",
        "success": True,
        "content": f"搜索结果: {query}"
    }

register_tool_handler(
    "my_search",
    my_search_handler,
    description="搜索我的知识库"
)
```

注册后工具自动出现在模型可见的工具列表中。
出处：`xenon/nodes/tool_registry.py`

### 4. 注册推理引擎（Python API）

```python
from xenon.engine.base import BaseEngine
from xenon.engine.registry import register_engine

class MyCustomEngine(BaseEngine):
    def run(self, user_input, context=None) -> str:
        self._begin_run()
        messages = self._history_messages(user_input, limit=20)
        response = self._call_llm(messages, phase="my_custom")
        return response

register_engine(
    "my-custom",
    factory=lambda **kw: MyCustomEngine(**kw),
    description="我的自定义推理策略",
    mode_line="· MyCustom 执行中",
    result_title="MyCustom 结果"
)
```

注册一次，`/mode` 列表、REPL 路由、setup wizard 全部自动识别。
出处：`xenon/engine/registry.py`

### 5. 注册终端命令（Python API）

```python
from xenon.repl.command_registry import command_handler, register_command

register_command(
    "/myfeature",
    "我的新功能",
    "/myfeature [args] - 做某件事"
)

@command_handler("/myfeature")
def _cmd_myfeature(*, args: str, session_state: dict, **kwargs):
    # 命令逻辑
    return f"处理完成: {args}"
```

重启后 `/help` 自动列出新命令。
出处：`xenon/repl/command_registry.py`

---

## License

[MIT](LICENSE) · [GitHub](https://github.com/xianyu-sheng/Xenon) · [Gitee](https://gitee.com/xianyu-sheng123/Xenon)