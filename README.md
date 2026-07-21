# Xenon

**让开发者在终端里可靠、透明、低成本地使用 DeepSeek。**

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)]()
[![MIT License](https://img.shields.io/badge/license-MIT-green.svg)]()
[![CI](https://github.com/xianyu-sheng/Xenon/actions/workflows/ci.yml/badge.svg)](https://github.com/xianyu-sheng/Xenon/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/xianyu-sheng/Xenon/branch/main/graph/badge.svg)](https://codecov.io/gh/xianyu-sheng/Xenon)
[![source v0.6.3](https://img.shields.io/badge/source-v0.6.3-orange.svg)](CHANGELOG.md)
[![DeepSeek 缓存指南](https://img.shields.io/badge/DeepSeek-缓存最佳实践-1a73e8.svg)](docs/deepseek-guide.md)
[![架构设计](https://img.shields.io/badge/📐-架构设计-8b5cf6.svg)](docs/ARCHITECTURE.md)

---

## 🖥️ 新版终端界面

v0.6.3 的可见变化是 TUI 重新排版；核心仍是原有的多模型、8 引擎、工具管线和双上下文架构。同版本其余改动主要是修复已有组件的权限、失败传播、恢复、GitHub URL 和 DeepSeek 协议问题，不是替换整体架构。

```text
  💭 1 步 · 1 个工具  [Ctrl+O 展开详情]

● ReAct 结果
  文件已读取，当前 Xenon 版本为 0.6.3。

───────────────────────────────────────────────
  ❯ 继续输入…
───────────────────────────────────────────────

  ● deepseek  ·  … · deepseek/deepseek-v4-pro  ·  react  ·  context 5.0%  ·  cache 89%  ·  ¥0.03  ·  tools 1  ·  Ctrl+O details  ·  Shift+Tab mode
```

- 输入区由两条贯穿终端宽度的平行线定界，支持多行输入。
- 状态栏与输入框下边界分离，固定在整个终端屏幕底部。
- 模型回复与优化后的提示词不再使用大边框；回复保持正常亮度，日志和辅助信息降低亮度。
- 工具调用、探索过程和 HTTP 日志默认折叠，按 `Ctrl+O` 展开或折叠上一次详情。

完整的视觉层级、快捷键与普通终端回退行为见 **[TUI 设计与操作说明 →](docs/TUI.md)**。

---

## 🏛️ 三大架构支柱

> 不只是模型调用包装：核心抽象围绕缓存成本、工具权限和失败恢复设计。
> 📐 完整设计哲学、目录结构、与 Reasonix 对比见 **[架构设计文档 →](docs/ARCHITECTURE.md)**

### Pillar 1 · Cache-Aware Cost Loop

*缓存感知的费用闭环。全部本地计算，零额外 LLM 消费。*

按 DeepSeek 2026-07-21 官方价格，V4 上下文缓存命中/未命中价差最高 **120 倍**。Xenon 内置三层监控，让缓存效益**可见、可量化、可优化**。

```
L1 · StatusBar   ● deepseek · context 3.1% · cache 99% · <¥0.01
L2 · /cost       按模型拆分命中/未命中 token + 费用 breakdown + 节省
L3 · 退出报告    /exit 时自动打印总账单
```

SHA256 去重 + 本地版本化定价快照 + PromptOptimizer 自动对齐前缀匹配窗口。

📖 **[DeepSeek 缓存最佳实践指南 →](docs/deepseek-guide.md)** · 📐 **[架构 §Pillar 1 →](docs/ARCHITECTURE.md#-pillar-1--cache-aware-cost-loop缓存感知的费用闭环)**

---

### Pillar 2 · 8-Engine Auto-Router

*不是换更好的模型，而是换更适合的引擎。*

| 引擎 | 场景 |
|------|------|
| **direct** | 纯对话 |
| **react** | 多步推理 + 工具调用（默认复杂引擎） |
| **plan-execute** | 先规划 DAG → 拓扑并行执行 |
| **reflection** | 执行者 + 独立审查者双模型 |
| **novel** | 大纲→章节，长文生成 |
| **plan-react · plan-reflection · react-reflection** | 组合引擎 |

输入文件路径/GitHub URL 自动切 ReAct；LLM 输出工具 JSON 自动检测重试。

📐 **[架构 §Pillar 2 →](docs/ARCHITECTURE.md#-pillar-2--8-engine-auto-router八引擎自动路由)**

---

### Pillar 3 · 7-Stage Tool Pipeline

*26 个工具统一执行管线。*

```
存在性校验 → 参数标准化 → 幻觉检测 → 权限闸门 → 断路器 → 执行 → 结果封装
```

每个工具失败返回结构化 `{success, error}`，断路器防无限重试，SSRF/命令注入/路径越界全拦截。

📐 **[架构 §Pillar 3 →](docs/ARCHITECTURE.md#-pillar-3--7-stage-tool-pipeline七阶段工具执行管线)**

---

## ⚡ 30 秒上手

```bash
pip install -U "git+https://github.com/xianyu-sheng/Xenon.git"  # 当前 v0.6.3 源码
xenon                                                     # 启动 REPL

❯ /setup                # 配 API Key
❯ /model deepseek-v4-pro
❯ 你好                  # 开始对话
```

`v0.6.3` tag / GitHub Release 尚在发布检查清单中；发布前请使用上述 Git 安装命令，避免装到不包含新 TUI 的旧版本。

固定底部状态栏实时显示 API、模型、范式、上下文、缓存、费用和工具计数。`/cost` 看完整账单，`Ctrl+O` 查看折叠过程，`Ctrl+Alt+V` 粘贴图片让 DeepSeek 推理。

---

## 🎯 全部能力

| 类别 | 内容 |
|------|------|
| **推理范式** | 8 种（direct · react · plan-execute · reflection · novel + 4 组合） |
| **模型商** | 11 家预设 · 3 Tier 分级 · 故障自动转移 |
| **MCP 生态** | Smithery 社区服务器 · 双传输 · 惰性加载 · `/mcp browse` 安装 |
| **DeepSeek 缓存** | toolbar 实时 + `/cost` 面板 + 退出报告 + 命中率骤降告警 |
| **视觉桥接** | `Ctrl+Alt+V` 粘贴 → 多模态转录 → DeepSeek 推理 · SHA256 去重 |
| **工程可靠性** | 断路器 · 三阶段预算 · 空洞检测 · 6 步上下文压缩 |
| **REPL 体验** | 双线多行输入 · 固定底栏 · 无边框回复 · `Ctrl+O` 折叠详情 · 自适应宽度 |

---

## 🧭 设计重点

Xenon 不是 IDE 替代品，而是终端里的 AI 编程工作区：强调可观察的模型路由、费用、权限与工具执行轨迹。

| 重点 | Xenon 的实现 |
|------|--------------|
| DeepSeek 适配 | V4 模型发现、V4 Pro 默认 `reasoning_effort=max`、思考模式工具续轮、缓存与人民币费用追踪 |
| 工具可靠性 | 权限闸门、结构化失败、断路器、事务化文件写入 |
| 长任务连续性 | 跨轮工具轨迹、工作记忆、自动保存与恢复 |
| 可扩展性 | 多模型 fallback、MCP、8 种执行范式 |

---

## 📖 延伸阅读

| 文档 | 内容 |
|------|------|
| **[⚡ 快速上手指南](docs/GUIDE.md)** | 安装 → 配置 → 8 范式 → 多模型切换 → DeepSeek 接入（中英双语） |
| **[TUI 设计与操作](docs/TUI.md)** | 双线输入区 · 固定底栏 · 无边框回复 · 折叠详情 |
| **[架构设计](docs/ARCHITECTURE.md)** | 三大支柱详解 · 独有亮点 · 目录结构 · 设计原则 · vs Reasonix |
| **[DeepSeek 缓存实践](docs/deepseek-guide.md)** | 原理 → 对齐策略 → 三层监控 → 费用对比 → 骤降诊断 |
| **[DeepSeek 收录准备](docs/DEEPSEEK_INTEGRATION.md)** | 官方兼容性证据 · 收录 PR 文案 · 提交检查清单 |

---

## 安装 · 命令 · 测试

```bash
git clone https://github.com/xianyu-sheng/Xenon.git && cd Xenon
pip install -e ".[dev]"
```

| 常用命令 | |
|----------|--|
| `/setup` `/model <n>` `/models` | 配置与模型管理 |
| `/mode [name]` `Shift+Tab` | 范式切换 |
| `/cost` `/vision on\|off` | 费用追踪 · 视觉模式 |
| `/mcp browse` `/mcp install` | MCP 生态 |
| `/save` `/load` `/resume` `/clear` | 会话管理 |

```bash
pytest tests xenon/tests -m "not live and not e2e" -q
```

---

MIT · [xianyu-sheng/Xenon](https://github.com/xianyu-sheng/Xenon) · Credits: [Rich](https://github.com/Textualize/rich) · [prompt_toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit) · [Smithery](https://smithery.ai)
