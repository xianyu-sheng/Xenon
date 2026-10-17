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

报告：历史版本归档（v0.7.x 时期报告，已随旧评测产物清理，以本文为准）

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
> - 数据随旧评测产物清理（v0.7.x 时期），本文档保留结论性数字

> **可以讲的工程故事**：发现自己的核心指标测错了对象——system prompt 里的秒级时间戳
> 让 Cache Rails 每轮分叉，此前报告的命中率只是单次 run 内的复用，不是跨轮复用。
> 定位、修复、加回归测试锁定不变量。这比一个漂亮数字更有说服力。

> **不要**写"20 任务全过"、"准确率 90%"，或把 SWE-bench 的 4/7 说成榜单成绩。
> 当前自建套件是 65%，SWE-bench 只跑了 1 个 instance 用于验证 harness 接入。
> WebArena / GAIA 未评测，原因已在 README 标注，不要含糊成"计划中"。

---

## 七、相关报告

- Real 模式 v2（本节生成，9/20 成功；旧报告文件已清理）
- Mock 模式（20/20 框架自检；旧报告文件已清理）

> v0.2.2 时期的旧版测试报告已从文档目录移除，需要时可从 git 历史中找回。

## 当前版本重跑基线

在确认供应商凭证、预算和隔离目录后运行：

```bash
python evals/runner.py \
  --mode real \
  --model deepseek/deepseek-v4-pro \
  --workdir /tmp/xenon-real-workdir \
  --output real_report_v0.7.3.md
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

---

## 九、SWE-bench 30 例分层抽样 — 预注册（待启动）

> 本节在开跑前冻结协议；结果出来后的任何解释都以本节字段为准。
> 标注「启动前填写」的字段须在第一次正式运行前补全并提交，之后不得再改。

### 9.1 冻结字段

| 字段 | 值 |
|---|---|
| 数据集 | `SWE-bench/SWE-bench_Lite`，`split=test` |
| 抽样 | `evals/swebench_select.py --size 30 --seed 20260729`（repo 分层 + 最大余数配额 + 分层内 seeded sample） |
| 选择清单 | `evals/manifests/swebench-lite-30-seed20260729.json`（digest `efc755a153a340094db95b176f0ad88881d65006e9f831f3e955710f7b823630`；30 例列表已随仓库公布） |
| 主引擎 | `react`（唯一用于主结果的引擎） |
| 引擎消融 | `evals/swebench_select.py --calibration-size N` 的 calibration subset，小 N；**不与主结果合并报分** |
| 步数预算 | **120 步冻结**（`STANDARD_STEP_BUDGET`，`tests/test_step_budget.py` 锁定）；运行命令显式带 `--max-steps 120` |
| 其他预算 | `XENON_MAX_TOKENS=8192`、`XENON_VERIFY_RETRIES=2`、gate fuse 阈值 2、provider 重试单次（评测模式） |
| 模型 | `deepseek-v4-flash` via 官方 API（`https://api.deepseek.com/v1`）；主结果用 `--reasoning-effort off`（官方 API 默认思考模式会耗尽 max_tokens 导致截断）；响应中的 system_fingerprint 记入 trace |
| 代码版本 | `70c6d79`；评测源码树 = `git archive 70c6d79` 解包到 Linux 文件系统的独立副本 |
| 运行环境 | Ubuntu 24.04.2 LTS（WSL2 + systemd）；Docker Engine（Ubuntu `docker.io`，overlayfs）容器层存于 `D:\wsl\Ubuntu\ext4.vhdx`；镜像加速 `docker.m.daocloud.io`；Python 3.12；`swebench==4.1.0`；官方 instance image digest 记入 pull 报告 |
| 超时 | `--request-timeout 60`、`--engine-timeout 900`、`--prepare-timeout 1800`（如调整，在此登记） |

### 9.2 判定与排除规则（预注册）

- 判定只认官方 `python -m swebench.harness.run_evaluation` 的 `resolved`；
- `empty_patch` 单独计数，**不得**并入 `resolved=false`，`0/0` 不得报告为成功率；
- **infra 排除**仅限：官方 image 拉取失败、harness 自身崩溃、宿主机资源耗尽——排除必须附原始日志并公开实例 ID；
- 模型侧失败（超时、429、工具拦截、预算触顶）**不排除**，计入 resolved=false 并单列原因；
- 报告两个数：`raw resolved`（分母 30）与 `resolved（剔除 infra 后）`。

