<p align="center">
  <img src="docs/logo.svg" width="168" alt="Xenon Star Core logo">
</p>

<h1 align="center">Xenon</h1>

**面向 DeepSeek 的终端 AI 编程工作区。**

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![MIT License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![CI](https://github.com/xianyu-sheng/Xenon/actions/workflows/ci.yml/badge.svg)](https://github.com/xianyu-sheng/Xenon/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/xianyu-sheng/Xenon/branch/main/graph/badge.svg)](https://codecov.io/gh/xianyu-sheng/Xenon)
[![release v0.7.3](https://img.shields.io/badge/release-v0.7.3-orange.svg)](https://github.com/xianyu-sheng/Xenon/releases/tag/v0.7.3)

Xenon 在终端中提供多模型对话、工具执行、自动路由、持久记忆和会话恢复。
它重点解决三个工程问题：模型缓存是否真正命中、工具操作是否受控、长期记忆是否由用户决定。

代码托管：
[GitHub 主仓库](https://github.com/xianyu-sheng/Xenon) ·
[Gitee 国内镜像](https://gitee.com/xianyu-sheng123/Xenon)

## 快速开始

```bash
# 中国大陆网络优先：从 Gitee 镜像安装
pip install -U "git+https://gitee.com/xianyu-sheng123/Xenon.git@v0.7.3"

# 国际网络或上游开发：从 GitHub 安装
pip install -U "git+https://github.com/xianyu-sheng/Xenon.git@v0.7.3"
xenon
```

进入 Xenon 后运行 `/setup` 配置模型和 API Key，然后直接描述任务。Python 3.10 及以上版本可用。

开发安装：

```bash
git clone https://gitee.com/xianyu-sheng123/Xenon.git
cd Xenon
pip install -e ".[dev]"
```

需要参与代码开发时，也可以克隆
[GitHub 主仓库](https://github.com/xianyu-sheng/Xenon)。两个仓库的源码和版本标签保持一致。

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

### Cache Rails 验证

v0.7.3 使用 DeepSeek Flash/Pro 交替调用 10 次进行真实验收。连续两轮测试均保持
两条独立轨道且无分叉；两个模型冷启动后的 16 次返回调用全部获得厂商缓存命中，
两轮热调用 input token 命中率分别为 96.57% 和 96.74%。真实命中率始终以 API
返回的 usage 为准，本地轨道估算不会冒充厂商数据。

详见 [DeepSeek 缓存指南](docs/deepseek-guide.md)。

## 产品方向与质量门槛

Xenon 的优先级是**正确性优先，效率放大**。Cache Rails 能降低一次正确任务的
Token 和费用，但不能抵消错误的工具选择、错误的文件修改或未经验证的结论。
因此我们不会把缓存节省率与任务正确率合并成一个总分：先确认结果做对，再衡量
完成同一结果用了多少 Token 和费用。

当前评测分为三层：

1. **任务正确性**：实际执行了必要工具、结果非空，并逐步增加文件状态、测试结果和语义结果校验。
2. **执行可靠性**：工具参数校验、路径边界、权限决策、模型 fallback、记忆确认和会话恢复。
3. **效率价值**：Cache Rails 命中率、可复用前缀、全 miss 成本基线、实际节省和延迟。

v0.7.3 的一次真实 20 任务评测结果为：通用任务成功率 **45%（9/20）**，而 Cache Rails
命中率 **85.98%**、可复用前缀命中 **1,416,064 tokens**、预计节省 **79.77%**。
这组数据说明 Xenon 已经具备明显的效率优势，但当前阶段的首要工作仍是提升任务正确率，
不能把“省钱”误解为“做对了”。完整记录见 [评测结果](docs/EVAL_RESULTS.md)。

接下来的开发门槛：

- 先修复评测任务与工作目录、预置材料不匹配的问题，避免把环境缺失误判为模型失败。
- 为文件编辑、代码搜索和多轮修订增加可机器验证的结果断言，而不只检查工具名。
- 为 REPL 命令、权限确认、记忆写入和会话恢复建立真实闭环场景。
- 只有在正确性和可靠性不回退的前提下，才接受 Cache Rails、路由和 Token 优化。

评测真实任务时建议使用独立任务目录，并查看报告中的 `Verified Success Rate`：

```bash
python evals/runner.py \
  --mode real \
  --model deepseek/deepseek-v4-pro \
  --workdir /tmp/xenon-eval-workdir \
  --isolate-tasks
```

未配置结果断言的旧任务会显示为 `Verified: n/a`，不会被误认为已经证明正确。

## 常用命令

| 命令 | 用途 |
|------|------|
| `/setup` `/model` `/pool` | 配置模型与故障转移池 |
| `/mode` `Shift+Tab` | 选择或切换执行引擎 |
| `/cache` `/cost` | 查看缓存证据、轨道和费用 |
| `/memory status` `/memory inspect` | 查看记忆范围、路径和元数据 |
| `/mcp` `/skill` | 管理 MCP 与 Agent Skills |
| `/save` `/resume` | 保存或恢复会话 |
| `Ctrl+O` | 展开或折叠最近一次执行详情 |

运行 `/help` 查看完整命令列表。

## 文档

| 文档 | 内容 |
|------|------|
| [快速上手](docs/GUIDE.md) | 安装、配置、模型与执行模式 |
| [架构设计](docs/ARCHITECTURE.md) | 缓存、路由、工具、记忆与恢复机制 |
| [DeepSeek 缓存](docs/deepseek-guide.md) | Cache Rails、usage、费用与诊断 |
| [记忆系统](docs/MEMORY_SYSTEM_SPEC.md) | 作用域、用户确认、容量治理与回滚 |
| [Agent Skills](docs/AGENT_SKILLS.md) | `SKILL.md` 发现、加载与安全边界 |
| [外部集成](docs/INTEGRATIONS.md) | Skill/MCP 的机器可读集成契约 |
| [TUI 操作](docs/TUI.md) | 输入区、状态栏与快捷键 |
| [发布验收](docs/RELEASE_ACCEPTANCE.md) | 测试、构建、用户路径和跨平台发布门槛 |
| [更新日志](CHANGELOG.md) | 各版本功能与验证结果 |
| [完整审查报告](docs/XENON_AUDIT_2026-07-27.md) | 评测可信度、工程健康度与下一轮验收计划 |

## 社区与反馈

- [Gitee Issues（中文使用反馈与安装问题）](https://gitee.com/xianyu-sheng123/Xenon/issues)
- [GitHub Issues（代码问题、功能讨论与 PR）](https://github.com/xianyu-sheng/Xenon/issues)
- [贡献指南](CONTRIBUTING.md)
- [安全问题报告](SECURITY.md)

GitHub 是 Xenon 的开发主线，Gitee 用作国内镜像、下载入口和中文社区反馈渠道。
重要问题会同步回 GitHub，版本发布以 GitHub 为源头并同步创建 Gitee Release。

## 开发与测试

```bash
ruff check xenon tests
pytest -q -m "not live"
uv build
```

真实供应商测试默认不会运行，需显式选择带 `live` 标记的用例。

## License

[MIT](LICENSE) · [GitHub](https://github.com/xianyu-sheng/Xenon) · [Gitee](https://gitee.com/xianyu-sheng123/Xenon)
