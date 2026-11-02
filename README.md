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
  <b>SWE-bench Lite 盲测</b>：固定 30 例 <b>62.2%</b> · 零重叠留出集 <b>52.2%</b> · <b>4,000+</b> 测试 · <b>Python 3.10–3.12</b>
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="#内核架构">内核架构</a> ·
  <a href="#证据与失败语义">证据</a> ·
  <a href="#评测">评测</a> ·
  <a href="docs/OPERATION_GUIDE.md">文档</a> ·
  <a href="https://github.com/xianyu-sheng/Xenon/issues">Issues</a> ·
  <a href="README.en.md">English</a>
</p>

> [!TIP]
> **The model proposes; the harness proves.** Xenon 不把 LLM 当裁判，也不让多个同构引擎各自解释工具。它只保留一个 `LoopRuntime`：模型输出 capability action，catalog 绑定工具，gateway 返回 receipt，controller 决定 repair/debt，`Finalizer` 独占交付提交。

## 快速开始

```bash
# 从 GitHub Releases 安装（不依赖 PyPI）
pip install https://github.com/xianyu-sheng/Xenon/releases/download/v0.9.5/xenon_harness-0.9.5-py3-none-any.whl

xenon   # 首次运行会引导配置 API Key、模型与运行入口
```

Windows 若提示 `'xenon' 不是内部或外部命令`，把 pip 输出的 Scripts 路径加入 `PATH`，或直接使用 `python -m xenon`。

```bash
# 源码安装
git clone --depth 1 https://github.com/xianyu-sheng/Xenon.git
cd Xenon && python -m venv .venv
.venv/Scripts/python -m pip install -e .        # Windows
source .venv/bin/activate && pip install -e .   # Linux / macOS
```

## 为什么是单循环

旧版 Xenon 有 7 个“推理引擎”。`engine/registry.py` 自己承认：七种内置范式**运行流程完全同构**，差异只有引擎类、少量参数、模式行和面板标题。结果是同一套循环被复制 7 次，工具语义散落在 prompt、plan 校验、TaskState、ClaimGate、delivery 五处。

现在只剩一条控制流：

```text
TaskSpec
  → LoopRuntime（唯一 writer，显式 phase machine）
      view      = Projection(ledger[0..seq], catalog, spec)
      action    = Strategy.propose(view)        # 模型只提案
      decision  = Policy.check(view, action)    # 确定性 veto / approval
      receipt   = Gateway.execute(action)       # 唯一副作用入口
      verdict   = Controller.reconcile(...)     # V0 / V1 / V2
      event     = Runtime.append(...)           # 唯一 durable write
  → Finalizer（只 stage 已验证路径，生成交付提交）
```

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

- `ToolCatalog` 用 **7 类 capability** 表达工具语义：`read / search / edit / verify / vcs / shell / remote`；
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
| 引擎 | 单一 `react`（不是多引擎并集 / best-of-N） |
| 模型 | `deepseek-v4-flash`，官方 API |
| 判定 | 官方 `swebench` harness；FAIL_TO_PASS 全过且 PASS_TO_PASS 无回归 |
| 重复 | 3 次独立运行取均值；逐次结果公开 |

| 样本 | 本轮框架 | 上一代 v0.8.5 |
|---|---:|---:|
| 固定 30 例（seed 20260729） | **62.2%** | 63.3% |
| 零重叠留出集（seed 20261016） | **52.2%** | 46.7% |

完整协议、逐次结果、判定日志与 sha256 归档见 [EVAL_RESULTS](docs/EVAL_RESULTS.md)。

## 收录

- [Awesome-MCP-ZH](https://github.com/yzfly/Awesome-MCP-ZH) — 中文 MCP 资源精选（MCP 客户端 → 可验证的终端编码代理）
- [awesome-opensource-ai](https://github.com/alvinreal/awesome-opensource-ai) — 开源 AI 项目清单（Autonomous Coding Agents）
- [awesome-mcp-lists](https://github.com/collabnix/awesome-mcp-lists) — Collabnix 社区 MCP 客户端 / 工具清单
- [awesome-ai-coding-tools](https://github.com/mahmoudsajjadi/awesome-ai-coding-tools) — AI 编码工具 CLI agents 清单

## 常用命令

```text
/evidence   证据链 / kernel ledger / V2 coverage
/goal       目标、阶段、计划、债务
/resume     恢复会话与 ledger pointer
/plan       计划模式（软引导）
/mode       选择推理范式（react / plan-execute / ...）
/mcp        MCP server 与工具管理
/rewind     回退到回合节点
/fork       从回合节点分叉
/compact    压缩长对话
/tools      查看工具与 capability
```

## 文档

| 主题 | 文档 |
|---|---|
| 操作手册 | [OPERATION_GUIDE](docs/OPERATION_GUIDE.md) |
| 架构设计 / ADR | [ARCHITECTURE](docs/ARCHITECTURE.md) · [ADR 0001](docs/adr/0001-single-loop-kernel.md) |
| Harness / 内核说明 | [HARNESS](docs/HARNESS.md) |
| 扩展开发 | [EXTENDING](docs/EXTENDING.md) |
| 评测 | [EVAL_PROTOCOL](docs/XENON_EVAL_PROTOCOL.md) · [EVAL_RESULTS](docs/EVAL_RESULTS.md) |
| 真实 TTY 证据 | [docs/evidence](docs/evidence) |
| 更新日志 | [CHANGELOG](CHANGELOG.md) |

## 诚实边界

- **哈希链证明过程，不证明结论。** 它能发现损坏、单点篡改与重排；防不住有写权限的人整链重算，除非启用可选的 HMAC 锚点。
- **abstain 是产品行为。** 无法确定性验证的语义要求不会被包装成 pass。
- **审批不是 sandbox。** 外部 MCP server 仍需在受控环境运行；approval 只是调用边界。
- **SWE-bench 数字有样本和预算限制。** 请按 EVAL_RESULTS 的协议阅读，不要把 30 例子集当成榜单结论。

## License

MIT
