# Xenon

[![CI](https://github.com/xianyu-sheng/Xenon/actions/workflows/ci.yml/badge.svg)](https://github.com/xianyu-sheng/Xenon/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/xianyu-sheng/Xenon/branch/main/graph/badge.svg)](https://codecov.io/gh/xianyu-sheng/Xenon)
[![Release](https://img.shields.io/github/v/release/xianyu-sheng/Xenon)](https://github.com/xianyu-sheng/Xenon/releases)
[![GitHub stars](https://img.shields.io/github/stars/xianyu-sheng/Xenon?logo=github)](https://github.com/xianyu-sheng/Xenon/stargazers)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)

<p align="center">
  <img src="docs/logo-animated.gif" alt="Xenon — 像素跑者沿中线奔跑，逐渐消散重组为方块 XENON 标识" width="440">
</p>

<h3 align="center">一个循环，一个账本，一套可回放的真相</h3>

<p align="center">模型可以提案，但不能把未验证的效果说成交付。<br/>Xenon 把每一步工具执行变成 typed receipt，把每个 claim 绑定到 fact，把不能验证的部分显式变成 debt 或 abstain。</p>

<p align="center">
  <b>v1.0</b> · 单一内核循环 · 每轮入账 · TS 界面 · 可被其他 Agent 调用（MCP server）<br/>
  <b>4,000+</b> 测试 · <b>Python 3.10–3.12</b> · <b>Node.js 20+</b>（界面）
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="#作为-mcp-server">接入 Claude Code</a> ·
  <a href="#内核架构">内核架构</a> ·
  <a href="#证据与失败语义">证据</a> ·
  <a href="#评测">评测</a> ·
  <a href="docs/OPERATION_GUIDE.md">文档</a> ·
  <a href="https://github.com/xianyu-sheng/Xenon/issues">Issues</a> ·
  <a href="README.md">English</a>
</p>

> [!TIP]
> **The model proposes; the harness proves.** Xenon 不把 LLM 当裁判，也不让多个同构引擎各自解释工具。它只保留一个 `LoopRuntime`：模型输出 capability action，catalog 绑定工具，gateway 返回 receipt，controller 决定 repair/debt，`Finalizer` 独占交付提交。

## v1.0 意味着什么

1.0 不是加功能，而是把「凭什么叫完成」这件事收口成可检查的机制：

- **每一轮都进账本**——包括纯对话。`/evidence` 看到的是同一份真相，不是模型自述。
- **只有一条执行与验证链**：7 个同构引擎已删除，模式只是同一个循环的阶段参数。
- **交付有物理边界**：只有 `Finalizer` 能提交；preflight 审计不过就回滚本次 commit。
- **崩溃恢复精确到调用**：`tool_started` 无回执 → 标记「结果未知」并开 debt，绝不自动重放有副作用的调用。
- **凭证静态加密**：Windows DPAPI（可选 keyring），明文只在兜底时出现且被明确标注。
- **界面只有一套**：TypeScript（Ink）前端，构建产物随 wheel 分发；内置编辑器是显式 opt-in，不静默降级。
- **能力双向**：既能用别人的 MCP server，也能**作为 MCP server** 被 Claude Code 等 Agent 调用。

## 它和 Claude Code 有什么不同

能力面上两者接近：终端里跑、多轮、工具调用、MCP、计划模式、审批。如果你要的是一个**已经很好用的编码代理**，先用 Claude Code。

Xenon 想解决的是另一件事——**它凭什么说「做完了」**：

- **交付有物理边界**：只有 `Finalizer` 能提交；preflight 审计不过就回滚本次 commit，而不是返回一段“已完成”的文字。
- **claim 必须绑到 fact**：模型说「已创建 3 个文件」，这三个路径必须出现在 ledger 的 receipt 里，否则算 debt，挡住交付。
- **验证不了就 abstain**：语义类要求无法确定性验证时如实说「无法验证」，不包装成 pass。
- **每一步可回放**：一条 append-only ledger，`/evidence` 看到的是同一份真相，不是模型的自述。
- **能拦住的硬规则**：`git push`、破坏性命令**不经询问直接拒绝**；需要越权时走一次性授权，用完即撤销。

一句话：**Claude Code 让你更快地完成工作；Xenon 试图让「完成」这件事可以被检查。**

> 目前是 **research beta**：机制齐备、有真实 TTY 与真实任务证据（`docs/evidence/`），但内核路径尚未重跑 SWE-bench。别把它当生产工具用。

## 快速开始

需要 **Python 3.10–3.12** 与 **Node.js 20+**（交互界面是 TypeScript 前端；wheel 已内置构建产物）。

```bash
# 从 GitHub Releases 安装（不依赖 PyPI；wheel 自带界面，无需 npm）
pip install https://github.com/xianyu-sheng/Xenon/releases/download/v1.0.0/xenon_harness-1.0.0-py3-none-any.whl

xenon --version   # 1.0.0
xenon             # 首次运行：若未配置凭证，界面会提示输入 /setup
```

Windows 若提示 `'xenon' 不是内部或外部命令`，把 pip 输出的 Scripts 路径加入 `PATH`，或直接使用 `python -m xenon`。

```bash
# 源码安装
git clone --depth 1 https://github.com/xianyu-sheng/Xenon.git
cd Xenon && python -m venv .venv
.venv/Scripts/python -m pip install -e .        # Windows
source .venv/bin/activate && pip install -e .   # Linux / macOS

# 构建交互前端（源码检出没有构建产物；wheel 安装可跳过）
cd frontend && npm ci && npm run build:release && cd ..
```

首次启动后：`/setup` 选厂商、填 Key（掩码输入、静态加密保存）、选模型与模式。

## 作为 MCP server

Xenon 可以把「跑任务 + 读证据 + 审计别人的记录」暴露给其他 Agent：

```bash
xenon mcp-serve --workspace /path/to/repo --permission read-only
```

```bash
# 接入 Claude Code（本地配置）
claude mcp add xenon -- xenon mcp-serve --workspace /path/to/repo --permission read-only
```

三个工具：

| 工具 | 作用 |
|---|---|
| `xenon_run_task` | 在工作区跑一轮内核任务，返回账本指针与证据标签 |
| `xenon_evidence` | 读一份账本，给出 phase / 证据 / claim↔receipt / 实际调用 |
| `xenon_audit` | 审计另一个 Agent 的 transcript，区分「冲突」与「无法核验」 |

默认 **read-only 且 fail-closed**：没有交互式审批通道时，需要授权的调用直接失败并说明原因，不会挂起。
完整的安装、注册、权限、排错手册见 [MCP_SERVER](docs/MCP_SERVER.md)；
Claude Code 的真实握手记录见 [docs/evidence/mcp-claude-integration](docs/evidence/mcp-claude-integration/)。

## 内核架构

```text
                    ┌──────────── LoopRuntime（唯一 writer）────────────┐
 TaskSpec ─────────▶│ intake → running → tool_call → reconciling      │
                    │        → auditing → delivering → finalized      │
                    │ view      = Projection(ledger, catalog, spec)   │
                    │ action    = Strategy.propose(view)               │
                    │ decision  = Policy.check(action)                 │
                    │ receipt   = Gateway.execute(action)              │
                    │ verdict   = Controller.reconcile(...)            │
                    │ event     = Ledger.append(...)                   │
                    └─────────────────────────────────────────────────┘
          ▲                       │                        │
     Projections              ToolCatalog               EventSink
  ModelView / TaskState     core + MCP pack          TUI / JSONL / SDK
  PlanView / EvidenceView
```

### 七条不变量

- **I1 单 writer**：只有 `LoopRuntime` 能 append ledger。
- **I2 单入口**：所有工具副作用经过 `ToolGateway`；交付提交由 `Finalizer` 独占。
- **I3 单事实源**：ledger 是唯一真相；其他都是带 `input_seq` 的只读投影。
- **I4 语义接口**：kernel 只认 capability / obligation / receipt；工具名只活在 catalog。
- **I5 提案/事实分离**：LLM 输出是 `claim-*` / `hyp-*`；只有 binder 能产生 `fact-*`。
- **I6 单一决策权**：`VerificationController` 决定 repair / replan / abstain / abort。
- **I7 可回放**：同一 ledger 前缀产生逐字节一致的投影，回放过程零网络。

## 一个回合怎么走

```text
输入
  → TaskSpec：objective / user_objective / obligations / budget / permission
  → 模型提案：think / call_tool / submit_plan / update_plan / finish / abort
  → Catalog：capability → ToolSpec → ToolCall
  → Policy：allow / veto / require_approval
  → Gateway：七阶段 ToolExecutor → typed ToolReceipt
  → Controller：V0 receipt 不变量 / V1 step 后置条件 / V2 coverage
      bound         → 继续
      unresolved    → debt / repair / replan
      ambiguous     → abstain，不猜
      abstained     → 询问用户或保留 debt
  → Finalizer：preflight audit → intent → commit → receipt → final decision
      non-deliver   → 回滚本次 commit
  → Projection：/evidence /goal /resume /plan 读取同一 ledger
```

## 工具与 MCP

- `ToolCatalog` 用 **8 类 capability** 表达工具语义：`read / search / discover / edit / verify / vcs / shell / remote`；
- `discover`（`find_tools`）在调用前按意图查询目录，返回有界候选，所有权限模式下免费；
- 工具名只在 catalog/spec 出现，kernel 不分支具体工具名；
- MCP 使用 **per-tool ToolSpec**，不是把所有工具都塞给模型；
- 工具预算按 objective 做确定性选择（例如 46 个 GitHub 工具只注入 16 个），未知工具 `unresolved`；
- approval 是外部 MCP 调用的边界；每次授权绑定目标并带 TTL，调用后立即撤销；
- `git push` 在显式 `TaskSpec.allow_push` 前保持 veto。

## 证据与失败语义

| 层级 | 检查 | 权威 |
|---|---|---|
| **V0** | exit code、no-op write、target 匹配、真实 test target、commit sha、worktree delta | controller |
| **V1** | step 的 `expects` 是否被 fact 关闭 | controller |
| **V2** | 全局 obligation coverage、负事实、semantic gap | deterministic binder + user abstention |

- 工具失败不会被关键词猜成成功；
- 零匹配是 `unresolved`，多匹配是 `ambiguous`，两者都不会静默取一个；
- 无法确定性验证的语义要求是 `abstained`，不是 pass；
- debt 会阻止交付，直到有 fact 或用户明确接受；
- 哈希链能证明过程未被篡改，但不证明模型结论正确。

```text
/evidence   查看当前 kernel ledger、V2 coverage、claims、debt 与链核验
/goal       查看目标/阶段/计划/未结债务
/resume     只恢复 ledger pointer，不重放已执行副作用
```

## 评测

固定协议，逐项冻结，任何人可复现：

| 项 | 取值 |
|---|---|
| 协议 | 完全盲测：解题阶段不应用官方 test patch，prompt 不含 FAIL_TO_PASS 名称 |
| 循环 | 单一 `LoopRuntime`；模式只是它的阶段配置 |
| 模型 | `deepseek-v4-flash`，官方 API |
| 判定 | 官方 `swebench` harness；FAIL_TO_PASS 全过且 PASS_TO_PASS 无回归 |
| 重复 | 3 次独立运行取均值；逐次结果公开 |
| 记录时间 | 2026-10-16（v0.9.4 阶段） |

| 样本 | 本轮框架 | 上一代 v0.8.5 |
|---|---:|---:|
| 固定 30 例（seed 20260729） | **62.2%** | 63.3% |
| 零重叠留出集（seed 20261016） | **52.2%** | 46.7% |

> **适用范围**：这两个数字测的是 2026-10-16 的框架，**不是 1.0 的单循环内核**。
> 内核成为默认路径后还没用同一协议重跑；在那之前，请把它们当作「上一代框架的水平」。
> 重跑已列入路线（见 Issues），结果无论好坏都会公开。

完整协议、逐次结果、判定日志与 sha256 归档见 [EVAL_RESULTS](docs/EVAL_RESULTS.md)。

## 收录

- [Awesome-MCP-ZH](https://github.com/yzfly/Awesome-MCP-ZH) — 中文 MCP 资源精选（MCP 客户端 → 可验证的终端编码代理）
- [awesome-opensource-ai](https://github.com/alvinreal/awesome-opensource-ai) — 开源 AI 项目清单（Autonomous Coding Agents）
- [awesome-mcp-lists](https://github.com/collabnix/awesome-mcp-lists) — Collabnix 社区 MCP 客户端 / 工具清单
- [awesome-ai-coding-tools](https://github.com/mahmoudsajjadi/awesome-ai-coding-tools) — AI 编码工具 CLI agents 清单

## 常用命令

输入 `/` 直接弹出**命令面板**（支持 `perm` 这类前缀/缩写过滤，`Enter` 补全、再回车执行）；输入 `@` 触发文件路径补全（`↑/↓` 选择，`Tab` 补全）；`Shift+Tab` 在模式间循环。

```text
/evidence     证据链 / kernel ledger / V2 coverage
/goal         目标、阶段、计划、债务
/resume       恢复会话与 ledger pointer
/plan         计划模式（软引导）
/mode         选择模式（单循环的阶段配置：direct / plan-execute / …）
/permissions  审批策略 + 当前会话授权（可逐条撤销）
/memory       跨会话记忆：用户/项目/会话作用域（教训就是记忆记录）
/model        切换模型池里的当前模型
/sessions     会话列表与恢复
/mcp          MCP server 与工具管理
/rewind       回退到回合节点
/fork         从回合节点分叉
/compact      压缩长对话
/tools        查看工具与 capability
```

## 文档

| 主题 | 文档 |
|---|---|
| 操作手册 | [OPERATION_GUIDE](docs/OPERATION_GUIDE.md) · [GUIDE](docs/GUIDE.md)（英文快速开始） |
| 架构设计 / ADR | [ARCHITECTURE](docs/ARCHITECTURE.md) · [ADR 0001](docs/adr/0001-single-loop-kernel.md) |
| Harness / 内核说明 | [HARNESS](docs/HARNESS.md) |
| MCP server（接入其他 Agent） | [MCP_SERVER](docs/MCP_SERVER.md) |
| 外部审计（审计别人的记录） | [EXTERNAL_AUDIT](docs/EXTERNAL_AUDIT.md) |
| 扩展开发 | [EXTENDING](docs/EXTENDING.md) |
| 评测 | [EVAL_PROTOCOL](docs/XENON_EVAL_PROTOCOL.md) · [EVAL_RESULTS](docs/EVAL_RESULTS.md) |
| 真实 TTY 证据 | [docs/evidence](docs/evidence) |
| 更新日志 | [CHANGELOG](CHANGELOG.md) |

## 诚实边界

- **这是 research beta。** 机制齐备、有真实 TTY 与真实任务的证据，但覆盖面仍窄；
  内核路径没重跑 SWE-bench，也没有多用户/长周期验证。生产环境请用成熟工具。
- **哈希链证明过程，不证明结论。** 它能发现损坏、单点篡改与重排；防不住有写权限的人整链重算，除非启用可选的 HMAC 锚点。
- **abstain 是产品行为。** 无法确定性验证的语义要求不会被包装成 pass。
- **审批不是 sandbox。** 外部 MCP server 仍需在受控环境运行；approval 只是调用边界。
- **SWE-bench 数字有样本和预算限制。** 请按 EVAL_RESULTS 的协议阅读，不要把 30 例子集当成榜单结论。

## License

MIT
