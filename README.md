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
| 执行引擎 | direct、ReAct、Plan-Execute、Reflection、Novel 和三种组合模式 |
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
