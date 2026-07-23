<p align="center">
  <img src="docs/logo.svg" width="168" alt="Xenon Star Core logo">
</p>

<h1 align="center">Xenon</h1>

**让开发者在终端里可靠、透明、低成本地使用 DeepSeek。**

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)]()
[![MIT License](https://img.shields.io/badge/license-MIT-green.svg)]()
[![CI](https://github.com/xianyu-sheng/Xenon/actions/workflows/ci.yml/badge.svg)](https://github.com/xianyu-sheng/Xenon/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/xianyu-sheng/Xenon/branch/main/graph/badge.svg)](https://codecov.io/gh/xianyu-sheng/Xenon)
[![release v0.7.1](https://img.shields.io/badge/release-v0.7.1-orange.svg)](https://github.com/xianyu-sheng/Xenon/releases/tag/v0.7.1)
[![DeepSeek 缓存指南](https://img.shields.io/badge/DeepSeek-缓存最佳实践-1a73e8.svg)](docs/deepseek-guide.md)
[![架构设计](https://img.shields.io/badge/📐-架构设计-8b5cf6.svg)](docs/ARCHITECTURE.md)

---

## 🖥️ 新版终端界面

v0.7.1 在新版 TUI、User-Governed Memory、多模型、8 引擎和工具管线之上，
正式加入标准 Agent Skills、机器可读集成 CLI、Ark 一等 Provider、llms.txt 优先
文档检索和真实 Skill/MCP 互操作验证。

```text
  💭 1 步 · 1 个工具  [Ctrl+O 展开详情]

● ReAct 结果
  文件已读取，当前 Xenon 版本为 0.7.1。

───────────────────────────────────────────────
  ❯ 继续输入…
───────────────────────────────────────────────

  ● deepseek  ·  … · deepseek/deepseek-v4-pro  ·  react  ·  context 5.0%  ·  cache 89%  ·  ¥0.03  ·  tools 1  ·  Ctrl+O details  ·  Shift+Tab mode
```

- 输入区由两条贯穿终端宽度的平行线定界，支持多行输入。
- 状态栏与输入框下边界分离，固定在整个终端屏幕底部。
- 模型回复与优化后的提示词不再使用大边框；回复保持正常亮度，日志和辅助信息降低亮度。
- 工具调用、探索过程和 HTTP 日志默认折叠，按 `Ctrl+O` 展开或折叠上一次详情。
- 终端标签使用 Star Core 活动状态：执行时群星循环闪耀，等待输入、权限确认或任务完成时静止。

完整的视觉层级、快捷键与普通终端回退行为见 **[TUI 设计与操作说明 →](docs/TUI.md)**。

---

## 🏛️ 四大架构支柱

> 不只是模型调用包装：核心抽象围绕缓存成本、工具权限和失败恢复设计。
> 📐 完整设计哲学、目录结构、与 Reasonix 对比见 **[架构设计文档 →](docs/ARCHITECTURE.md)**

### Pillar 1 · Cache-Aware Cost Loop

*缓存感知的费用闭环。全部本地计算，零额外 LLM 消费。*

按 DeepSeek 2026-07-21 官方价格，V4 上下文缓存命中/未命中价差最高 **120 倍**。Xenon 将缓存效益做成可观测、可解释且可安全调优的闭环。

```
L1 · StatusBar   ● deepseek · context 3.1% · cache 99% · <¥0.01
L2 · /cache      status · explain · history · doctor · optimize
L3 · /cost       按模型拆分命中/未命中 token + 费用 breakdown + 节省
L4 · 退出报告    /exit 时自动打印总账单
```

五层 Prompt Compiler 稳定前缀，Prompt Manifest 用私有 HMAC 归因请求族；缓存亲和只在同能力层、基础分近似的健康模型之间打破平局，绝不以缓存换模型质量。

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

### Pillar 4 · User-Governed Memory

*Xenon 可以主动发现值得记住的信息，但不能偷偷替用户做决定。*

```text
明确“记住”指令 ───────────────────────→ 写入 + 路径/范围/ID 回执
Xenon 自动发现 ─→ 候选内容/原因/位置 ─→ 用户确认 ─→ 写入
                                              └────→ 忽略（零写入）
```

用户全局、项目本地、项目共享和会话四层隔离；`metadata.json` 保存机器元数据，
小型 Markdown 文件供人检查。每条记忆记录创建时间、最近检索时间和次数；达到
token 阈值时按重要性、置信度、使用与时间综合归档，固定/共享规则不自动淘汰，
也不做物理删除。跨进程事务锁避免两个终端同时写入时丢数据；潜在冲突只提示，
由用户通过 `replace/rollback` 建立或撤销版本链。

从家目录启动时，Xenon 进入安全的“无项目模式”：不会扫描家目录文件树，也不会
把 `~/XENON.local.md` 当作项目记忆。此时普通“记住”默认写入用户全局；项目本地/
共享记忆只有进入带项目边界的具体目录后才会启用。

```text
❯ 把“项目默认使用 Python 3.12”存入我的项目本地记忆
🧠 已写入 · project-local · .xenon/memory/local/project.md
```

📖 **[记忆系统完整规范 →](docs/MEMORY_SYSTEM_SPEC.md)**

---

## ⚡ 30 秒上手

```bash
pip install -U "git+https://github.com/xianyu-sheng/Xenon.git@v0.7.1"
xenon                                                     # 启动 REPL

❯ /setup                # 配 API Key
❯ /model deepseek-v4-pro
❯ 你好                  # 开始对话
```

上述命令安装经过回归验证的 `v0.7.1` tag；参与开发时可去掉 `@v0.7.1` 跟随 `main`。

固定底部状态栏实时显示 API、模型、范式、上下文、缓存、费用和工具计数。`/cost` 看完整账单，`Ctrl+O` 查看折叠过程，`Ctrl+Alt+V` 粘贴图片让 DeepSeek 推理。

---

## 🎯 全部能力

| 类别 | 内容 |
|------|------|
| **推理范式** | 8 种（direct · react · plan-execute · reflection · novel + 4 组合） |
| **模型商** | 12 家预设 · Ark 一等接入 · 3 Tier 分级 · 故障自动转移 |
| **MCP 生态** | Smithery 社区服务器 · 双传输 · 惰性加载 · `/mcp browse` 安装 |
| **Agent Skills** | 标准 `SKILL.md` · 用户/项目四层覆盖 · 正文与资源按需加载 · 兼容旧 YAML |
| **外部集成 CLI** | `integrations describe/verify --json` · 原子 Skill 安装 · MCP env/header 安全注入 · 有界真实握手 |
| **官方文档检索** | `docs_fetch` · llms.txt/llms-full.txt 优先 · 关键词选页 · HTML 透明降级 |
| **DeepSeek 缓存** | 逐请求真实 usage + `/cache` 解释/诊断/历史 + 五层前缀编译 + 保守缓存亲和路由 |
| **视觉桥接** | `Ctrl+Alt+V` 粘贴 → 多模态转录 → DeepSeek 推理 · SHA256 去重 |
| **工程可靠性** | 断路器 · 三阶段预算 · 空洞检测 · 6 步上下文压缩 |
| **透明记忆** | 四层作用域 · 写入确认/回执 · 自动容量治理 · 可恢复归档 · 安全导入 |
| **REPL 体验** | 双线多行输入 · 固定底栏 · 无边框回复 · `Ctrl+O` 折叠详情 · 自适应宽度 |

---

## 🧭 设计重点

Xenon 不是 IDE 替代品，而是终端里的 AI 编程工作区：强调可观察的模型路由、费用、权限与工具执行轨迹。

| 重点 | Xenon 的实现 |
|------|--------------|
| DeepSeek 适配 | V4 模型发现、V4 Pro 默认 `reasoning_effort=max`、思考模式工具续轮、缓存与人民币费用追踪 |
| 工具可靠性 | 权限闸门、结构化失败、断路器、事务化文件写入 |
| 长任务连续性 | 跨轮工具轨迹、四层透明记忆、自动保存与恢复 |
| 可扩展性 | 多模型 fallback、MCP、8 种执行范式 |

---

## 📖 延伸阅读

| 文档 | 内容 |
|------|------|
| **[⚡ 快速上手指南](docs/GUIDE.md)** | 安装 → 配置 → 8 范式 → 多模型切换 → DeepSeek 接入（中英双语） |
| **[TUI 设计与操作](docs/TUI.md)** | 双线输入区 · 固定底栏 · 无边框回复 · 折叠详情 |
| **[记忆系统规范](docs/MEMORY_SYSTEM_SPEC.md)** | 分层作用域 · 用户确认 · JSON/Markdown · 阈值与归档 |
| **[Agent Skills](docs/AGENT_SKILLS.md)** | `SKILL.md` 目录规范 · 分层发现 · 延迟加载 · 安全边界 |
| **[外部集成 CLI](docs/INTEGRATIONS.md)** | 面向 Ark CLI 等工具的 JSON 契约 · Skill 安装 · MCP 配置 |
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
| `/cache status` `/cache explain` `/cache doctor` | 缓存状态、直接证据与确定性诊断 |
| `/cache optimize --dry-run` `/fix-cache --apply` | 只读优化报告 · 启用可逆的同能力缓存亲和 |
| `/cost` `/vision on\|off` | 费用追踪 · 视觉模式 |
| `/mcp browse` `/mcp install` | MCP 生态 |
| `/skill list` `/skill run <name>` `/skill doctor` | Agent Skills 发现、运行与诊断 |
| `xenon integrations describe --json` | 输出可机器读取的集成能力契约 |
| `xenon integrations verify --connect-mcp --json` | 验证 Skills 与真实 MCP 握手，输出脱敏指标 |
| `xenon skill install <path> --json` | 非交互安装标准 Agent Skill |
| `xenon mcp add/list/doctor --json` | 非交互 MCP 配置与诊断 |
| `/memory status` `/memory inspect` `/memory doctor` | 查看位置、单条元数据与系统健康状态 |
| `/memory add` `/memory replace` `/memory rollback` | 写入、明确替代与可逆版本回滚 |
| `/save` `/load` `/resume` `/clear` | 会话管理 |

```bash
pytest tests xenon/tests -m "not live and not e2e" -q
```

---

MIT · [xianyu-sheng/Xenon](https://github.com/xianyu-sheng/Xenon) · Credits: [Rich](https://github.com/Textualize/rich) · [prompt_toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit) · [Smithery](https://smithery.ai)
