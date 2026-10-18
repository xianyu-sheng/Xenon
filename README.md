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

<h3 align="center">把"机器到底验证了什么"摊开给你看的终端 AI 编码代理</h3>

<p align="center">别的 agent 说"完成了"，你只能选择相信它。<br/>Xenon 把结果拆成两半说清楚：哪些是机器确认过的，哪些需要你自己看一眼。</p>

<p align="center"><b>SWE-bench Lite 盲测</b>（单一 <code>react</code> 引擎 · 120 步 · 官方 harness · 3 次均值）：固定 30 例 <b>62.2%</b> · 零重叠留出集 <b>52.2%</b> · <a href="#评测">协议与全部逐次结果</a></p>

<p align="center">
  <a href="docs/OPERATION_GUIDE.md">文档</a> ·
  <a href="#快速开始">快速开始</a> ·
  <a href="CHANGELOG.md">更新日志</a> ·
  <a href="https://github.com/xianyu-sheng/Xenon/issues">问题反馈</a> ·
  <a href="https://github.com/xianyu-sheng/Xenon/releases">Releases</a> ·
  <a href="README.en.md">English</a>
</p>

> [!TIP]
> **机器只对过程负责，语义判断交还给你。** 每次工具调用都进证据链（L0）；全仓只有一个阻断类校验（L1）；语义问题不阻断交付，整理成「待人工核验清单」随答案一起交给你（L2）。

<p align="center">
  <img src="docs/hero-terminal.svg" alt="Xenon 终端：一次交付里机器确认了什么、哪两项需要人工核验" width="860">
</p>

## 快速开始

```bash
# 从 GitHub Releases 安装（不依赖 PyPI）
pip install https://github.com/xianyu-sheng/Xenon/releases/download/v0.9.4/xenon_harness-0.9.4-py3-none-any.whl

xenon   # 首次运行会引导配置（API Key、模型、推理引擎）
```

> **Windows：** 若提示 `'xenon' 不是内部或外部命令`，把 pip 打印的 Scripts 路径加进 PATH，或直接使用 `python -m xenon`（v0.9.4 起随发布产物提供）。混用 conda 与独立 Python 时建议用专用 venv：`python -m venv .venv`。

