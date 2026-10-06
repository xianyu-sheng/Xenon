<p align="center">
  <img src="docs/logo.svg" width="168" alt="Xenon Star Core logo">
</p>

<h1 align="center">Xenon</h1>

**Agent Harness for AI coding agents** — 可信、可验证、可评测的 AI Agent 运行时

Xenon 不是又一个 AI 编程助手，而是让 Agent **可信地运行**所需的基础设施：
所有副作用经由同一条工具收敛点，工具输出是 Evidence，LLM 输出只是 Claim。

<p align="center">
  <img src="docs/demo.gif" width="760" alt="Xenon demo">
</p>

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![MIT License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![CI](https://github.com/xianyu-sheng/Xenon/actions/workflows/ci.yml/badge.svg)](https://github.com/xianyu-sheng/Xenon/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/xianyu-sheng/Xenon/branch/main/graph/badge.svg)](https://codecov.io/gh/xianyu-sheng/Xenon)
[![release v0.9.1](https://img.shields.io/badge/release-v0.9.1-orange.svg)](https://github.com/xianyu-sheng/Xenon/releases/tag/v0.9.1)

[GitHub](https://github.com/xianyu-sheng/Xenon) · [Gitee 镜像](https://gitee.com/xianyu-sheng123/Xenon)

---

## 核心差异

- 🔒 **证据导向**：工具输出是 Evidence，LLM 输出是 Claim —— 验证闭环抑制幻觉传播
- 🛡️ **执行隔离**：路径围栏、命令注入拦截、权限门，所有副作用经同一收敛点
- 🎯 **意图契约（新）**：LLM 主导意图 + 正则护栏，每轮一份不可变 `TurnContract`；
  显式禁令一票否决，低置信操作**询问用户**而不是硬拒或静默放行
- 📄 **文档直读（新）**：`read_document` 零依赖解析 xlsx / docx / **xls / WPS .et（OLE2+BIFF8）** / csv / json
- 🔄 **7 种推理范式**：ReAct / Plan-Execute / Reflection 及其组合，可替换、可观察
- 📊 **可复现评测**：SWE-bench Lite **40.0%** 实例通过率（同模型 +6.7pp）
- 🧠 **运行韧性**：智能检查点与语义边界续写、全引擎循环检测、空洞回答识别

可以当命令行工具直接用，也可以当库嵌入 Agent 评测流程。Python 3.10+。

## 实测效果

| 评测维度 | 结果 | 说明 |
|---|---|---|
| SWE-bench Lite | **40.0%** 实例级通过率 | 30 实例，同模型 A/B 对比 **+6.7pp** |
| 多引擎矩阵 | **45.8%** cell 级通过率 | 5 引擎 × 30 实例，**+11.0pp** |
| 缓存效率 | **97.35%** 命中率 | Cache Rails 节省 **93%** token 成本 |

数字来自可复现的真实运行（官方 SWE-bench Docker 容器 + 同模型基线），详见 [评测结果](docs/EVAL_RESULTS.md)。

## 快速开始

```bash
# 安装指定版本
pip install -U "git+https://github.com/xianyu-sheng/Xenon.git@v0.9.1"

# 或克隆源码本地安装
git clone https://github.com/xianyu-sheng/Xenon.git && cd Xenon
pip install -e .
```

```bash
xenon                # 启动交互式终端
xenon> /setup        # 首次配置：选择模型、输入 API Key
xenon> 帮我重构这个函数，提取出公共逻辑
xenon> /mode plan-execute   # 切换推理引擎；/help 查看全部命令
```

开发环境：

```bash
pip install -e ".[dev]"
ruff check xenon tests evals
pytest -q -m "not live"     # 跳过需要真实 API 的测试
```

## 核心特性

| 特性 | 说明 |
|---|---|
| 7 种推理引擎 | ReAct / Plan-Execute / Reflection / Plan-ReAct / Plan-Reflection / ReAct-Reflection / Direct，`register_engine()` 扩展 |
| 意图契约 | LLM 分类器主导（默认开启、自动从已配置 Provider 选最快模型），正则提供原语与硬约束，分类失败自动降级 |
| 文档读取 | `read_document`：xlsx/xlsm、docx、xls/.et（纯标准库 OLE2+BIFF8）、csv/tsv/json，可选 pypdf |
| 11 个工具族 | 文件读写、代码搜索、git/GitHub、LSP 导航、shell、网络请求、MCP 等，`register_tool_handler()` 注册 |
| 12 家 LLM Provider | OpenAI / Anthropic / DeepSeek / 火山 Ark / Google / 智谱 / 通义 / Moonshot / 百川 / MiniMax / Ollama / 小米 |
| 4 作用域记忆 | user / project-local / project-shared / session，加权检索 + token 预算压缩 |
| MCP 客户端 | stdio / HTTP / SSE 三种传输，自动发现外部工具并注入 Agent |
| Cache Rails | 追加式提示词轨道，97%+ 缓存命中率（`/cache`、`/cost` 查看） |
| 运行韧性 | 检查点续写、全引擎 LoopDetector、HollowDetector、DirectoryScout 项目结构注入 |
| 感知能力 | 剪贴板截图热键 `Ctrl+Alt+V`（多模态模型生成描述）、LSP 定义/引用导航 |
| 工具安全层 | 权限确认、超时、断路器、证据闸门、结构化结果、中断恢复、能力不足时询问式升级 |
| 终端界面 | prompt_toolkit 多行输入、固定状态栏、`Ctrl+O` 折叠详情 |

## 推理引擎

| 引擎 | 策略 | 适用场景 |
|---|---|---|
| direct | 单次 LLM 调用 | 无工具需求的简单任务 |
| react | Thought→Action→Observation 循环 | 需要工具交互的通用任务 |
| plan-execute | 计划→步骤执行→验证闭环 | 多步骤代码修改 |
| reflection | 执行→审查→反馈→再执行 | 需要质量审查的复杂任务 |
| plan-react | 计划→ReAct 步骤执行 | 结构化分解后的工具执行 |
| plan-reflection | 计划→执行→审查→修复 | 完整开发流程 |
| react-reflection | ReAct→审查→ReAct 修复 | 已有结果后审查改进 |

## 架构

```
REPL / CLI (repl/)          输入解析、会话、11 个命令组
  ├── 意图契约 TurnContract  LLM 分类 + 正则护栏 → 每轮唯一授权/能力契约
  ├── 引擎层                7 种范式 + registry
  ├── 工具层                11 个 tool_families + 7 阶段管线（含 read_document）
  ├── 约束层                工作区绑定 + 路径围栏 + 权限门 + 命令注入拦截
  ├── 验证层                Evidence Runtime + 空洞回答识别
  ├── 韧性层                检查点续写 + 全引擎循环检测
  ├── 感知层                视觉桥 + 剪贴板监视 + LSP
  ├── 记忆层                4 作用域
  ├── MCP 层                stdio / HTTP / SSE
  └── Provider 层           llm_client + 12 厂商预设
```

约束层与验证层是 Harness 的承重墙：所有文件副作用都经由 `ToolExecutor` 这一条
通道，一处加固即全局生效。完整模块图见 [架构设计](docs/ARCHITECTURE.md)。

## 意图契约与文档读取（v0.9.x 新增）

- 每轮开始时构建一份不可变 `TurnContract`：LLM 分类器负责语义意图与操作
  （read/write/create/delete/move/execute/network），正则层只做原语识别与
  安全约束（否定、chat-only、no-tools）。**显式禁令永远一票否决**。
- 低置信度的写入/执行提议、或工具超出本轮级别时，Xenon 会**询问用户**
  （`y` 本轮授权 / `n` 拒绝 / `a` 本会话总是允许），而不是让模型解释"没有权限"。
- 分类器默认开启，自动从已配置 Provider 中挑选最快的模型（如
  `deepseek/deepseek-v4-flash`）；无可用 Provider、超时或调用失败时自动降级
  为正则判定，不阻断对话。关闭方式：`XENON_INTENT_CLASSIFIER_ENABLED=0`
  或 `config.yaml` 写 `intent_classifier.enabled: false`。

## Windows 说明

- 默认使用 prompt_toolkit 输入路径（多行编辑、历史、补全、固定状态栏）。
- 如果你的环境里设置过 `XENON_NO_PT=1`，请删除该变量（它会退回旧的自建
  读取器）；`[Environment]::SetEnvironmentVariable('XENON_NO_PT',$null,'User')`。
- `.et` / `.xls` 等二进制表格直接用 `read_document` 读取，无需转换。

## 文档

| 文档 | 内容 |
|---|---|
| [架构设计](docs/ARCHITECTURE.md) | 缓存、路由、工具、记忆与恢复机制 |
| [快速上手](docs/GUIDE.md) | 安装、配置、模型与执行模式 |
| [扩展指南](docs/EXTENDING.md) | MCP / Skill / 工具 / 引擎 / 命令的注册方式 |
| [DeepSeek 缓存](docs/deepseek-guide.md) | Cache Rails、usage、费用与诊断 |
| [Ark Provider](docs/ARK_PROVIDER.md) | 火山方舟接入与模型配置 |
| [记忆系统](docs/MEMORY_SYSTEM_SPEC.md) | 作用域、用户确认、容量治理与回滚 |
| [Agent Skills](docs/AGENT_SKILLS.md) | `SKILL.md` 发现、加载与安全边界 |
| [TUI 操作](docs/TUI.md) | 输入区、状态栏与快捷键 |
| [运维指南](docs/OPERATION_GUIDE.md) | 运行、检查点、故障恢复与诊断 |
| [集成清单](docs/INTEGRATIONS.md) | 可接入的 Provider、工具与平台 |
| [评测结果](docs/EVAL_RESULTS.md) · [评测协议](docs/XENON_EVAL_PROTOCOL.md) | 跑测数据与复现方法 |
| [横向对比](docs/COMPARISON.md) | 与其他 Agent 框架 / 生态的对比 |
| [贡献指南](CONTRIBUTING.md) · [更新日志](CHANGELOG.md) | Issue / PR 流程、版本历史 |

## License

[MIT](LICENSE) · [GitHub](https://github.com/xianyu-sheng/Xenon) · [Gitee](https://gitee.com/xianyu-sheng123/Xenon)
