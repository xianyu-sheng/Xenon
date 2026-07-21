# Xenon

**让开发者零成本享受 DeepSeek 极致性价比。**

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)]()
[![MIT License](https://img.shields.io/badge/license-MIT-green.svg)]()
[![Tests](https://img.shields.io/badge/tests-131-brightgreen.svg)]()
[![v0.6.2](https://img.shields.io/badge/version-0.6.2-orange.svg)](https://github.com/xianyu-sheng/Xenon/releases)
[![DeepSeek 缓存指南](https://img.shields.io/badge/DeepSeek-缓存最佳实践-1a73e8.svg)](docs/deepseek-guide.md)
[![架构设计](https://img.shields.io/badge/📐-架构设计-8b5cf6.svg)](docs/ARCHITECTURE.md)

---

## 🏛️ 三大架构支柱

> 不是又一个套壳 agent。每一处抽象都 justified by 实际的缓存经济效益或工具执行可靠性。
> 📐 完整设计哲学、目录结构、与 Reasonix 对比见 **[架构设计文档 →](docs/ARCHITECTURE.md)**

### Pillar 1 · Cache-Aware Cost Loop

*缓存感知的费用闭环。全部本地计算，零额外 LLM 消费。*

DeepSeek 上下文缓存命中/未命中价差高达 **120 倍**。Xenon 内置三层监控，让缓存效益**可见、可量化、可优化**。

```
L1 · StatusBar   💾96%  💰¥<0.01  💡92%    每次 API 调用毫秒级刷新
L2 · /cost       按模型拆分命中/未命中 token + 费用 breakdown + 节省
L3 · 退出报告    /exit 时自动打印总账单
```

SHA256 去重 + 硬编码定价表 + PromptOptimizer 自动对齐前缀匹配窗口。

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

## 📸 效果演示

![Xenon terminal demo](docs/demo.gif)

*七步闭环：启动动画 → 首次对话 → `/cost` → 二次对话命中率飙升 → `/cost` → `/vision` 视觉模式 → 退出省钱报告*

---

## ⚡ 30 秒上手

```bash
pip install xenon       # 或 pipx install xenon
xenon                   # 启动 REPL，氙气轨道动画 Logo ✦

> /setup                # 配 API Key
> /model deepseek-v4-pro
> 你好                  # 开始对话
```

底部 toolbar 实时显示缓存命中率。`/cost` 看完整账单。`Ctrl+Alt+V` 粘贴图片让 DeepSeek 推理。

---

## 🎯 全部能力

| 类别 | 内容 |
|------|------|
| **推理范式** | 8 种（direct · react · plan-execute · reflection · novel + 4 组合） |
| **模型商** | 12 家 · 3 Tier 分级 · 故障自动转移 |
| **MCP 生态** | Smithery 7000+ 服务器 · 双传输 · 惰性加载 · `/mcp browse` 一键安装 |
| **DeepSeek 缓存** | toolbar 实时 + `/cost` 面板 + 退出报告 + 命中率骤降告警 |
| **视觉桥接** | `Ctrl+Alt+V` 粘贴 → 多模态转录 → DeepSeek 推理 · SHA256 去重 |
| **工程可靠性** | 断路器 · 三阶段预算 · 空洞检测 · 6 步上下文压缩 |
| **REPL 体验** | prompt_toolkit 补全 · Rich 渲染 · 斜杠命令 · 视觉层次 · 亮色锚点 |

---

## 🆚 一句话差异

Xenon 不是 IDE 替代品——是**深度控制面板**。你仍在 VSCode/JetBrains 里写代码，Xenon 在终端里精确控制模型、范式、费用。

| 独有能力 | Xenon | 其他工具 |
|----------|:-----:|:--------:|
| 多范式引擎（8 种） | ✅ | ❌ |
| DeepSeek 缓存追踪 | ✅ | ❌ |
| 视觉桥接（任意模型组合） | ✅ | ❌ |
| 工具断路器 + 三阶段预算 | ✅ | ❌ |
| MCP 注册中心（7000+） | ✅ | 仅 Claude Code |
| 12 家模型 · 故障转移 | ✅ | 部分 |

---

## 📖 延伸阅读

| 文档 | 内容 |
|------|------|
| **[⚡ 快速上手指南](docs/GUIDE.md)** | 安装 → 配置 → 8 范式 → 多模型切换 → DeepSeek 接入（中英双语） |
| **[架构设计](docs/ARCHITECTURE.md)** | 三大支柱详解 · 独有亮点 · 目录结构 · 设计原则 · vs Reasonix |
| **[DeepSeek 缓存实践](docs/deepseek-guide.md)** | 原理 → 对齐策略 → 三层监控 → 费用对比 → 骤降诊断 |

---

## 安装 · 命令 · 测试

```bash
git clone https://github.com/xianyu-sheng/Xenon.git && cd xenon
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
pytest tests/ -q    # 131 单元测试
```

---

MIT · [xianyu-sheng/Xenon](https://github.com/xianyu-sheng/Xenon) · Credits: [Rich](https://github.com/Textualize/rich) · [prompt_toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit) · [Smithery](https://smithery.ai)
