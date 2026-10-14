# Xenon 观测层（AgentOps）设计 — P0

> **状态：已评审通过（P0 完成）** · 关联 issue：[\#131](https://github.com/xianyu-sheng/Xenon/issues/131) · Schema：`xenon.trace/v1`
>
> 本文件是 P1–P4 的实现依据。第 14 节记录维护者裁决（2026-10-14）。

---

## 1. 背景与目标

### 1.1 已有底盘（不重建）

| 已有能力 | 位置 | 性质 |
|---|---|---|
| 哈希链事件流（append-only，类型化） | `xenon/session/events.py` | 防篡改审计，唯一事实来源 |
| `/evidence` 自证 | `xenon/session/projection.py`、`xenon/repl/command_groups/evidence.py` | 审计投影 |
| run_id / call_id 链路 ID | `xenon/engine/trace.py` | 仅日志关联 |
| EventBus 多订阅 | `xenon/engine/event_bus.py` | 扩展点 |
| LLM usage 采集与回调 | `xenon/utils/llm_usage.py`（`LLMUsage`、`register_usage_callback`） | 有 token 明细，未入事件链 |
| Token/成本统计 | `/cost`、`/debug-tokens`、`xenon/utils/deepseek_cache.py`（含缓存计价） | 会话内、聊天口径 |
| 三阶段预算 | `xenon/engine/budget.py` | 限流，非观测 |
| 评测 harness | `evals/` | 手动（离线回放见 #126） |

### 1.2 缺口

1. **无 span 模型**：run → turn → tool/LLM 调用的层级、耗时、状态无结构化记录
2. **无指标聚合**：按工具/模型的延迟分位、token/成本归属、重试/gate 统计
3. **无导出**：不能对接 OpenTelemetry / Jaeger / Grafana / Langfuse
4. **无确定性回放**：复现失败必须重新付费执行
5. **无时间线视图**：只能翻日志

### 1.3 目标 / 非目标

**目标**：以「事件链 → 纯投影」的方式，提供 `trace`（span 导出）、`stats`（指标）、`view`（离线查看器）、`replay`（确定性回放）四项能力。

**非目标**（硬性）：

- ❌ 不做 SaaS / 账号体系 / 默认上报；不引入默认联网
- ❌ 不做服务端仪表盘；查看器为离线自包含 HTML
- ❌ 不改变事件链哈希算法、链语义与已有字段含义
- ❌ 不新增阻断类校验（全仓铁律：只有空洞回答一个阻断门）
- ❌ 不把观测耦合进引擎主循环（投影必须旁路、可失败、不影响回合）
- ❌ 不重复 evidence 的职责（见第 2 节）

---

## 2. 职责边界：审计 ≠ 观测

| 维度 | evidence（审计） | observability（本设计） |
|---|---|---|
| 回答的问题 | 过程是否**未被篡改** | 过程里**发生了什么**、慢在哪、贵在哪 |
| 数据 | 事件链本体（哈希 + 前驱） | 只读事件链派生的 span / 指标 |
| 完整性 | 哈希链自证 | 引用链头哈希，不自行保证 |
| 参与者 | 用户与审计方 | 开发者、调试者、运维 |
| 变更权限 | **本设计不得触碰** | 只新增投影模块 |

同一份事实来源，两种投影；两者可互相引用（导出文件头带链状态），但互不依赖。

---

## 3. 事实来源

### 3.1 事件记录格式（现状）

`~/.xenon/sessions/events/<session_id>.jsonl`，每行：

```json
{"id": "a1b2c3d4e5f6", "type": "turn/user", "at": 1760000000.123, "h": "sha256…", "prev": "genesis|sha256…", "…payload"}
```

- `at`：写入时刻（秒，浮点）
- `h` / `prev`：内容哈希 / 前驱哈希（`canonical_event_json` 规范化后 sha256）
- 写失败静默降级（best-effort），读侧跳过坏行

### 3.2 现有事件与字段（读代码核实）

| 事件 | 现有字段 | 观测用途 | 缺口 |
|---|---|---|---|
| `turn/user` | `turn_id`, `text[:500]`, `intent`, `level`, `operations`, `continuation`, `deferred` | 回合起点、意图/级别归因 | 无回合结束标记（时长需推导） |
| `turn/assistant` | `preview[:300]` | 回合终点候选 | 缺 `turn_id`、`outcome`、耗时 |
| `tool/result` | `tool`, `success`, `paths[:20]`, `error[:120]` | 工具成功率 | 缺 `call_id`、起止时间/耗时 |
| `gate/opened` | `gate_id`, `kind`, `level`, `operations`, `reason` | 审批等待起点 | — |
| `gate/resolved` | `gate_id`, `outcome`（`consumed`/`stale`/…） | 审批结果、等待时长 | 已可配对 |
| `goal/*`、`plan_mode/set` | 见 `xenon/session/events.py` | 目标/计划里程碑 | — |
| LLM 调用 | **未入链**；回调 `on_provider_request_start/end` + `LLMUsage` 有数据 | 延迟/token/模型归因 | 需新增事件（见 3.3） |

### 3.3 提案：向后兼容的补充（**需评审**，见开放问题 1–3）

**A. 既有事件新增可选字段**（不改已有字段语义，老记录读侧容忍缺失）：

- `turn/assistant` += `turn_id`, `outcome`（`pass|remind|fail|fuse`）, `duration_ms`
- `tool/result` += `call_id`, `started_at`, `ended_at`, `duration_ms`

（`gate/opened` 已带 `gate_id`，无需补充。）

> 说明：新增字段会进入该条记录的哈希计算（写入时），**不改变哈希算法、链连续性判定与既有字段含义**；旧记录仍可验证。

**B. 新事件类型 `llm/call`**（一次 LLM 调用完成后写一条，单事件即 span）：

```json
{
  "type": "llm/call",
  "call_id": "…", "run_id": "…", "phase": "llm:deepseek/deepseek-chat",
  "provider": "deepseek", "model": "deepseek-chat",
  "started_at": 1760000000.1, "ended_at": 1760000003.9, "duration_ms": 3800,
  "attempt": 1, "success": true,
  "usage": {"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500,
            "cache_hit_tokens": 800, "cache_miss_tokens": 400}
}
```

写入点：`xenon/utils/llm_usage.py` 的使用回调/`on_provider_request_end` 路径（具体实现位置在 P1 定，避免粘贴式改动引擎核心）。
`attempt`：provider 重试序号（归因重试风暴）。

---

## 4. Span 模型

### 4.1 层级

```
trace (session)
├─ turn span          name="turn"            kind=turn
│  ├─ llm span        name="chat <model>"    kind=llm
│  ├─ tool span       name="tool <tool>"     kind=tool
│  └─ gate span       name="gate <kind>"     kind=gate
└─ (orphan spans：无法归属的 llm/tool，挂 trace 下，attributes.orphan=true)
```

### 4.2 字段定义

| 字段 | 类型 | 说明 |
|---|---|---|
| `trace_id` | string | 会话 ID（事件文件名） |
| `span_id` | string | 事件 `id`；推导 span 用 `"derived:"+序号` |
| `parent_id` | string\|null | 归属回合/父 span |
| `name` / `kind` | string | 见上表 |
| `start_time` / `end_time` | float\|null | unix 秒 |
| `duration_ms` | int\|null | null = 不可知（点事件） |
| `status` | `ok`\|`error`\|`unknown` | 工具失败=error；缺配对=unknown |
| `provenance` | `recorded`\|`derived` | 直接来自事件 / 规则推导 |
| `attributes` | object | 按 kind 白名单（见 4.3） |

### 4.3 事件 → span 映射

| span | 来源 | 时间 | attributes（白名单） |
|---|---|---|---|
| turn | `turn/user` → 下一条 `turn/user` 或 `turn/assistant` | 推导（`derived`）；有 `duration_ms` 时用实测 | `intent`, `level`, `operations`, `continuation`, `deferred`, `outcome` |
| llm | `llm/call` | 实测 | `provider`, `model`, `phase`, `attempt`, `success`, `usage.*` |
| tool | `tool/result` | 有起止用实测；否则点事件（`derived`, duration=null） | `tool`, `success`, `paths`（截断 20） |
| gate | `gate/opened` → `gate/resolved`（按 `gate_id`） | 实测（等待时长） | `kind`, `level`, `operations`, `outcome` |
| goal/plan | `goal/*`、`plan_mode/set` | 点事件 | `state`, `reason` |

**归属规则**：优先 `turn_id`；缺失时归属"最近的前一条 `turn/user`"（顺序假设写入文档与测试）。

**降级策略**（老记录 / 坏数据）：

- 缺字段 → 对应字段 `null`，`provenance="derived"`，不报错
- `gate/opened` 无配对 → `status=unknown`、`duration_ms=null`
- 链损坏 → 导出继续，文件头标 `chain.broken` 并在 stderr 警告

---

## 5. Schema：`xenon.trace/v1`

### 5.1 导出格式（JSONL）

第一行 header，其后每行一个 span（按 `start_time` 排序，同值按 `span_id` 稳定排序）：

```json
{"schema":"xenon.trace/v1","session_id":"20261013-215800-1234-ab12cd","exported_at":1760000100.0,
 "source":{"kind":"events-jsonl","chain":{"status":"ok","verified":120,"legacy":0,"head":"sha256…","broken_at":null}},
 "options":{"level":"standard","redacted":true}}
{"trace_id":"…","span_id":"…","parent_id":null,"name":"turn","kind":"turn","start_time":…,"end_time":…,"duration_ms":12000,"status":"ok","provenance":"derived","attributes":{…}}
```

OTLP 导出为一次性 POST（见第 6 节），不产生 JSONL。

### 5.2 兼容性策略

- **加法兼容**：v1 内只新增可选字段；删除/改名/改语义 = 升 `v2`
- 消费者 **必须忽略未知字段**（文档与测试同时约束）
- 每个导出文件都带 `schema`；解析器遇到未知 major → 明确报错，不做猜测
- 快照测试冻结 v1 输出（`exported_at` 等时间字段在快照中归一化）

---

## 6. OTLP 映射（OpenTelemetry GenAI 语义约定）

- 传输：**OTLP/HTTP + JSON**（`httpx` 已是运行依赖，不新增依赖；gRPC/protobuf 不在 P1 范围）
- 资源属性：`service.name="xenon"`, `service.version=<xenon.__version__>`
- 仅 `xenon trace export --format otlp --endpoint <URL>` 显式触发；**无后台/默认上报**

| Xenon | OTel |
|---|---|
| `kind=llm` | `SpanKind.CLIENT`，name `chat <model>` |
| `provider` | `gen_ai.system` |
| `model` | `gen_ai.request.model` |
| `usage.prompt_tokens` | `gen_ai.usage.input_tokens` |
| `usage.completion_tokens` | `gen_ai.usage.output_tokens` |
| `tool` | `gen_ai.tool.name` |
| `kind=turn/gate` | `SpanKind.INTERNAL` |
| `start_time/end_time` | `startTimeUnixNano/endTimeUnixNano` |
| `trace_id/span_id` | OTel id（十六进制化，见 P1 实现注记） |

P1 实现前需核对当时的 GenAI 语义约定版本，映射表以文档形式固定在该模块内。

---

## 7. 指标（P2）

| 指标 | 定义 | 来源 |
|---|---|---|
| `turn_duration_ms` | p50/p95，按窗口 | turn spans |
| `llm_call_duration_ms` | p50/p95，按 provider/model | llm spans |
| `tool_call_duration_ms` | p50/p95，按工具 | tool spans（有耗时的） |
| `tokens_in/out/total` | 按 model、按 turn | llm spans |
| `cache_hit_ratio` | hit/(hit+miss) | llm spans |
| `cost` | tokens × 价目表 | `~/.xenon/pricing.yaml`（内置默认 + 用户覆盖） |
| `tool_success_rate` | success/total，按工具 | tool spans |
| `tool_failure_top` | 失败工具排行（计数） | tool spans |
| `gate_wait_ms` | resolved − opened（p50/p95） | gate spans |
| `gate_outcomes` | consumed/stale/… 计数 | gate spans |
| `retries` | attempt>1 的调用数 | llm spans |
| `orphan_rate` | 无法归属 span 占比（数据质量指标） | 全局 |

- 未知模型 → `cost=null`，输出 `cost_unknown=true`；不猜价格
- 窗口：`--session <id>`（默认最近会话）或 `--since 7d|30d|all`
- `--format prometheus`：文本格式导出（映射：计数=计数器、时长=直方图或摘要，实现时冻结命名 `xenon_*`）

---

## 8. 隐私红线与脱敏级别

**原则：字段级白名单（allow-list），不是内容审核。**

| 级别 | 内容 | 用途 |
|---|---|---|
| `minimal` | ids、时间、类型、成功/失败、工具名、token 计数 | 最保守共享 |
| `standard`（默认） | + 路径（≤20，与 `/evidence` 同口径）、错误摘要 ≤120 字符、gate outcome | 本地统计/导出 |
| `full` | + 事件链中**已存在**的文本预览（`text[:500]`/`preview[:300]`） | 仅本地 JSONL，显式 opt-in |

- **OTLP 导出硬上限 `standard`**；指定 `full` 时降级并打印警告
- 一律剔除敏感键（复用 `mask_sensitive_params` 的名单：`api_key/token/secret/password/…`）；导出物出现凭据视为 P0 级 bug，带测试
- 不新增默认联网；端点为显式参数/配置
- 文档如实声明：脱敏是字段白名单，不检查语义内容——**不要导出你不想暴露的会话**

---

## 9. 性能预算

| 项 | 预算 | 验证 |
|---|---|---|
| 事件追加（新增 `llm/call`，每条一次） | p95 ≤ 0.5ms | 基准测试（1000 次追加计时，阈值放宽防 CI 抖动） |
| 单回合总观测开销 | ≤ 1%（对总时长） | 基准对比测试（跑固定 fixture 回合） |
| 导出 10k 事件会话 | < 2s（普通笔记本） | 生成 10k 合成事件的计时测试 |
| 回放 | 纯文件读取，无网络 | `--check` 确定性测试 |

写路径必须 **best-effort**：观测写入失败不得影响回合（沿用事件链既有语义）。

---

## 10. CLI 界面

```
xenon trace export [SESSION] [--format jsonl|otlp] [--out FILE]
                   [--level minimal|standard|full] [--endpoint URL]
xenon stats        [--session ID] [--since 7d|30d|all] [--tool NAME] [--model NAME]
                   [--format text|prometheus]
xenon trace view   [SESSION] [--out FILE.html]          # P3
xenon replay       [SESSION] [--from-turn N] [--check]  # P4
```

- `SESSION` 缺省 = 最近会话
- 退出码：`0` 成功；`2` 参数错误/会话不存在；`3` 链损坏（导出仍产出，头部标记 + stderr 警告）
- P1–P4 均不联网（OTLP 除外，且仅显式触发）

---

## 11. 回放语义（P4）

- **录制** = 事件链 + `llm/call`（均在本地）
- **回放** = 按原顺序重建 span 树与时间线；`--check` 校验每步与录制一致
- ❌ 不重新执行工具、不调用网络；"重新执行一遍"属于另一个功能（rerun），**非目标**
- 确定性验收：同一记录两次 `replay --check` 输出逐字节一致

与 #126（评测离线回放）共用 driver 与录制格式定义，两边只维护一份。

---

## 12. 测试计划

| # | 测试 | 级别 |
|---|---|---|
| 1 | schema 快照：fixture 会话 → v1 导出 → 冻结对比 | P1 |
| 2 | 兼容：无新字段的 legacy 记录可导出（null/derived，不报错） | P1 |
| 3 | 链损坏：构造断链 → 导出仍工作且 header 标记 `broken` | P1 |
| 4 | 脱敏：构造含凭据/prompt 的会话 → minimal/standard 导出无敏感串；OTLP 对 full 降级 | P1 |
| 5 | OTLP：mock HTTP 端点断言 GenAI 字段映射 | P1 |
| 6 | 指标对账：抽样会话的 stats 与事件链人工核算一致 | P2 |
| 7 | 性能：追加 p95、10k 导出计时（阈值放宽） | P1/P2 |
| 8 | 回放确定性：两次 `--check` 输出一致 | P4 |
| 9 | 全量回归：无新增阻断门、回合行为零变化 | 每阶段 |

---

## 13. 里程碑与认领

| 阶段 | 状态 | 认领方式 |
|---|---|---|
| P0 设计（本文档） | **已完成**（裁决见第 14 节） | — |
| P1 导出（JSONL + OTLP） | 可认领 | 评论 #131 说明认领 |
| P2 指标（`xenon stats`） | 可认领 | 同上 |
| P3 查看器（离线 HTML） | 待 P1 合并 | 同上 |
| P4 回放（与 #126 共用 driver） | 待 P1/P2 合并 | 同上 |

每个阶段独立 PR；不合并阶段。实现 PR 必须引用本文档对应章节。

---

## 14. 裁决记录（维护者，2026-10-14）

| # | 问题 | 裁决 |
|---|---|---|
| 1 | `tool/result` 补字段 vs 新事件 | ✅ 补可选字段（`call_id/started_at/ended_at/duration_ms`），不新发事件 |
| 2 | `turn/assistant` 补充字段 | ✅ 补 `turn_id/outcome/duration_ms` |
| 3 | OTLP 传输 | ✅ JSON over HTTP（零新依赖；protobuf 不在 P1 范围） |
| 4 | 价目表 | ✅ 内置默认 + `~/.xenon/pricing.yaml` 用户覆盖；条目带 `currency`（默认 CNY）；未知模型 `cost=null` + `cost_unknown=true` |
| 5 | 归档/保留 | ✅ 本设计不涉及，后续独立 issue |
| 6 | `xenon stats` 默认窗口 | ✅ 最近会话；`--since` 扩展窗口 |
| 7 | P3 查看器技术 | ✅ 自包含 HTML + 内联 JS |
| 8 | 命名 | ✅ CLI 子命令为主（`xenon trace/stats/replay`）；REPL 斜杠命令为后续便利项 |

---

*本文件已生效。实现中发现新问题请在此追加“后续裁决”小节，不另开平行文档。*