### 9.3 报告模板

| instance_id | repo | resolved | empty_patch | steps | tokens(hit/miss/out) | 成本 | wall time | 失败原因 |
|---|---|---|---|---|---|---|---|---|

另附汇总：总成本、p50/p90 步数、缓存命中率、gate 触发次数（含"降级交付但 patch 有效"的比例）。

### 9.4 预算包络（deepseek-v4-flash，单价见 `xenon/utils/deepseek_cache.py`，核对日期 2026-07-21）

假设：输入约 25k tokens/步、缓存命中率约 85%、输出约 800 tokens/步。

| 场景 | 单例 | 30 例 |
|---|---:|---:|
| 典型（中位 ~30–40 步） | ≈ ¥0.23 | ≈ ¥7 |
| 保守（均值 ~60 步） | ≈ ¥0.35 | ≈ ¥11 |
| 硬上限（全部打满 120 步，上下文更大） | ≈ ¥0.9 | ≈ ¥27 |
| 理论极端（bonus 3× 封顶 360 步，deadline 通常先触发） | ≈ ¥2.7 | ≈ ¥80 |

程序级（1 例复验 + 5 例热身 + 主 30 例 + 6×7 引擎消融）建议：**硬上限 ¥100，预期 ¥20–35**。

> 4/7 只是官方 harness 接入验证，不是成绩。本节跑完后，第一个可引用数字是
> 「SWE-bench Lite 30 例子集（seed 20260729）」，且必须与本节字段一起引用。

### 9.5 启动记录（2026-10-15）

