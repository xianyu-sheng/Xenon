# Xenon 评测结果

> 真实模型跑 ReAct 多轮闭环（方案 C 修复后），按**实际执行**的工具评分（`expected_tools ⊆ executed` 且 `final_answer` 非空）。
> 数据生成：2026-07-08 10:10 UTC，DeepSeek-V4-Pro via 火山方舟。
> **不**美化、不补全——这是真证据。
>
> **基线状态：** 本文记录的是历史 v0.7.2/v2 跑测，不是 v0.7.3 当前成功率。
> v0.7.3 的真实评测需要在明确的供应商凭证、模型、费用和隔离工作目录下重新执行；
> 在重跑前，不应把 45.0% 当作当前版本指标。

## v0.7.3 当前跑测（2026-07-27）

| 提交 | 模型 | 任务 | 成功率 | 平均 Token | Tool Calls | Tool Failures |
|------|------|------:|--------:|-----------:|-----------:|--------------:|
| `a1226c1` | `deepseek/deepseek-v4-pro` | 20 | **55.0%（11/20）** | 296.1 | 237 | 11 |

按类别：`file_edit` 1/4、`code_search` 3/4、`context_memory` 3/4、`tool_call` 3/4、`multi_turn_revision` 1/4。
相对历史 v2 的 45.0%（9/20）提升 10 个百分点，但该分数仍受任务集路径和前置材料不匹配影响，不能单独视为端到端用户成功率。

评测期间发现 MCP 任务会写持久化配置；本次使用隔离目录执行后已清理临时条目。后续真实评测必须通过 runner 的凭证隔离，不得直接使用用户的 `~/.xenon/credentials.yaml`。

### 缓存前缀稳定性修复后的跑测（2026-07-29，当前基准）

| 指标 | 结果 |
|------|------|
| 验证通过率（机器断言） | **65.0%（13/20）** |
| 工具执行成功率 | **95.09%**（112 次调用 / 8 次失败） |
| 结果断言通过率 | **90%** |
| 厂商上报缓存命中率 | **97.35%**（字段覆盖 100%） |
| 可复用前缀 token | **1,152,256** |
| 实际成本 / 全 miss 基线 | **¥0.2567 / ¥3.6847** |
| 节省 | **¥3.4280（93.03%）** |
| 轨道数 / 分叉数 | 3 / 14 |
| 观测 LLM 调用 | 131 |

报告：`evals/reports/official/2026-07-29-react-real/report.md`

**这次跑测修正了一个此前让缓存指标失真的缺陷。** `ReActEngine._build_system_prompt()`
把 `datetime.now().strftime('%H:%M:%S')` 注入 system prompt 的运行环境段，而 REPL
每轮新建引擎（`repl.py:2643`）。system prompt 属于 Cache Rails 的稳定前缀，参与轨道
契约哈希，因此**每轮都会分叉**，跨轮 `reusable_tokens` 恒为 0，每轮都报 `cold_start`。

修复前 README/本文档报告的 85.98% 命中率，实际只反映**单次 run 内部**多轮 ReAct
迭代共享同一引擎实例的复用，**不是跨轮复用**——这两件事差别很大。修复方式是改用
`date.today()`（精度到天），精确时间交由 `command` 工具（`date` / `Get-Date`）实时获取。
`tests/test_cache_prompt_stability.py` 锁定该不变量：同一天内多次实例化的 system prompt
必须完全一致，且不得包含 `HH:MM:SS` 模式。

### 历史记录：Xenon 专属价值指标跑测（2026-07-27，含上述缺陷）

修正聚合口径并补齐评测回调后，真实跑测结果为：通用任务成功率 **45.0%（9/20）**；
CacheTracker 缓存字段覆盖 100%，Cache Rails 命中率 **85.98%**，可复用前缀命中
**1,416,064 tokens**。估算实际费用 **¥1.0684**，全 prompt cache-miss 基线 **¥5.2812**，
预计节省 **¥4.2128（79.77%）**。同时观测到 1 次 Rail 分叉、2 次参数拦截和 2 次路径越界拦截。