更多安装方式（源码 / uv / pipx）见 [安装](#安装)。

## 评测

### 协议（逐项冻结，可复现）

| 项 | 取值 |
|---|---|
| 协议 | **完全盲测**：解题阶段不应用官方 test patch，提示词不含 FAIL_TO_PASS 测试名（`XENON_EVAL_APPLY_TEST_PATCH=0`、`XENON_EVAL_FAIL_HINT=0`） |
| 引擎 | **单一 `react`** —— 不是多引擎并集、不是 best-of-N。这是**框架 A/B**：引擎固定，差异才能归因到框架代码本身 |
| 模型 | `deepseek-v4-flash` 官方 API，`--reasoning-effort off` |
| 预算 | 120 步（`STANDARD_STEP_BUDGET`），引擎 900s 硬墙 |
| 判定 | 官方 `swebench` harness：干净容器 + 模型补丁 + 官方 test patch；`FAIL_TO_PASS` 全过且 `PASS_TO_PASS` 无回归才算 resolved |
| 分母 | `resolved / submitted(=30)`；空补丁单独统计但**仍算未解出**（保守口径） |
| 重复 | 每条件 **3 次独立运行**取均值（逐次结果全部公开；单次极差可达 4 例 ≈ 13pp） |

### 两套样本

| 样本 | 抽样 | 说明 |
|---|---|---|
| 固定 30 例 | `evals/swebench_select.py --size 30 --seed 20260729` | 历史基线；提交历史点名其中 9/30 例 → 有样本内调优痕迹 |
| **零重叠留出集** | 同规则 `--seed 20261016 --exclude` 原样本（池 270 例） | **主证据**，与原样本零重叠 |

两份 manifest（含 digest）已提交，任何人可逐例复现。

### 结果

| 样本 | 本轮框架 | 上一代 v0.8.5 |
|---|---|---|
| 固定 30 例（seed 20260729） | **62.2%** | 63.3% |
| 零重叠留出集（seed 20261016） | **52.2%** | 46.7% |

> 因子分解（同模型 / 同实例 / 同 harness）：框架迭代 **0pp**（p=1.0）· 预算 10→120 步 **+36.7~46.7pp** · 解题时测试可见 **+16.7pp**（非标准协议，仅作上界）。→ **瓶颈在预算与模型，不在脚手架。**

### 用更强的模型来打我们的脸

单次运行（30 例、flash 模型）API 成本约 **¥3–5**，前沿模型约 ¥50–200/次；我们只有 flash 的预算。如果你有更强的模型，欢迎在同一 manifest、同一协议上复现：

```bash
git clone --depth 1 https://github.com/xianyu-sheng/Xenon.git && cd Xenon
python -m venv .venv && .venv/bin/pip install -e . && .venv/bin/pip install "swebench==4.1.0" datasets docker

IDS=$(python -c "import json;print(' '.join(json.load(open('evals/manifests/swebench-lite-30-heldout-seed20261016.json'))['instance_ids']))")
ARGS=(); for i in $IDS; do ARGS+=(--instance-id "$i"); done

XENON_EVAL_APPLY_TEST_PATCH=0 XENON_EVAL_FAIL_HINT=0 \
.venv/bin/python evals/swebench_xenon.py "${ARGS[@]}" \
  --prepared-root ./work --engines react --max-steps 120 \
  --model <your-model> --reasoning-effort off \
  --request-timeout 60 --engine-timeout 900 \
  --predictions preds.jsonl --traces traces.jsonl

python -m swebench.harness.run_evaluation --dataset_name SWE-bench/SWE-bench_Lite --split test \
  --predictions_path preds.jsonl --run_id my-run --max_workers 2 --cache_level env
```

**完整分步指南（含验收清单与我们踩过的 7 个坑）**：[REPRODUCE_SWEBENCH](docs/REPRODUCE_SWEBENCH.md)。

跑完把 `run_id`、模型名和官方报告贴到 [#162](https://github.com/xianyu-sheng/Xenon/issues/162)——**协议已冻结，换模型就能直接比**；我们会把数字并排进上表（注明模型与协议）。

完整协议、逐次结果、判定日志与 sha256 归档：[EVAL_RESULTS](docs/EVAL_RESULTS.md)。


## 它具体做什么

<table>
<tr><td><b>证据链，而不是日志</b></td><td>每一次工具调用写入 append-only 哈希链（内容哈希 + 前驱哈希）；<code>/evidence</code> 随时重算自证，能发现损坏、单点篡改与重排。</td></tr>
<tr><td><b>只有一个阻断类校验</b></td><td>"空洞回答"（没干活、答非所问）是确定性的，命中就同回合反馈重试、同因熔断；语义类问题不阻断交付，整理成「待人工核验清单」随答案输出。</td></tr>
<tr><td><b>每个回合是一棵树上的节点</b></td><td><code>/rewind</code> 回退、<code>/fork</code> 分叉；中断或熔断后，裸敲"继续"会自动从那个节点接上，不用重述上下文。</td></tr>
<tr><td><b>越权操作要走审批</b></td><td>工作区内写文件不打扰你；越界写和命令会弹统一面板（带 diff 预览），授权可按目标与时长限定，子代理默认只读。</td></tr>
<tr><td><b>不可信输出带来源标记</b></td><td>文件、网页、GitHub、MCP 与命令输出都标注来源与可信度；命中常见提示注入模式时给出可见告警，审批面板注明"该动作可能受不可信内容影响"。只提醒，不阻断，也不改写原内容。</td></tr>
<tr><td><b>路由会从你的选择中学习</b></td><td>智能路由的推荐被 <code>/mode</code> 覆盖后，同类任务下偏好范式获得小幅置信度加权；偏好只留在本地，删 <code>~/.xenon/routing_preferences.json</code> 即重置。</td></tr>
<tr><td><b>截断与上限如实标注</b></td><td>大文件截断、上下文超限都明确标出来，不伪装完整。</td></tr>
</table>

## 一个回合怎么走

```text
你的输入
  → 意图判定（LLM 分类为主，失败自动降级正则）
  → 引擎执行（预算检查点 / 循环检测 / 断路器）
  → 回合校验（唯一判定点）
      pass    → 正常交付
      remind  → 交付 + 待人工核验清单（不阻断）
      fail    → 同回合重试，预算用尽则降级交付
      fuse    → 同因结构失败熔断，按未完成交付
  → 会话树落盘 + 哈希链追加 + 回合归档
```

## 机器验证了什么，没验证什么

| 层 | 测的是什么 | 结果怎么呈现 |
|---|---|---|
| **L0 证据层** | 过程是否真实、可复核 | 哈希链事件日志；`/evidence` 随时重算 |
| **L1 结构层** | 回答是不是"没干活" | 唯一的阻断类校验：反馈重试 / 同因熔断 |
| **L2 语义层** | 宣称、准则、文件声称是否站得住 | 不阻断；命中即附「待人工核验清单」，重试耗尽则降级交付 |

语义判断全仓只有一处（`xenon/turn/gate.py`），引擎内部不做语义判定；工具成败只由工具回执决定，不靠关键词猜。

`/evidence` 的输出（结构化回合摘要 + 一行链自证）：

<img src="docs/evidence-chain.png" alt="/evidence 输出示例" width="720">

## 常用命令

```
/evidence   查看证据链（当前 / 全局 / 指定会话）      /plan   计划模式
/goal       设定本轮目标                             /review 审阅改动（含 .bak 回退）
/rewind     回退到某个回合                           /fork   从某个回合分叉
/sessions   会话列表                                 /compact 压缩长对话
/permissions 查看当前授权                           /tools  工具清单
```

启动时先选会话：直接敲 `xenon` 会弹会话选择器（数字选历史会话或新对话），`xenon resume` 恢复最近一次。恢复后会回放对话历史。

## 安装

```bash
# 方式一：从 GitHub Releases 安装 wheel（推荐，不依赖 PyPI）
pip install https://github.com/xianyu-sheng/Xenon/releases/download/v0.9.4/xenon_harness-0.9.4-py3-none-any.whl
xenon --version   # 验证安装

# 方式二：源码安装（editable，浅克隆省流量）
git clone --depth 1 https://github.com/xianyu-sheng/Xenon.git
cd Xenon && python -m venv .venv
.venv/Scripts/python -m pip install -e .        # Windows
source .venv/bin/activate && pip install -e .   # Linux / macOS
```

也可以用 `uv tool install <wheel URL>` 或 `pipx install <wheel URL>`，入口会自动处理。

## 文档

| 主题 | 中文 | English |
|---|---|---|
| 操作手册 / Quick Start | [OPERATION_GUIDE](docs/OPERATION_GUIDE.md) | [GUIDE](docs/GUIDE.md) |
| 架构设计 | [ARCHITECTURE](docs/ARCHITECTURE.md) | [ARCHITECTURE.en](docs/ARCHITECTURE.en.md) |
| 扩展开发（MCP / 子代理 / 钩子 / 自定义命令） | [EXTENDING](docs/EXTENDING.md) | [EXTENDING.en](docs/EXTENDING.en.md) |
| 与 Aider / Claude Code / OpenCode / Crush 对比 | [COMPARISON](docs/COMPARISON.md) | [COMPARISON.en](docs/COMPARISON.en.md) |
| 工具输出威胁模型 | [THREAT_MODEL](docs/THREAT_MODEL.md) | — |
| 开发者指南（测试配方 / 平台差异 / 品牌管线） | [DEVELOPMENT](docs/DEVELOPMENT.md) | — |
| 观测层（AgentOps）设计 P0 | [OBSERVABILITY](docs/OBSERVABILITY.md) | — |
| TUI 设计与操作 | [TUI](docs/TUI.md) | — |
| 评测协议 / 评测结果 | [EVAL_PROTOCOL](docs/XENON_EVAL_PROTOCOL.md) · [EVAL_RESULTS](docs/EVAL_RESULTS.md) | — |
| 更新日志 | [CHANGELOG](CHANGELOG.md) | — |

## 环境变量

<details>
<summary>共 16 项（点击展开）</summary>

| 变量 | 作用 | 默认 |
|---|---|---|
| `XENON_APPROVAL_TTL` | 会话级 `a` 授权规则的有效期（秒） | 1800 |
| `XENON_VERIFY_RETRIES` | 结构层失败后的同回合重试次数 | 2 |
| `XENON_RETRY_BUDGET_MIN/MAX` | 重试轮的步数预算区间 | 10 / 40 |
| `XENON_GATE_FUSE_THRESHOLD` | 同因结构失败的熔断阈值 | 2 |
| `XENON_MAX_ITERATIONS` | 单次运行的步数上限（所有引擎统一 120） | 120 |
| `XENON_ENGINE_TIMEOUT` | 单次运行的引擎绝对时限（秒）；超时返回已完成部分 | 600 |
| `XENON_MAX_TOKENS` | 单次模型调用的输出上限 | 8192 |
| `XENON_MAX_CONTINUATIONS` | 截断后的自动续写次数 | 3 |
| `NO_COLOR` | 标准环境变量；关闭输出与输入提示的颜色 | 未设置 |
| `XENON_READ_ONLY` | 只读模式（禁用一切写/执行工具） | 关 |
| `XENON_COMMAND_ALLOWLIST` | 命令首词白名单（逗号分隔） | 空 = 不限 |
| `XENON_EXTRA_ROOTS` | 启动目录之外额外允许文件工具读写的根目录（Windows 用 `;` 分隔） | 空 = 仅启动目录及其子目录 |
| `XENON_SESSION_EVENTS(_DIR)` | 事件日志开关 / 目录 | 开 |
| `XENON_HOOKS` | 本地 hooks 开关 | 开 |
| `XENON_ATTRIBUTION` | 提交与 PR 是否附带 Xenon 署名（`0` 关闭） | 开 |
| `GITHUB_TOKEN` / `GH_TOKEN` | GitHub 工具认证（提升 API 限额；未设置时自动回退到已登录的 `gh auth token`） | 空 |

</details>

## 平台支持

- **Windows**：Windows Terminal (ConPTY) + PowerShell 全功能支持（粘贴、中文输入法、方向键）；`python -m xenon` 自 v0.9.4 起随发布产物提供。
- **Linux / macOS**：常规终端即可；`XENON_TERMINAL_ANIMATION=0` 关闭启动动画。

## 诚实边界

- **机器不验证语义。** L2 命中时只提醒不阻断——内容真伪请对照「待人工核验清单」自行确认。
- **哈希链证明的是过程，不是结论。** 它保证事件流未被篡改，不保证模型答对了。
- **链本身是无密钥锚点的完整性检查。** 能发现偶然损坏、单点篡改和重排，防不住"有写权限的人整链重算"；需要强抗篡改请把事件目录放到受控存储。（可选 HMAC 锚点见 #106）
- **沙箱是检测 + 软件边界，不是真隔离。** 不可信代码请在 WSL / Docker 容器里运行。
- **工具输出的注入检测是启发式提醒，不是安全边界。** Xenon 会标记来源并提示常见注入模式，但不会阻断或改写内容；范围与限制见[工具输出威胁模型](docs/THREAT_MODEL.md)。
- `/review` 的回退只覆盖有 `.bak` 备份的编辑；版本管理仍请交给 git。
- 意图分类器默认走最快可用的模型，约 1 秒延迟；不可用时会降级为正则（偏保守）。

## 参与贡献

- 贡献流程与 PR 的 CI 证据要求：[CONTRIBUTING](CONTRIBUTING.md)（含 English 段落）
- 本地开发、测试配方与平台差异：[DEVELOPMENT](docs/DEVELOPMENT.md)
- 适合新人的任务：[good first issue](https://github.com/xianyu-sheng/Xenon/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22)
- 贡献者入口（任务清单与认领方式）：[#144](https://github.com/xianyu-sheng/Xenon/issues/144)
- 功能建议 / 体验反馈 / 路线图讨论：[路线图与反馈收集](https://github.com/xianyu-sheng/Xenon/discussions/143)
- 质量门禁：Linux 全量离线测试 + Windows/macOS 平台冒烟 + Codecov 覆盖率；离线套件 3,000+ 用例
- 如果 Xenon 对你有帮助，欢迎点一个 ⭐

## 收录

<!-- 按榜单影响力排序；新收录在合并后插入到对应位置 -->

- [Awesome-MCP-ZH](https://github.com/yzfly/Awesome-MCP-ZH) — 中文 MCP 资源精选（MCP 客户端 → 可验证的终端编码代理）
- [awesome-opensource-ai](https://github.com/alvinreal/awesome-opensource-ai) — 开源 AI 项目清单（Agentic AI & Multi-Agent Systems → Autonomous Coding Agents）
- [awesome-mcp-lists](https://github.com/collabnix/awesome-mcp-lists) — Collabnix 社区的 MCP 客户端 / 工具清单（MCP Clients → Command Line Tools）
- [awesome-ai-coding-tools](https://github.com/mahmoudsajjadi/awesome-ai-coding-tools) — AI 编码工具三层分类清单（CLI coding agents；致谢 @xianyu-sheng）

## License

MIT
