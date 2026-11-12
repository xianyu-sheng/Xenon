# 旧引擎遗留件去留决策表（缺陷 1/7）

**状态**：阶段 1–4 已执行（2026-11-10/11），表内"决策"列对已执行项以实际结果为准。
**日期**：2026-11-10，更新 2026-11-11。

> 本表基于实际导入审计（`scripts/legacy_import_audit.py` 可复现），
> 而非文档猜测。每个决策都标注了**前置条件**——删除从不是单文件行为，
> 而是"耦合单元退役"的结果。

## 0. 判断原则

1. **内核路径依赖的一律保留。** 内核网关（`catalog/gateway.py`）内部包着
   legacy `ToolExecutor`，所以 `nodes/tool_executor.py` 直接依赖的引擎文件
   是**活跃基础设施**，不是遗留件。
2. **按耦合单元成组退役。** `task_state` / `task_verifier` /
   `hollow_detector` / `turn/gate.py` / `repl/turn_flow.py` 彼此咬合，
   单删一个只会留下半截链路。
3. **删除的门槛**：零生产调用方（仅测试也算"无"）+ 该路径的替代者已通过
   真实任务验证 + 全量测试绿。
4. **名字是历史，语义要对**：`engine/` 目录名冻结（改目录名会让 217 个
   历史账本路径与文档失效），内部文件按语义迁移或退役。

## 1. 双路径现状

```text
REPL 输入 ──should_route_to_kernel──┬── True ──▶ RuntimeAdapter ──▶ LoopRuntime
                                    │            （ledger/ 哈希链，内核唯一 writer）
                                    └── False ─▶ turn_flow（direct chat）
                                                 ├─ TaskState（legacy 续接）
                                                 └─ session/tree + session/events（L0 旧哈希链）
```
> 2026-11-11：`TurnGate` / `TaskVerifier` / `HollowDetector` 已随发布闸门一并删除。
> 上图只保留真正仍在运行的组件。

两个入口共用 `ToolExecutor`（`nodes/`），所以工具层不是"遗留件"，
而是两代路径的地基。

## 2. `xenon/engine/` 逐文件决策

| 文件 | 行数 | 内核路径使用 | 关键使用者 | 决策 | 前置条件 |
|---|---|---|---|---|---|
| `context.py` | 372 | ✅ 网关/工具层 | gateway、nodes/、repl/ | **保留**（AgentContext 是共享状态底座） | — |
| `callbacks.py` | 589 | ✅ | xenon/__init__、tool_executor、kernel_host | **保留**（UI 回调基础设施） | — |
| `tool_executor.py` 之外的 `tool_tracker.py` | 205 | ✅ 网关 | gateway | **保留**（执行观测） | — |
| `tool_runtime.py` | 46 | ✅ 网关 | gateway | **保留**（工作区注入） | — |
| `tool_profiles.py` | 109 | ✅ | tool_executor | **保留**（工具配置） | — |
| `tool_gate.py` | 244 | ✅ | tool_executor、param_validation | **保留**（黑名单/参数级别=管理配置） | 后续可考虑前移到 catalog |
| `circuit_breaker.py` | 111 | ✅ | tool_executor | **保留**（执行器重试保护） | — |
| `checkpoint_manager.py` | 263 | ✅ | utils/llm_client | **保留**（传输检查点） | — |
| `scheduler.py` | 156 | ✅ | main.py、command_groups/agent | **保留**（子代理调度） | — |
| `task_recipe.py` | 192 | ✅ | tool_executor、turn_flow | **保留**（recipe 校验） | — |
| `trace.py` | 92 | ❌ | 过滤器永远拿到空 run id | **已删除 · 2026-11-11**（账本即 Trace，不建第二套运行日志） | ✅ 已完成 |
| `evidence_contracts.py` | 155 | ✅ | tool_executor、tool_node、tool_families | **已迁移 · 2026-11-10** → `nodes/evidence_contracts.py`（纯移动，8 处导入改写） | ✅ 已完成 |
| `evidence_gate.py` | 421 | ⚠️ 部分 | tool_executor（仅 `GateVerdict` 类型）、~~turn/gate.py~~ | **已收缩 · 2026-11-11**：只保留 `GateVerdict`（30 行），原地留在 `engine/`；其余随 TurnGate 删除 | ✅ 已完成 |
| `evidence_runtime.py` | 737 | ❌ 网关显式 `record_evidence=False` 关闭 | ~~tool_executor（关闭态）、`/evidence` 命令 legacy 分支~~ | **已删除 · 2026-11-10**（L0 旧哈希链；`/evidence` 实为读 session/events，不受影响） | ✅ 已完成 |
| `task_state.py` | 1220 | ❌（内核 `_clear_legacy_task_state` 显式清空） | 仅 repl/turn_flow.py | **保留**（实际：`turn_flow` 仍用它做 legacy 续接） | turn_flow 退役 |
| `task_verifier.py` | 88 | ❌ | repl.py、turn/gate.py | **删除** | TurnGate 退役 |
| `hollow_detector.py` | 287 | ❌ | 仅 turn/gate.py | **删除**（L1 空洞检测；内核以证据/债务替代） | TurnGate 退役 |
| `budget.py` | 221 | ❌ | repl/context_strategies.py（旧压缩） | **删除** | 旧压缩链退役（内核双摘要已上线，见缺陷 4） |
| `structured_compactor.py` | 103 | ❌ | repl/context_manager.py | **删除** | 同上 |
| `result.py` | 127 | ❌ | repl/turn_helpers.py | **删除** | turn_flow 退役 |
| `__init__.py` | 0 | — | — | 保留占位 | — |