> 保留此条用于对比。其缓存命中率受上述 system prompt 时间戳缺陷影响，不代表跨轮复用能力。

评测报告将 CacheTracker 作为缓存命中/费用的唯一来源，把 UsageTracker 作为总 Token/延迟来源，
避免原生 tool-call 路径下两套回调产生口径差异。任务成功率与 Xenon 价值指标保持独立，不合并成一个含义不清的总分。

---

## 一、v1 → v2 概览（方案 C 修复效果）

| 版本 | 成功率 | Tool Calls | Tool Failures | 备注 |
| --- | ---: | ---: | ---: | --- |
| **v1**（3 文件 workdir / 单轮 / 旧拒绝兜底） | 25.0% (5/20) | 56 | 18 | 任务前提多不满足、LLM 易文字声称完成 |
| **v2**（xenon 自身 workdir / 3 轮 multi-turn / 自适应拒绝兜底） | **45.0% (9/20)** | 160 | 13 | **+80%**，3 个根因都修了 |

**3 个根因修复的实际效果**：

| 根因 | v1 受影响 | v2 修复效果 | 代表任务 |
| --- | ---: | --- | --- |
| **根因 1**：RealAgent 单轮不友好 | 5 任务失败 | 1 任务改判成功 | `generate-diff-preview`（旧拒绝兜底 + 新重试次数 → 强制 LLM 调工具） |
| **根因 2**：workdir 太简单 | 6 任务失败 | 5 任务改判成功 | `use-project-rules`（workdir 加 `.xenon/rules.md`）、`code-search-entrypoint`（workdir 是 xenon 自身有 main.py） |
| **根因 3**：ReAct 拒绝兜底固定 2 次 | 3 任务失败 | 2 任务改判成功 | `generate-diff-preview`（重试上限 2→4） |

---

## 二、跑测环境

| 项目 | 值 |
| --- | --- |
| **模型** | `deepseek/deepseek-v4-pro` |
| **API 网关** | 火山方舟 (`https://ark.cn-beijing.volces.com/api/coding`) |
| **引擎** | `ReActEngine`（`max_iterations=8` / `max_turns=3`） |
| **工作目录** | `/tmp/xenon_real_workdir`（**xenon 自身 132 文件** + `.xenon/rules.md`） |
| **任务集** | `evals/tasks.yaml`（20 任务，6 类别） |
| **框架** | `python evals/runner.py --mode real`（方案 C 修复后） |
| **跑测时间** | 2026-07-08 10:10:13 UTC |
| **隔离** | workdir 与原仓库**完全隔离**——cp 一份到 `/tmp/`，原仓库零污染 |

---

## 三、Mock 模式（框架自检）

> ⚠️ **Framework smoke test — NOT an agent capability measurement.**

| 指标 | 值 |
| --- | --- |
| Tasks | 20 |
| Success Rate | 100.0% |
| Average Tokens | 112.0 |
| Tool Calls | 36 |
| Tool Failures | 0 |

---

## 四、Real 模式 v2 结果

| 指标 | v1 | v2 | Δ |
| --- | ---: | ---: | ---: |
| **Tasks** | 20 | 20 | — |
| **Success Rate** | 25.0%（5/20） | **45.0%（9/20）** | **+80%** |
| **Average Tokens** | 260.4 | 298.5 | +15%（multi-turn 累积） |
| **Tool Calls** | 56 | 160 | +186%（multi-turn 多轮） |
| **Tool Failures** | 18 | 13 | -28%（workdir 改善 + ReAct 兜底改善） |

### 4.1 9 个成功任务（v2）