- **harness 端到端复验通过**：`astropy__astropy-12907` × `react` × 120 步 ×
  `deepseek-v4-flash`（官方 API，`--reasoning-effort off`）。
  - agent 侧：37s / 14 次迭代 / 13 次工具调用 / 提取到 504 字节补丁（`apply_success=true`）
  - 官方判定：**resolved 1，unresolved 0，empty_patch 0，errors 0**（118s）
  - 产物：`D:\swebench\runs\revalidation-astropy\`（predictions / traces / events / reports）
  - 说明：该实例同时在 30 例清单内，主运行会以同一冻结配置重跑并计入正式结果。
- 运行环境：WSL 3.0.3 + Ubuntu 24.04.2 + Docker Engine（containerd snapshotter，
  `docker.m.daocloud.io`→`docker.1panel.live` 加速）；Docker 数据随发行版 vhdx 落在
  `D:\wsl\Ubuntu\ext4.vhdx`；数据集走 `hf-mirror.com` 并启用 `HF_HUB_OFFLINE=1` 离线加载。
- 主运行与官方判定已排队自动执行（systemd：`swebench-pull` → `swebench-run` →
  `swebench-grade`），原始产物统一保存于 `D:\swebench\runs\main-30-70c6d79\`。

### 9.6 结果（2026-10-16）

**官方判定**（`swebench.harness.run_evaluation`，run_id `xenon-react-main30-70c6d79`）：

| 指标 | 值 |
|---|---|
| 提交实例 | 30 |
| 完成判定 | 28（ 2 个空补丁按官方口径单独计数） |
| **resolved** | **25** |
| unresolved | 3 |
| empty_patch | 2 |
| errors | 0 |
| raw resolved（30 为分母） | 25/30 = 83.3% |
| resolved（剔除空补丁，28 为分母） | 25/28 = 89.3% |

按仓库分层：django 11/11、matplotlib 2/2、sphinx 2/2、pytest 2/2、scikit-learn 2/2、astropy 1/1、psf/requests 1/1、pylint 1/1、**sympy 3/8**（失败集中在 sympy）。

失败分类（全部为模型侧，无 infra 失败）：
- 空补丁 ×2：`sympy-13895`（3 次 edit_file 均因“编辑未产生任何变化”被工具拒绝，最终零改动）；`sympy-23262`（全程未尝试编辑）
- unresolved ×3：`sympy-11897` / `sympy-13146` / `sympy-14817`——补丁应用成功但 FAIL_TO_PASS 未过；其中 11897 还引入了 PASS_TO_PASS 回归（`test_latex_basic`）

过程指标：LLM 调用 1034 次、成功工具调用 899 次；单例 LLM 调用中位数 22（min 9 / max 121，含重试），绝大多数实例远未触及 120 步预算；agent 阶段 1h21m，官方判定 19min。估算 LLM 成本 ≈ ¥4–6（按 v4-flash 本地定价表，输入 ~20k/调用、缓存命中 ~90% 估算；逐调用 token 用量未落入 trace，故为估算值）。

> ⚠️ **可比性声明（重要）**：本次运行中，运行时会先把官方 test patch 应用并提交进 agent 工作树（每个工作树可见 `apply test_patch (eval)` 提交，见 `evals/swebench_runtime.py` 的 v0.8.3 注释），因此 **agent 在解题时可以看到官方隐藏测试**。这与 SWE-bench 官方协议（解题阶段不可见测试，评分阶段才应用）不一致，**该 25/30 不能与 SWE-bench 榜单或其他盲测数字直接比较**，应永远表述为「测试可见（test-visible）变体」。
>
> 已做核查：(a) 全部 30 个补丁均未修改真实测试文件（命中的 `src/_pytest/*` 是 pytest 自身源码，非测试文件）；(b) 抽查 3 个实例（astropy-12907 / django-10914 / sympy-13146）的 trace，未发现读取测试文件的行为；抽查不构成全量证明。(c) 代码注释称「OpenHands/SWE-agent 等主流框架在 agent 运行时都会应用 test_patch」——此说法与官方协议不符，需核实并在未来修正。
>
> 下一步二选一：① 按标准协议（解题阶段不应用 test patch）重跑同 30 例，得到可与榜单同口径的数字；② 保留本次结果，但任何对外表述必须始终标注「测试可见变体」。

**产物**：`D:\swebench\runs\main-30-70c6d79\`（30 行 predictions / traces / events / official-eval.log；逐实例判定报告在 WSL `/root/logs/run_evaluation/xenon-react-main30-70c6d79/`）。

### 9.7 盲测重跑（2026-10-16 启动）

为得到与官方协议（解题阶段测试不可见）及 v0.8.2 盲测基准可直接比较的数字，用同一 30 例清单与同一冻结配置重跑，唯一变化是解题阶段不应用 test patch：

| 字段 | 值 |
|---|---|
| 源码 | `9d43c7d`（新增 `XENON_EVAL_APPLY_TEST_PATCH=0` 开关） |
| 协议 | `XENON_EVAL_APPLY_TEST_PATCH=0`；已核验盲测工作树中无 `apply test_patch (eval)` 提交（主运行同实例有 1 个） |
| 配置 | react / 120 步 / `deepseek-v4-flash` 官方 API / reasoning off |
| 清单 | 同一 manifest（digest `efc755a153a340094db95b176f0ad88881d65006e9f831f3e955710f7b823630`） |
| 产物 | `D:\swebench\runs\blind-30-9d43c7d\` |

盲测结果出来后与 9.6（测试可见）及 v0.8.2 盲测（react 9/30 instance-level）三方并列报告。

### 9.8 三方对照（2026-10-16 全部完成）

同一 30 例清单（digest `efc755a1…`）、同一模型（`deepseek-v4-flash`）、同一引擎（`react`）、同一官方 harness，仅协议与预算不同：

| # | 框架版本 | 协议 | 步数预算 | 补丁产出 | 空补丁 | **resolved** | instance-level |
|---|---|---|---|---|---|---|---|
| 1 | v0.8.2（2026-08-13） | 盲测 | 10 | 19/30 | 11 | **9** | **30.0%** |
| 2 | v0.9.4+（2026-10-16, `9d43c7d`） | 盲测 | 10 | 19/30 | 11 | **9** | **30.0%** |
| 3 | v0.9.4+（2026-10-16, `9d43c7d`） | 盲测 | 120 | 28/30 | 2 | **20** | **66.7%** |
| 4 | v0.9.4+（2026-10-15, `70c6d79`） | 测试可见 | 120 | 28/30 | 2 | **25** | **83.3%** |

**因子分解**：

| 变量 | 对比 | Δ instance-level |
|---|---|---|
| 框架 v0.8.2 → v0.9.4+（固定 10 步 + 盲测） | #1 → #2 | **+0.0pp** |
| 预算 10 → 120（固定盲测） | #2 → #3 | **+36.7pp** |
| 测试可见性（固定 120 步） | #3 → #4 | **+16.7pp** |

**同预算 A/B 的细节（#1 vs #2）**：聚合完全一致——resolved 9、unresolved 10、empty 11、补丁产出 19/30；resolved 集合仅两例互换（旧版解出 `django-16041`、`sympy-14817`，新版解出 `django-10914`、`matplotlib-23562`）。即在 10 步预算下，两个月的框架迭代未带来净提升，瓶颈在预算而非框架。

**未决问题**（已由 9.9 补测）：旧框架在 120 步下的表现未测（若要完整分解框架效应，需补跑 v0.8.2 × 120 步 × 盲测，预估 ~1.5h + ~¥3–5）。

**协议与产物**：
- #2/#3 均为盲测（`XENON_EVAL_APPLY_TEST_PATCH=0`，工作树无 `apply test_patch` 提交，已核验）
- #1 的盲测属自然成立（test patch 应用机制 2026-08-14 才引入）
- 产物：`D:\swebench\runs\{main-30-70c6d79, blind-30-9d43c7d, blind-s10-9d43c7d}\`
- 判定期间一次网络中断（`s10` 首轮判定在 33 分钟后被 `RemoteDisconnected` 中断），已原地重跑成功；原始日志保留在各自 `official-eval.log`

### 9.9 2×2 分解完成：v0.8.2 × 120 步盲测（2026-10-16）

补上 9.8 遗留的最后一格：用 v0.8.2（tag `6de46a8`）在同一 30 例清单、同一模型、同一官方 harness、同一协议（盲测）与同一预算（120 步）下重跑。

| 字段 | 值 |
|---|---|
| 源码 | v0.8.2 tag `6de46a8`（`/root/xenon-v082`，venv editable 指向） |
| 协议 | 盲测（v0.8.2 本就没有 test patch 应用机制；已核验工作树无 `apply test_patch` 提交） |
| 配置 | react / `--max-steps 120` / `deepseek-v4-flash` 官方 API（v0.8.2 无 `--reasoning-effort` 参数，请求不携带该字段） |
| 清单 | 同一 manifest（digest `efc755a1…`） |
| 产物 | `D:\swebench\runs\old120-6de46a8\`；官方报告 `/root/xenon__react__deepseek__deepseek-v4-flash.xenon-react-old120-6de46a8.json` |

**完整 2×2 + 可见性对照（全部为官方 harness 判定）**：

| # | 框架版本 | 协议 | 步数预算 | 补丁产出 | 空补丁 | **resolved** | instance-level |
|---|---|---|---|---|---|---|---|
| 1 | v0.8.2 | 盲测 | 10 | 19/30 | 11 | 9 | 30.0% |
| 2 | v0.9.4+ | 盲测 | 10 | 19/30 | 11 | 9 | 30.0% |
| 3 | v0.8.2 | 盲测 | 120 | 28/30 | 2 | **23** | **76.7%** |
| 4 | v0.9.4+ | 盲测 | 120 | 28/30 | 2 | 20 | 66.7% |
| 5 | v0.9.4+ | 测试可见 | 120 | 28/30 | 2 | 25 | 83.3% |

**因子分解（最终）**：

| 变量 | 对比 | Δ instance-level |
|---|---|---|
| 框架 v0.8.2 → v0.9.4+（固定 10 步盲测） | #1 → #2 | +0.0pp |
| 框架 v0.8.2 → v0.9.4+（固定 120 步盲测） | #3 → #4 | **−10.0pp** |
| 预算 10 → 120（固定 v0.8.2 盲测） | #1 → #3 | **+46.7pp** |
| 预算 10 → 120（固定 v0.9.4+ 盲测） | #2 → #4 | **+36.7pp** |
| 测试可见性（固定 v0.9.4+ 120 步） | #4 → #5 | **+16.7pp** |

**结论（本项目的核心问题的答案）**：在本次测量中，**框架迭代没有显示出独立增益**——10 步预算下两版完全同分（9/30），120 步预算下旧框架反而多解出 3 例（23 vs 20）。提升几乎全部来自步数预算，其次来自解题阶段测试可见（后者不是合法协议，仅作上界参考）。

**配对检验**：#3 与 #4 的 resolved 集合——共同解出 17 例；仅旧版解出 6 例（`django-13265`、`django-14997`、`pytest-7220`、`sphinx-7738`、`sympy-11897`、`sympy-21171`）；仅新版解出 3 例（`django-11742`、`pylint-6506`、`sympy-15346`）。McNemar 精确检验 b=6 / c=3，**p≈0.51，差异不显著**。因此正确表述是「未测到框架增益」，而不是「旧框架更好」。

**旧框架 120 步的协议说明（报告时必须附带）**：
- v0.8.2 无 `--reasoning-effort` 参数（该参数为 v0.9.x 引入），请求不携带 reasoning 字段；新框架三次运行均为 `--reasoning-effort off`。
- 首轮运行（15:02–15:39）因本机网络对 API 的间歇抽风，两个实例（`django-13321`、`django-14608`）撞上 900s 硬墙被杀；重启后复用已完成结果续跑，最终 30/30 完成预测。`django-13321` 两次尝试均为空补丁（另一空补丁为 `sympy-15346`）。
- 因子分解中 v0.8.2 的 10 步基线（#1）原为 heyroute 中转环境（2026-08-13），#3 为官方 API 直连；两个端点上 v0.9.4+ 的 10 步结果完全一致（9/30），提示端点差异对分数影响有限。此历史差异无法完全消除，列出以保持透明。

**判定环境修复（仅可靠性，无协议影响）**：本机到 `raw.githubusercontent.com` 的连接会间歇性「TLS 握手成功但数据黑洞」，而官方 harness 的 `get_requirements_by_commit` 使用无超时的 `requests.get`，判定会无限挂起（本次发生两次，均以 py-spy 定位）。处理：① 给 harness 的 4 处 `requests.get` 加 `timeout=60`（原文件保留 `.bak-before-timeout-patch` 备份）；② 通过 venv `.pth` 注入进程级 shim（`zz_swebench_http_shim`），对 `raw.githubusercontent.com` 走「磁盘缓存 + 有界重试」。两者都不改变取回文件的字节内容，仅把无限挂起变为重试。缓存目录 `/root/swebench-http-cache`。

### 9.10 留出集预注册：样本污染对照（2026-10-16，跑前冻结）

**动机**：9.9 的 2×2 全部使用同一 30 例（seed `20260729`）。审计发现该样本上存在**针对性调优痕迹**——提交历史直接点名其中 9 例（`django-16408`、`matplotlib-24970`、`sphinx-7738` 各 2 次等），且 v0.8.2 的交付补救/验证闭环、v0.8.3 的 plan-execute 迷你轮次都被标注为「SWE-bench 实测」产物。若旧世代的优势部分来自「见过这套题」，则在**与之零重叠**的留出样本上，旧版成绩应下降更多。

**预注册字段（结果产生前冻结）**：

| 字段 | 值 |
|---|---|
| 清单 | `evals/manifests/swebench-lite-30-heldout-seed20261016.json` |
| 抽样命令 | `evals/swebench_select.py --size 30 --seed 20261016 --calibration-size 5 --exclude evals/manifests/swebench-lite-30-seed20260729.json` |
| 抽样池 | 数据集 300 例 − 原 30 例 = **270 例** |
| 与原样本重叠 | **0**（已核验） |
| digest | `e594893cc05337a87617d302c1c35094f005f8375a06775fa79c043005439489` |
| 协议 | 与 9.9 完全一致：react / 120 步 / `deepseek-v4-flash` 官方 API / `--reasoning-effort off` / 盲测（`XENON_EVAL_APPLY_TEST_PATCH=0`）/ 不注入 FAIL_TO_PASS（`XENON_EVAL_FAIL_HINT=0`） |
| 对照 | v0.8.5（tag `7784024`，评测脚手架打同款协议补丁） vs main（`7be984d` 及以后） |
| 重复 | 每条件 **3 次**，逐次报告 resolved、均值与极差 |
| 判定 | 官方 harness，同一判定脚本、同一镜像缓存 |

**判定规则（预先写死）**：

1. 若留出集上「旧 − 新」的 delta 明显小于原样本（或反转）→ 支持污染假说：9.9 的「未测到框架增益」应改述为「原样本上的旧版优势至少部分来自样本内调优」。
2. 若留出集上 delta 与原样本相近 → 污染假说不成立，9.9 结论维持。
3. 两个样本的结果**分开报告**，不合并计算；留出集结果是关于框架增益的主证据。

**工具变更**：`evals/swebench_select.py` 新增 `--exclude`（可重复，支持零重叠留出集）；新增 `tests/test_swebench_select.py`（确定性、分层配额、校准子集、摘要敏感度、排除语义）。

### 9.11 三轮重复：原 30 例（2026-10-16 完成）

同一冻结协议（react / 120 步 / 盲测 / `XENON_EVAL_FAIL_HINT=0` / reasoning off）、同模型、同官方 harness，各重复 3 次：

| 轮次 | v0.8.5（旧） | main（新） |
|---|---|---|
| rep 1 | 19/30 = 63.3% | 18/30 = 60.0% |
| rep 2 | 20/30 = 66.7% | 17/30 = 56.7% |
| rep 3 | 18/30 = 60.0% | 21/30 = 70.0% |
| **均值** | **19.00/30 = 63.3%** | **18.67/30 = 62.2%** |

- 差值（旧 − 新）：**+0.33 例 = +1.1pp**
- 逐实例：旧更好 5 / 新更好 6 / 打平 15 → 符号检验 **p = 1.000**
- 池化配对（90 对）：仅旧 11 / 仅新 10 → McNemar **p = 1.000**
- 极差：旧 2 例（60.0–66.7%），新 4 例（56.7–70.0%）

**结论**：在可能被调优过的原样本上，两版**无统计差异**；前两轮「旧领先 1–3 例」的印象被第 3 轮（新版最高分 70.0%）抹平。这与 9.9 的单次结论一致，且现在有了方差估计。

**协议口径补充**：本节与 9.9 的全部运行均**不向提示词注入 FAIL_TO_PASS 测试名**（`XENON_EVAL_FAIL_HINT` 默认关闭）。此前 9.6–9.8 中 v0.8.3+ 的运行曾默认注入测试名（`891a863` 引入），那些数字标注为「测试名披露变体」，与本节不可直接比较。

### 9.12 留出集结果与污染对照（执行 9.10 的预注册，2026-10-16）

留出集：`swebench-lite-30-heldout-seed20261016.json`（digest `e594893cc05337a87617d302c1c35094f005f8375a06775fa79c043005439489`，与原样本**零重叠**，抽样池 270 例）。结果：

| 轮次 | v0.8.5（旧） | main（新） |
|---|---|---|
| rep 1 | 13/30 = 43.3% | 17/30 = 56.7% |
| rep 2 | 14/30 = 46.7% | 15/30 = 50.0% |
| rep 3 | 15/30 = 50.0% | 15/30 = 50.0% |
| **均值** | **14.00/30 = 46.7%** | **15.67/30 = 52.2%** |

- 差值（旧 − 新）：**−1.67 例 = −5.6pp**（符号与原样本相反）
- 逐实例：旧更好 2 / 新更好 8 / 打平 14 → 符号检验 **p = 0.109**
- 池化配对：仅旧 10 / 仅新 15 → McNemar **p = 0.424**（n = 30，未达显著）

**污染对照（核心）**：

| 条件 | 原样本 | 留出集 | 降幅 |
|---|---|---|---|
| 旧 v0.8.5 | 63.3% | 46.7% | **−16.7pp** |
| 新 main | 62.2% | 52.2% | **−10.0pp** |

**按 9.10 预注册规则判定**：留出集上「旧 − 新」由 **+1.1pp 反转为 −5.6pp**，且旧框架的降幅明显大于新框架 → **污染假说成立**：原样本上的旧版优势至少部分来自样本内调优（提交历史点名其中 9/30 实例，且 v0.8.x 的交付补救/验证闭环明确针对 SWE-bench 失败模式）。

**修正后的结论**：

1. 「框架迭代没有可测量的提升」仅在**原样本**上成立；在**零重叠留出集**上，新框架领先 5.6pp（方向与污染假说一致，但 n = 30 未达统计显著）。
2. 两版在留出集上都下降，说明留出集整体难度更高，不能跨集合比较绝对值；可比的是**差值的符号反转**与**降幅差异**。
3. 若要达到统计显著，下一步应扩大留出集（如 60–100 例）或增加重复次数；本节的表述应保持为「未达显著的方向性证据」。

**产物与归档**：`D:\swebench\archive\set-r\`（原样本 6 组）与 `D:\swebench\archive\set-h\`（留出集 6 组），每组含 predictions、官方报告、判定/运行日志、traces、SUMMARY.md/json（resolved 名单与比率）以及 INDEX.json（3 次均值/极差 + 每个文件的 sha256）。

**判定环境经验（可复现性）**：官方 harness 的 `clean_images()` 在 `--cache_level env` 下会删除「判定运行期间新出现」的 `sweb.eval*` 镜像；批量拉取必须在两批评定之间。runtime 用 SDK `get_archive()` 流式提取 `/testbed`（含 GB 级 `.git`）在大仓库上会断流超时——改用 `docker cp` 手动填充缓存（布局 `_official_source/<实例>/<镜像ID前16位>/testbed`，30 例 3 分钟）后恢复。