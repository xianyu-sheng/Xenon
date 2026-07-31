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
[![release v0.7.3](https://img.shields.io/badge/release-v0.7.3-orange.svg)](https://github.com/xianyu-sheng/Xenon/releases/tag/v0.7.3)

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
│  Provider 层  llm_clients/                    │  ← _openai / _anthropic (可替换)
│              每加一个厂商只需加一个文件          │
└──────────────────────────────────────────────┘
```

---

## 如何扩展 — 四个例子

Xenon 的设计目标是：**每加一种新能力，只需写一个文件**。以下是四种最常见扩展的
具体做法。

### 1. 添加一个新工具

在 `xenon/nodes/tool_families/` 下创建 `my_tool.py`：

```python
# xenon/nodes/tool_families/my_tool.py
def register(registry):
    registry.register(
        name="my_search",
        description="搜索我的知识库",
        parameters={"query": "string", "limit": "int"},
        handler=_handle_search,
    )

def _handle_search(params, ctx):
    query = params["query"]
    limit = params.get("limit", 10)
    results = my_search_backend(query, limit)
    return {"success": True, "data": results}
```

然后在 `tool_registry.py` 的 `_load_builtins()` 里加一行 `import`。不需要修改
工具执行管线、权限系统或引擎代码。

### 2. 添加一个新的推理引擎

```python
# xenon/engine/my_engine.py
from xenon.engine.base import BaseEngine

class MyEngine(BaseEngine):
    """自定义推理范式。"""

    def run(self, user_input: str, context=None) -> str:
        self._begin_run()
        messages = self._history_messages(user_input, limit=20)
        # 自定义推理逻辑...
        return self._call_llm(messages, phase="my_engine")
```

然后在 `xenon/repl/repl.py` 的 `_handle_chat()` 里加上对新引擎的 dispatch。
引擎自动继承断路器、工具安全、记忆注入等全部基础设施。

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

```python
# xenon/llm_clients/_my_provider.py
def chat(messages, max_tokens, temperature, timeout):
    """实现 MyProvider 的原生协议。"""
    resp = httpx.post("https://api.myprovider.com/v1/chat", json={...})
    return resp.json()["content"]
```

然后在 `xenon/utils/llm_client.py` 的 `chat_completion()` 里加一个
`elif endpoint.provider == "my_provider":` 分支。`_base.py` 里的
HTTP 连接池、重试逻辑、凭证管理和 usage 追踪对任何 provider 通用。

---

## 快速开始

```bash
# 中国大陆网络优先：从 Gitee 镜像安装
pip install -U "git+https://gitee.com/xianyu-sheng123/Xenon.git@v0.7.3"

# 国际网络或上游开发：从 GitHub 安装
pip install -U "git+https://github.com/xianyu-sheng/Xenon.git@v0.7.3"
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