小计：保留 12、迁移 1、拆分 1、退役/删除 7。

## 3. `engine/` 之外的耦合单元

| 单元 | 使用者 | 决策 | 前置条件 |
|---|---|---|---|
| `turn/gate.py`（192 行） | ~~repl.py 发布门、turn_flow~~ 零调用方 | **已删除 · 2026-11-11**：整条发布闸门不可达（`_render_engine_result` → `_verify_turn` → `TurnGate.evaluate` 只互相调用） | ✅ 已完成 |
| `repl/turn_flow.py` | repl.py、skill 命令组 | 同上（legacy 回合编排） | 同上 |
| `repl/context_manager.py` + `context_strategies.py` | REPL 上下文（含旧 LLM 压缩） | **保留**：direct chat 仍用其历史管理；内核压缩已独立（双摘要） | direct chat 迁入内核上下文后 |
| `session/tree.py`、`session/events.py`（L0 旧链） | TurnGate 路径、`/resume`、`/evidence` legacy | **保留**：`/resume` 等会话功能仍依赖 | 会话功能迁到 `ledger/` 后；届时用 `xenon import-legacy` 一次性导入（**不做启动扫描**——源仍在写入，扫描等于复制活数据，真实环境实测 25k 文件 ≈ 165 秒/次） |

## 4. 分阶段执行顺序（风险从低到高）

- **阶段 1（低风险）✅ 已完成 · 2026-11-10**：`budget.py`、`structured_compactor.py` 已删除（-324 行）。
  实施与审计一致：`budget.py` 的 `BudgetManager` 类零运行时使用者，`BudgetPhase`
  枚举内联进 `context_strategies.py`；`structured_compactor.py` 的
  `extract_result_id` / `summarize_observation_text` 迁入 `context_manager.py`
  模块级助手（逻辑逐行保留）。`tests/test_f2_budget.py` 随模块删除。全量
  4120 passed（-24 为随删的专属用例）。已知保留原样的一点：`_FAILURE_RE`
  会把 "0 failed" 标为 FAIL——原实现即如此，本轮不改变语义。
- **阶段 2（低风险）✅ 已完成 · 2026-11-10**：`evidence_runtime.py`（737 行）已删除。
  审计中发现的真实依赖比预估更广也更浅——`/evidence` 命令其实早已不读它
  （读的是 `session/events.py` 与 kernel `ledger/`）；真实写入方只有
  `ToolExecutor._ledger_event`（网关已用 `record_evidence=False` 关闭）、
  MCP 注册表的 `context.evidence` 钩子、以及会话保存的快照。全部随之移除：
  `AgentContext` 的证据门面（`evidence`/`get_evidence_runtime`/`bind_evidence`）、
  executor 的 `record_evidence`/`evidence_ledger` 参数与 `_ledger_event`/
  `_resolve_ledger`、MCP 三处钩子、`/save`、自动保存与恢复的证据快照/回填。
  保留：FactBindingGate 的盲编辑拦截（含自动补读）——它基于 tracker 而非账本。
  `.evidence` 门面读取一律改为不存在；旧存档中的 `evidence` 键被安全忽略。
  测试：删除 4 个子系统测试文件、重写 `test_online_evidence_gate.py`（保留
  gate 行为）、清掉 MCP 两处旧钩子断言；`docs/EVENT_SOURCE.md` 标注为历史文档。
  全量 4088 passed；真实 API 任务 finalized/verified。
- **阶段 3（中风险，耦合单元）✅ 已完成（主体）· 2026-11-11**：审计确认整个发布闸门
  不可达（`repl._render_engine_result` 零调用方 → `_verify_turn` → `TurnGate.evaluate`
  只有彼此调用），因此 `turn/gate.py`、`engine/task_verifier.py`、
  `engine/hollow_detector.py`、`turn_helpers.turn_status_from_result`、`_render_engine_result`
  与 `_verify_turn` 已删除，`evidence_gate.py` 只保留仍被 `tool_executor` 使用的
  `GateVerdict`。`task_state.py` **保留**：`turn_flow` 仍用它做 legacy 续接
  （direct chat 尚未全部走内核），它不在本次删除范围内。
- **阶段 4（迁移）✅ 已完成 · 2026-11-10**：`evidence_contracts.py` 已从
  `engine/` 迁入 `nodes/`（`git mv` + 8 处导入路径统一改写），无逻辑改动；
  模块本身零 engine 依赖，迁移后层次更正确（工具结果解析契约属于 nodes）。
  全量 4088 passed；真实 API 任务 finalized/verified。

## 5. 明确不在本表内的

- `nodes/tool_executor.py` 本身（7 阶段流水线）：内核网关的**执行底座**，
  两代路径共用。其内部对 legacy 级别检查的抑制已在内核裁决下完成（缺陷 2）。
- `catalog/gateway.py`：内核唯一副作用入口，保留。
- 文档、测试、账本路径中的 `engine/` 字样：历史事实，不改写。