| Task | Category | Tool Calls | Notes |
| --- | --- | ---: | --- |
| `update-readme-command` | file_edit | 10 | LLM 真读了 README + CHANGELOG 改了用法 |
| `code-search-entrypoint` | code_search | 16 | 找到 `xenon.main:cli` 入口 |
| `code-search-model-router` | code_search | 6 | 找到 `llm_client.py` 路由逻辑 |
| `run-focused-tests` | tool_call | 5 | pytest 19 测试全过 |
| `inspect-git-status` | tool_call | 3 | 正确识别非 git 仓库 |
| `generate-diff-preview` | tool_call | 3 | **v1 失败→v2 成功**（ReAct 拒绝兜底改进） |
| `call-weather-tool` | tool_call | 1 | wttr.in 北京 |
| `use-project-rules` | context_memory | 13 | **v1 失败→v2 成功**（workdir 加 rules.md） |
| `load-saved-session` | context_memory | 6 | 找到 sessions/default + compact 文件 |

### 4.2 11 个失败任务（v2）归类

| 失败类别 | 任务数 | 代表任务 | 根因 |
| --- | ---: | --- | --- |
| **任务路径与 workdir 不匹配** | 3 | `edit-python-function` / `add-unit-test` / `refactor-duplicate-code` | 任务 prompt 说"修改 `src/main.py`"，但 workdir 是 xenon 包结构（`xenon/main.py`），LLM 调了 read_file/edit_file 但路径不匹配 expected_tools 列表 |
| **任务前提仍不满足** | 2 | `code-search-tool-node` / `code-search-context-injection` | 任务描述模糊，LLM 调了 search/read 但没找到"tool_node" / "context-injection" 这种字面命名 |
| **REPL 行为单轮 ReAct 不可测** | 4 | `revise-after-test-failure` / `revise-after-review` / `handle-missing-api-key` / `mcp-tool-flow` | multi_turn 类任务**真实业务场景**需要 REPL 命令（`/mcp add` / `/compact`），但 RealAgent 只跑 ReAct 工具循环，没 REPL 命令 |
| **预期外：单轮已"答对"** | 2 | `remember-user-preference` / `compact-long-context` | LLM 文字答对了（"v0.2.2 已发布"/"无长对话可压缩"），但 expected_tools 包含 read_file/write_file，LLM 没调 |

### 4.3 11 个失败里有 4 个是**任务设计问题**（不是引擎问题）

- `revise-after-*` / `handle-missing-api-key` / `mcp-tool-flow` 这 4 个 multi_turn 类任务，**真实业务需要 REPL** 介入
- 当前 `RealAgent` 只跑 ReAct 工具循环，**不挂载** `/mcp add` / `/compact` 等 REPL 命令
- **根因 D（未修）**：RealAgent 与 REPL 未集成，导致 4 个 multi_turn 类任务**永远**不能跑通，无论 max_turns 调到多大

### 4.4 9 个成功的关键特征

- 工具调用**不**多（平均 7.1 次/任务）
- workdir 有真实素材（xenon 自身）
- 任务描述**不**需要 REPL 命令

---

## 五、改进路径（v3 路线）

### 5.1 短期（下一步 v3 路线）

1. **根因 D 修复**：RealAgent 接入 REPL —— 让 multi_turn 类任务能跑通 `/mcp add` / `/compact` 等命令
   - 预计影响：4 任务 v2 失败 → v3 成功
   - v3 预计：9/20 → 13/20 (65%)

### 5.2 中期（不动核心代码）

2. **改进任务 prompt**：把 `src/main.py` 路径改成 `xenon/main.py`，让 file_edit 类任务路径匹配
3. **改进 expected_tools**：把 `multi_turn_revision` 类的 expected_tools 改成 `read_file OR edit_file`（任一即可），允许 LLM 自主选择

### 5.3 长期（评测体系）

1. **SWE-bench 子集** —— 用真实 GitHub issue 测 agent 解决 bug 能力
2. **HumanEval 编程题** —— 测纯代码生成质量
3. **A/B 对比** —— 同一任务在 `direct` / `react` / `plan-execute` 三种引擎下的对比

