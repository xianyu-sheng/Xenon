<p align="center">
  <img src="docs/logo.svg" width="168" alt="Xenon Star Core logo">
</p>

<h1 align="center">Xenon</h1>

**面向实验、学习和社区协作的可扩展终端 AI 编程 Agent 试验场。**

Xenon 不以复刻商业级闭源 coding agent 为目标。它更像一个"AI 编程乌托邦"：
把模型协议、推理范式、工具调用和终端交互拆成**可观察、可替换的模块**，让任何人
都可以用一个小插件验证自己的 Agent 想法，并通过 Issue 或 PR 与社区一起迭代。

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![MIT License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![CI](https://github.com/xianyu-sheng/Xenon/actions/workflows/ci.yml/badge.svg)](https://github.com/xianyu-sheng/Xenon/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/xianyu-sheng/Xenon/branch/main/graph/badge.svg)](https://codecov.io/gh/xianyu-sheng/Xenon)
[![release v0.7.4](https://img.shields.io/badge/release-v0.7.4-orange.svg)](https://github.com/xianyu-sheng/Xenon/releases/tag/v0.7.4)

代码托管：
[GitHub 主仓库](https://github.com/xianyu-sheng/Xenon) ·
[Gitee 国内镜像](https://gitee.com/xianyu-sheng123/Xenon)

---

## 架构一览

```
用户输入
   │
   ▼
┌──────────────────────────────────────────────┐
│  REPL / CLI  (repl.py + repl_input.py)       │  ← 终端交互
├──────────────────────────────────────────────┤
│  命令层  12 个 command_groups/*.py            │  ← /help /model /mcp /memory ...
│          每个命令组是一个独立可替换的模块       │
├──────────────────────────────────────────────┤
│  引擎层  7 种推理范式 (BaseEngine ABC)         │  ← direct / ReAct / Plan-Execute
│  工具层  9 个 tool_families + 7 阶段管线       │  ← git / web / file / lsp / mcp ...
│  记忆层  memory/ (4 作用域 + 事务锁)           │  ← user / project / session
│  MCP 层  mcp/ (stdio / HTTP / SSE)            │  ← 7000+ 可发现服务器
├──────────────────────────────────────────────┤
│  Provider 层  utils/llm_client.py             │  ← 12 家厂商预设 + 原生协议分支
│              llm_clients/ 为规划中的拆分目标     │
└──────────────────────────────────────────────┘
```

---

## 如何扩展 — 四个例子

Xenon 的**目标**是「每加一种新能力只需写一个文件」。四条扩展路径的成熟度不同，
下面标注了各自的现状；正在收敛的部分见
[开放 issue](https://github.com/xianyu-sheng/Xenon/issues)。

### 1. 添加一个新工具

> 现状：注册与分发已经打通，但工具的 description/params 仍在
> `xenon/engine/react_prompts.py` 的 `BUILTIN_TOOLS` 里单独维护，
> 注册表尚未成为唯一来源。收敛进展见 issue #8。

```python
from xenon.nodes.tool_registry import register_tool_handler

def _handle_search(node, context):
    """handler 契约是 (node, context)。

    node    — 已归一化校验的调用（参数在 node 上）
    context — Xenon 的 AgentContext
    """
    query = getattr(node, "search_pattern", "")
    return {"action_type": "my_search", "success": True, "content": f"搜索: {query}"}

register_tool_handler("my_search", _handle_search, description="搜索我的知识库")
```

目前还需要同步一件事：把工具的 name/description/params 加进
`xenon/engine/react_prompts.py` 的 `BUILTIN_TOOLS`，否则模型看不到这个工具。

### 2. 添加一个新的推理引擎

> 现状：`BaseEngine` 抽象已就位（唯一抽象方法是 `run()`），但还没有
> `register_engine()`；接入一种新范式目前需要改多处。收敛进展见 issue #6。

```python
# xenon/engine/my_engine.py
from xenon.engine.base import BaseEngine

class MyEngine(BaseEngine):
    """自定义推理范式。BaseEngine.__init__ 需要 model_priority 等参数。"""

    def run(self, user_input, context=None) -> str:
        self._begin_run()
        messages = self._history_messages(user_input, limit=20)
        # 自定义推理逻辑...
        return self._call_llm(messages, phase="my_engine")
```

引擎自动继承断路器、工具安全、记忆注入等基础设施。但接入 REPL 目前还需要同步
改 `xenon/repl/model_registry.py` 的 `BUILTIN_MODES`、`xenon/repl/repl.py`
的 dispatch 分支，以及 `evals/runner.py` 的引擎白名单。

### 3. 添加一个新命令

在 `xenon/repl/command_groups/` 对应主题文件里（例如 `workspace.py`）加：

```python
from xenon.repl.command_registry import command_handler, register_command

register_command("/my-feature", "我的新功能", "/my-feature [args]")

@command_handler("/my-feature")
def _cmd_my_feature(*, args: str, session_state: dict, **kwargs):
    return f"你输入了: {args}"
```

重启 Xenon 后 `/help` 会自动列出，`/my-feature hello` 即可使用。
**不需要修改 `commands.py`**（它只是 re-export 层）。

### 4. 添加一个新的 LLM Provider

> 现状：provider 实现集中在 `xenon/utils/llm_client.py`，尚未按厂商拆包，
> 也还没有 `register_provider()`。收敛进展见 issue #5 / #7。

若厂商是 OpenAI 兼容协议，只需在 `xenon/utils/llm_client.py` 的
`_PROVIDER_DEFAULTS` 加一项：

```python
"my_provider": {
    "base_url": "https://api.myprovider.com/v1",
    "env_key": "MY_PROVIDER_API_KEY",
    "max_output_tokens": 8192,
},
```

同时需要在 `xenon/repl/provider_registry.py` 的 `PROVIDERS` 和
`xenon/repl/model_pool.py` 的 `cost_map` 各加一项。若是原生（非 OpenAI 兼容）
协议，还需在 `chat_completion()` / `chat_completion_with_tools()` /
`chat_completion_stream()` 三个入口各加分支 —— anthropic 就是这样接的。

HTTP 连接池、重试、凭证管理和 usage 追踪对所有 provider 通用。

---

## 快速开始

```bash
# 中国大陆网络优先：从 Gitee 镜像安装
pip install -U "git+https://gitee.com/xianyu-sheng123/Xenon.git@v0.7.4"

# 国际网络或上游开发：从 GitHub 安装
pip install -U "git+https://github.com/xianyu-sheng/Xenon.git@v0.7.4"
xenon
```

进入 Xenon 后运行 `/setup` 配置模型和 API Key，然后直接描述任务。
Python 3.10 及以上版本可用。

---

## 核心能力

| 能力 | 说明 |
|------|------|
| DeepSeek 适配 | V4 模型发现、`reasoning_effort`、原生工具调用、思考消息连续性 |
| Cache Rails | 按模型和执行契约维护追加式提示词轨道；`/cache` 与 `/cost` 展示厂商 usage 和费用 |
| 执行引擎 | direct、ReAct、Plan-Execute、Reflection 和三种组合模式 |
| 工具安全 | 权限确认、超时、断路器、结构化结果和中断恢复点 |
| 用户治理记忆 | 会话、项目本地、项目共享、用户全局四个作用域；自动候选必须确认后才写入 |
| 扩展 | Agent Skills、MCP（stdio/HTTP/SSE）、Ark 与 OpenAI-compatible Provider |
| 终端界面 | 多行输入、固定状态栏、`Ctrl+O` 折叠详情、运行状态图标 |

### 评测结果

所有数字来自可复现的真实运行。缓存与成本以 API 返回的 `usage` 为准。

| 评测 | 指标 | 结果 |
|------|------|------|
| 自建 20 任务 | 机器断言通过率 | 65%（13/20） |
| 自建 20 任务 | 工具执行成功率 | 95.09% |
| Cache Rails | 厂商上报命中率 | 97.35%，节省 93.03% |
| SWE-bench Lite | 引擎-实例格通过率 | 57.14%（4/7，1 instance × 7 engines 矩阵验证） |

**未跑的项目一律标注原因，不以"计划中"占位充当结果。**

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

## 开发

```bash
git clone https://github.com/xianyu-sheng/Xenon.git
cd Xenon
pip install -e ".[dev]"
ruff check xenon tests
pytest -q -m "not live"
```

---

## License

[MIT](LICENSE) · [GitHub](https://github.com/xianyu-sheng/Xenon) · [Gitee](https://gitee.com/xianyu-sheng123/Xenon)
