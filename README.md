<p align="center">
  <img src="docs/logo.svg" width="168" alt="Xenon Star Core logo">
</p>

<h1 align="center">Xenon</h1>

**Agent Harness —— 运行、约束、评测 AI 编程 Agent 的开源运行时。**

Xenon 不只是一个能写代码的 Agent，而是让 Agent **可信地**运行所需的那层基础设施：
7 种可替换的推理范式、证据约束的验证闭环、执行隔离边界、以及可复现的评测链路。
同一套运行时既支撑终端交互，也支撑 SWE-bench 官方评测——**两条路径共享同一组
隔离与验证约束**，因此评测数字对交互场景同样成立。

可以当命令行工具直接用，也可以当库嵌进你自己的 Agent 评测流程。

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![MIT License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![CI](https://github.com/xianyu-sheng/Xenon/actions/workflows/ci.yml/badge.svg)](https://github.com/xianyu-sheng/Xenon/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/xianyu-sheng/Xenon/branch/main/graph/badge.svg)](https://codecov.io/gh/xianyu-sheng/Xenon)
[![release v0.8.5](https://img.shields.io/badge/release-v0.8.5-orange.svg)](https://github.com/xianyu-sheng/Xenon/releases/tag/v0.8.5)

代码托管：
[GitHub](https://github.com/xianyu-sheng/Xenon) ·
[Gitee 镜像](https://gitee.com/xianyu-sheng123/Xenon)

---

## Harness 的五层职责

一个 Agent Harness 要回答的不是「模型能不能写出这段代码」，而是「**这个系统的输出
能不能被信任**」。Xenon 把这个问题拆成五层，每层都可独立替换与观察：

| 层 | 职责 | 实现 |
|---|---|---|
| **推理** | 用哪种策略把任务推进下去 | 7 种范式 + `register_engine()` 注册契约 |
| **工具** | 副作用如何发生、如何被记录 | ToolExecutor 7 阶段管线 + 10 个 tool_families |
| **约束** | 什么不允许发生 | 路径围栏 + 权限门 + 命令注入拦截 |
| **验证** | 凭什么相信结果 | Evidence Runtime：工具结果是 Evidence，LLM 输出只是 Claim |
| **度量** | 改动到底有没有效 | SWE-bench 官方 harness + 同模型 A/B + 落盘 seed |

第 3、4 层是 Xenon 与「又一个 Agent CLI」的真正区别——**它们决定了前两层的输出
能否被采信**。

---

## 为什么选择 Xenon

**可验证的实际效果：**
- SWE-bench Lite 官方评测 **40.0% 实例级通过率**（同模型对比 +6.7pp）
- 5 种引擎在 30 实例矩阵上的 **cell-level 45.8%**（+11.0pp）
- Cache Rails 达成 **97.35% 命中率**，节省 93% token 成本

**架构优势：**
- **可观察性**：每个推理步骤、工具调用、缓存命中都有迹可循
- **可替换性**：推理引擎、工具、Provider 通过注册机制解耦，无需修改核心代码
- **证据导向**：工具输出是 Evidence，LLM 输出是 Claim——验证闭环抑制幻觉
- **边界一致**：交互与评测共享同一组隔离约束，评测数字对日常使用同样成立

**安全边界（v0.8.5 实测加固）：**
- 文件写入受工作区围栏约束，**跟随符号链接**校验真实目标
- 模型提供的 `cwd` 一律被可信根覆盖，无法移动围栏本身
- 命令替换（`$()`/反引号/进程替换）、危险命令、敏感路径与凭证文件均拦截
- 隔离层经系统性边界探测，已修复的逃逸路径均有回归测试锁定
  （详见 [v0.8.5](https://github.com/xianyu-sheng/Xenon/releases/tag/v0.8.5)）

**适用场景：**
- 研究推理范式效果的学术团队
- 需要评测自己 Agent 的团队（复用 `evals/` 的可复现链路）
- 需要对 Agent 行为与副作用做精细控制的工程师
- 想在本地终端完成代码任务的开发者

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

```bash
# 安装
pip install -U "git+https://github.com/xianyu-sheng/Xenon.git@v0.8.3"

# 启动
xenon
```

进入后运行 `/setup` 配置模型和 API Key，然后直接描述任务。
Python 3.10+ 可用。

### 开发环境

```bash
git clone https://github.com/xianyu-sheng/Xenon.git
cd Xenon
pip install -e ".[dev]"
ruff check xenon tests evals
pytest -q -m "not live"
```

---

## 核心能力

| 能力 | 说明 |
|------|------|
| **推理引擎** | 7 种范式，通过 `register_engine()` 注册，自动继承工具安全、记忆注入、断路器 |
| **工具系统** | 10 个 tool_families（文件读写、代码搜索、git、网络、shell、MCP 等），`register_tool_handler()` 注册 |
| **LLM Provider** | 12 家厂商预设（OpenAI、Anthropic、DeepSeek、Google、Ark 等），OpenAI 兼容协议自动适配 |
| **记忆系统** | 4 作用域（user / project-local / project-shared / session），加权检索 + token 预算压缩 |
| **MCP 客户端** | stdio / HTTP / SSE 传输，自动发现与工具注入 |
| **Cache Rails** | 按模型和执行契约维护追加式提示词轨道；`/cache` 与 `/cost` 展示厂商 usage 和费用 |
| **工具安全** | 权限确认、超时、断路器、证据闸门、结构化结果与中断恢复 |
| **终端界面** | 多行输入、固定状态栏、`Ctrl+O` 折叠详情、运行状态图标 |
| **Agent Skills** | `SKILL.md` 驱动的技能加载系统 |

### 评测结果

所有数字来自可复现的真实运行，同模型 A/B 对比，官方 SWE-bench 标准 grading。

| 评测 | 指标 | 结果 | 说明 |
|------|------|------|------|
| SWE-bench Lite | instance-level | **40.0%**（12/30，同模型 A/B +6.7pp） | v0.8.2 官方 harness |
| SWE-bench Lite | cell-level | **45.8%**（33/72，同模型 A/B +11.0pp） | 5 引擎 × 30 实例矩阵 |
| cache rails | 厂商上报命中率 | 97.35%，节省 93.03% token | 追加式语境缓存 |

每引擎 resolved：react 9、plan-execute 2、plan-react 8、plan-reflection 7、react-reflection 7。

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
│   ├── command_groups/ # 12 个命令组
│   ├── provider_registry.py
│   └── model_pool.py
├── utils/             # LLM 客户端、缓存、原子写入
└── repl.py            # 入口
evals/
├── swebench_xenon.py  # SWE-bench 评测适配器
├── swebench_runtime.py # 官方运行时（Docker 容器）
└── results/           # 评测结果存档
```

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

## 扩展

Xenon 提供五条扩展路径。注册后自动被框架识别：

### 1. 注册一个 MCP 服务器（终端命令）

```bash
# 文件系统 MCP 服务器 —— 终端内执行，注册后自动发现工具
xenon> /mcp add fs npx -y @modelcontextprotocol/server-filesystem .
✅ MCP 服务器 'fs' 已连接  发现 10 个工具

# HTTP（SSE）传输
xenon> /mcp add web http://localhost:3000/sse
```

出处：`xenon/repl/command_groups/resources.py` /mcp add 子命令，
`xenon/mcp/registry.py` 服务器注册与工具发现。

### 2. 注册一个 Skill（终端命令）

```bash
# 交互式创建
xenon> /skill create
# 之后 Xenon 会引导填写名称、描述和执行提示

# 从 GitHub 仓库导入
xenon> /skill import https://github.com/user/repo/tree/main/skills/my-skill

# 查看已安装的技能
xenon> /skill list
```

Skill 存储在 `.xenon/skills/<name>/SKILL.md`，启动时自动发现。
出处：`xenon/repl/command_groups/skill.py`，`xenon/repl/skill_manager.py`。

### 3. 注册一个新工具（Python API）

```python
from xenon.nodes.tool_registry import register_tool_handler

def _handle_search(node, context):
    query = getattr(node, "search_pattern", "")
    return {"action_type": "my_search", "success": True, "content": f"搜索: {query}"}

register_tool_handler("my_search", _handle_search, description="搜索我的知识库")
```

注册后工具自动被 `ToolNode` 分发引擎识别，并合并进模型可见的工具列表
（`BUILTIN_TOOL_REGISTRY.plugin_schemas()`）。出处：
`xenon/nodes/tool_registry.py`。

### 4. 注册一个新推理引擎（Python API）

```python
from xenon.engine.base import BaseEngine
from xenon.engine.registry import register_engine

class MyEngine(BaseEngine):
    def run(self, user_input, context=None) -> str:
        self._begin_run()
        messages = self._history_messages(user_input, limit=20)
        return self._call_llm(messages, phase="my_engine")

register_engine(
    "my-engine",
    factory=lambda **kw: MyEngine(**kw),
    description="我的自定义推理范式",
    mode_line="· MyEngine 执行",
    result_title="MyEngine 结果",
)
```

注册一次，`/mode` 列表、REPL 分发、setup wizard 与 evals 白名单全部自动识别。
出处：`xenon/engine/registry.py`，`tests/test_engine_registry.py`。

### 5. 注册一个新命令（终端命令 + Python API）

```python
# 在 xenon/repl/command_groups/ 对应主题文件里
from xenon.repl.command_registry import command_handler, register_command

register_command("/my-feature", "我的新功能", "/my-feature [args]")

@command_handler("/my-feature")
def _cmd_my_feature(*, args: str, session_state: dict, **kwargs):
    return f"你输入了: {args}"
```

重启后 `/help` 自动列出。出处：`xenon/repl/command_registry.py`。

---

## License

[MIT](LICENSE) · [GitHub](https://github.com/xianyu-sheng/Xenon) · [Gitee](https://gitee.com/xianyu-sheng123/Xenon)