---

## 六、简历用法（实事求是 v2 版）

> **真实评测**（DeepSeek-V4-Pro，2026-07-29）：
> - 自建 20 任务 6 类别真实 ReAct 闭环评测，**机器断言验证通过率 65%**
>   （25% → 45% → 65%，三轮根因修复累积）
> - **工具执行成功率 95.09%**（112 次调用），结果断言通过率 90%
> - **Cache Rails 厂商上报命中率 97.35%**，节省 93.03%（¥0.2567 vs ¥3.6847 全 miss 基线）
> - **SWE-bench Lite 官方 harness 已接入**：1 instance × 7 引擎矩阵通过 4/7；
>   30-instance 分层抽样已确定待运行
> - 数据见 [`evals/reports/official/2026-07-29-react-real/`](../evals/reports/official/2026-07-29-react-real/report.md)

> **可以讲的工程故事**：发现自己的核心指标测错了对象——system prompt 里的秒级时间戳
> 让 Cache Rails 每轮分叉，此前报告的命中率只是单次 run 内的复用，不是跨轮复用。
> 定位、修复、加回归测试锁定不变量。这比一个漂亮数字更有说服力。

> **不要**写"20 任务全过"、"准确率 90%"，或把 SWE-bench 的 4/7 说成榜单成绩。
> 当前自建套件是 65%，SWE-bench 只跑了 1 个 instance 用于验证 harness 接入。
> WebArena / GAIA 未评测，原因已在 README 标注，不要含糊成"计划中"。

---

## 七、相关报告

- [`evals/reports/real_report.md`](../evals/reports/real_report.md) — Real 模式 v2（本节生成，9/20 成功）
- [`evals/reports/mock_report.md`](../evals/reports/mock_report.md) — Mock 模式（20/20 框架自检）
- [`docs/reports/v0.2.2/REAL_TASK_TEST_REPORT.md`](reports/v0.2.2/REAL_TASK_TEST_REPORT.md) — REPL 端到端 84 用例（v0.2.2 发版证据）
- [`docs/reports/v0.2.2/VERIFICATION_REPORT.md`](reports/v0.2.2/VERIFICATION_REPORT.md) — 独立验证报告（v0.2.2 发版证据）

## 当前版本重跑基线

在确认供应商凭证、预算和隔离目录后运行：

```bash
python evals/runner.py \
  --mode real \
  --model deepseek/deepseek-v4-pro \
  --workdir /tmp/xenon-real-workdir \
  --output evals/reports/real_report_v0.7.3.md
```

评测必须记录提交、模型、API 网关、任务集版本、运行时间、费用、成功率和失败分类。
不要在默认 CI 中启用该命令；真实供应商测试应作为显式、可审计的验收步骤执行。

---

## 八、方案 C 改动清单

| 文件 | 改动 | 根因 |
| --- | --- | --- |
| `evals/runner.py` | `RealAgent` 加 `max_turns=3`，每轮共享 `ContextManager` 累积 history，前一轮 `answer` 注入后一轮 user_input 作为 review feedback | 根因 1：RealAgent 单轮对 multi_turn 类任务不友好 |
| `xenon/engine/react_engine.py` | `no_tool_streak` 重试上限从固定 2 改成 `max(2, max_iterations // 2)`，warning 文本包含 streak + limit 便于诊断 | 根因 3：ReAct 拒绝兜底固定 2 次易被 LLM 硬扛 |
| `/tmp/xenon_real_workdir/` | 隔离 workdir：cp `xenon/` `tests/` `evals/` `docs/` + `.xenon/rules.md`（132 文件，114 py） | 根因 2：workdir 太简单 |

**约束**：
- 930/930 单测全绿（83.07s）—— ReAct 改动零破坏
- 评分函数 `_score` **未动**（用户明确要求不硬编码）
- `expected_tools` 列表**未动**（任务定义本身合理）
- 通用机制改进，**不**针对特定任务加白名单
