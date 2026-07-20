# Xenon

**让开发者零成本享受 DeepSeek 极致性价比。** 一个多范式 AI Agent 终端——8 种推理引擎、12 家模型商、MCP + Smithery 7000+ 服务器、DeepSeek 缓存追踪闭环。

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)]()
[![MIT License](https://img.shields.io/badge/license-MIT-green.svg)]()
[![Tests](https://img.shields.io/badge/tests-131-brightgreen.svg)]()
[![v0.6.2](https://img.shields.io/badge/version-0.6.2-orange.svg)](https://github.com/xianyu-sheng/Xenon/releases)
[![DeepSeek 缓存指南](https://img.shields.io/badge/DeepSeek-缓存最佳实践-1a73e8.svg)](docs/deepseek-guide.md)
[![架构设计](https://img.shields.io/badge/📐-架构设计-8b5cf6.svg)](docs/ARCHITECTURE.md)

![Xenon terminal demo](docs/demo.gif)

*💾 缓存命中率实时追踪 → 💰 费用完全透明 → 👁 视觉桥接让 DeepSeek "看见"图片。全部本地计算，零额外 LLM 消费。*

---

## 🏛️ 三大架构支柱

> 📐 详见 **[架构设计文档 →](docs/ARCHITECTURE.md)**

### Pillar 1 · Cache-Aware Cost Loop（缓存感知的费用闭环）

DeepSeek 上下文缓存命中/未命中价差高达 **120 倍**。Xenon 内置三层监控，让缓存效益可见、可量化、可优化。

```
L1 · StatusBar 实时  💾96%  💰¥<0.01  💡92%
L2 · /cost 完整面板  按模型拆分命中/未命中 token + 费用 breakdown
L3 · 退出省钱报告    /exit 时自动打印总账单
```

全部本地计算（SHA256 去重 + 硬编码定价表），不额外消费 token。PromptOptimizer 自动分离静态/动态内容，最大化前缀匹配窗口。

📖 **[DeepSeek 缓存最佳实践指南 →](docs/deepseek-guide.md)**

---

### Pillar 2 · 8-Engine Auto-Router（八引擎自动路由）

不是"换更好的模型"，而是**换更适合的引擎**。输入文件路径/GitHub URL 自动切 ReAct，LLM 输出工具 JSON 自动检测重试。

| 引擎 | 适用场景 |
|------|---------|
| **direct** | 纯对话、解释概念 |
| **react** | 多步推理、工具调用（默认复杂任务引擎） |
| **plan-execute** | 先规划 DAG → 拓扑并行执行 |
| **reflection** | 执行者 + 独立审查者，质量敏感任务 |
| **novel** | 大纲→章节，长文生成 |
| **plan-react / plan-reflection / react-reflection** | 组合引擎，极端复杂场景 |

---

### Pillar 3 · 7-Stage Tool Pipeline（七阶段工具执行管线）

26 个工具统一经过：**存在性校验 → 参数标准化 → 幻觉检测 → 权限闸门 → 断路器 → 执行 → 结果封装**。每个工具失败返回结构化 `{success, error}`，断路器防无限重试。

---

## ⚡ 30 秒上手

```bash
pip install xenon       # 或 pipx install xenon
xenon                   # 启动 REPL

> /setup                # 配 API Key
> /model deepseek-v4-pro
> 你好                  # 开始对话
```

进 REPL 后底部 toolbar 实时显示缓存命中率和费用，输 `/cost` 看完整账单，`Ctrl+Alt+V` 粘贴图片让 DeepSeek 推理。

---

## 🎯 核心能力

### 🧠 8 种推理范式

`direct` · `react` · `plan-execute` · `reflection` · `novel` · `plan-react` · `plan-reflection` · `react-reflection`

`Shift+Tab` 切换范式，或 `/mode <范式名>`

### 🌐 12 家模型商 · 3 Tier 分级

| Tier | 模型商 | 定位 |
|------|--------|------|
| **Tier 1** | DeepSeek | 主力推理 |
| **Tier 2** | 火山引擎 ARK / 豆包 | 国内加速 + 视觉 |
| **Tier 3** | OpenAI / Anthropic / Google / Kimi / 智谱 / 通义千问 / Grok / OpenRouter | 国际 + 备份 |
| **Local** | Ollama / LM Studio | 离线本地 |

故障自动转移：模型 A 失败 → 标记不可用 → 自动试模型 B → C。同 Tier 内轮询负载均衡。

### 🔌 MCP + Smithery 7000+ 服务器

双传输（stdio + SSE），惰性加载（启动 0ms），`/mcp browse` 搜索 → `/mcp install` 一键安装。

### 💾 DeepSeek 缓存追踪

底部 toolbar 实时刷新 `💾命中率 / 💰费用 / 💡节省`，`/cost` 完整面板，`/exit` 自动省钱报告。**[完整指南 →](docs/deepseek-guide.md)**

### 👁 视觉桥接器

`Ctrl+Alt+V` 粘贴图片 → 多模态模型转录 → DeepSeek 推理。零外部依赖，惰性加载，SHA256 去重。

### 🛡 工程可靠性

| 组件 | 作用 |
|------|------|
| **CircuitBreaker** | 工具连续 3 次失败熔断，30s 冷却 |
| **BudgetManager** | 三阶段预算：探索→执行→收束 |
| **HollowDetector** | 15 反模式检测"空转" |
| **Compactor** | 6 步上下文压缩 |

---

## 🆚 与主流工具的差异

Xenon 的定位不是替代 IDE 或编程助手——而是**深度控制面板**。你仍在 VSCode/JetBrains 里写代码，Xenon 在终端里精确控制用哪个模型、走什么范式、花了多少钱。

| 能力 | Xenon | Claude Code | Aider | Cursor | Copilot |
|------|:-----:|:-----------:|:-----:|:------:|:-------:|
| 多范式引擎 | ✅ 8 种 | ❌ | ❌ | ❌ | ❌ |
| DeepSeek 缓存追踪 | ✅ | ❌ | ❌ | ❌ | ❌ |
| 视觉桥接（任意模型） | ✅ | ❌ | ❌ | ❌ | ❌ |
| MCP + 注册中心 | ✅ 7000+ | ✅ | ❌ | ❌ | ❌ |
| 工具断路器 + 预算 | ✅ | ❌ | ❌ | ❌ | ❌ |
| 多模型路由 + 故障转移 | ✅ 12 家 | ❌ | ✅ | ⚠️ | ❌ |
| 开源协议 | MIT | 闭源 | Apache | 闭源 | 闭源 |

---

## 📖 延伸阅读

| 文档 | 说明 |
|------|------|
| **[架构设计](docs/ARCHITECTURE.md)** | 三大支柱 + 独有亮点 + 目录结构 + 设计原则 + 与 Reasonix 对比 |
| **[DeepSeek 缓存最佳实践](docs/deepseek-guide.md)** | 缓存原理 → 提示词对齐策略 → 三层监控 → 费用对比 → 命中率骤降诊断 |
| **[项目推广文案](~/桌面/xenon-promo.md)** | 掘金/知乎/小红书/抖音/即刻/HN 多平台文案 |

---

## 📦 安装

```bash
git clone https://github.com/xianyu-sheng/Xenon.git
cd xenon
pip install -e ".[dev]"
```

依赖：`httpx` · `pyyaml` · `rich` · `prompt-toolkit`

配置：`/setup` 交互式向导，或编辑 `~/.xenon/credentials.yaml`（自动 `chmod 0600`）。

---

## 🔧 命令参考

| 类别 | 命令 |
|------|------|
| 模型 | `/setup` `/models` `/model <name>` `/pool` |
| 范式 | `Shift+Tab` `/mode [name]` |
| 会话 | `/save` `/load` `/resume` `/clear` `/undo` `/context` |
| 费用 | `/cost` `/cost deepseek-v4-pro` |
| 视觉 | `/vision on\|off` |
| MCP | `/mcp browse` `/mcp install` `/mcp list` |
| 技能 | `/skill list` `/skill install` |
| 调试 | `/status` `/permissions` `/verbose` `/thinking` |
| 退出 | `/exit` `Ctrl+C` |

---

## 🧪 测试

```bash
pytest tests/ -q    # 131 单元测试
```

---

## 📄 License

MIT — see [LICENSE](LICENSE).

## 🙏 Credits

[Rich](https://github.com/Textualize/rich) · [prompt_toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit) · [httpx](https://github.com/encode/httpx) · [Smithery](https://smithery.ai)
