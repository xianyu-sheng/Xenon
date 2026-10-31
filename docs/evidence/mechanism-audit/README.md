# 拆除期删除清单复审（机制是否被误删）

对象：#202 拆除过程中删除的 53 个测试文件（`634dcb9..ed056ad`）。
方法：逐个文件按"被测机制是否在 kernel loop 上有等价物"判定，而不是按"引擎是否还在"判定。
日期：2026-10-31

## 判定口径

- **已迁**：机制在 loop 上存在且被测试覆盖 → 删除成立。
- **已补**：机制缺失，本轮回迁 + 补测 → 删除由本轮的补位取代。
- **形态消失**：被测实现是旧协议/旧架构特有的形状，loop 里不存在等价概念 → 删除成立。
- **待补**：机制在 loop 上仍缺，未回迁 → 记录为债务，不允许静默消失。

## 逐条

| 删除的测试 | 被测机制 | 判定 | 依据 |
|---|---|---|---|
| `test_inrun_structured_compaction` | 非 LLM 结构化压缩 | **已补** | `kernel/compaction.py` + `view/base.py` 折叠摘要；`tests/kernel/test_mechanism_parity.py` |
| `test_native_tool_compaction` | 原生 tool-call 协议块切分 | 形态消失 | kernel 只有统一 `Action` 协议，无"原生块"概念 |
| `test_truncation_and_limits` | 截断上限 / max_tokens 解析 | **部分已补** | max_tokens 透传已有 kernel 测试；`finish_reason=length` 的**消费**仍缺（#322 遗留） |
| `test_loop_detector_integration` | 重复调用检测 | **已补** | `kernel/guard.py` `RepeatedCallGuard` + veto 落账 |
| `test_claim_gate` | claim 门控 | 已迁 | claim 解析 + `audit` + `debt` + `abstain` |
| `test_verification_nudge` | 未验证就想收尾的提醒 | 已迁 | 无证据 `Finish` → 审计 → 债务 → `BLOCKED`（机制更强：不再靠提示词） |
| `test_plan_contract` | 计划结构契约 | 已迁 | `PlanContract`/`StepContract` + `_missing_obligations` |
| `test_plan_fallback` / `test_plan_native` / `test_plan_structured_failures` / `test_plan_execute_remediation` / `test_planexecute_replanner` | 计划修复/重规划预算 | 已迁 | `RepairFirst` + `max_repair_attempts` / `max_replans` |
| `test_planexecute_task_state` | 任务状态投影 | 已迁 | ledger 投影（`ledger/projections.py`）取代内存态 |
| `test_e3_event_bus` | 事件总线 → UI | **部分已迁** | `ReplEventSink` 投影；provider 阶段事件缺失 → #320 |
| `test_provider_request_events` | provider 请求阶段事件 | **待补** | #320 |
| `test_e4_reflection` | 反思 | 已迁 | `LoopProfile.reflect_rounds` + `LLMReflector` |
| `test_subagent` / `test_subagent_v1` / `test_e5_spawn_agent` / `test_spawn_agent_timeout` | 子任务 | 已迁 | `harness/child.py`（独立 ledger、无提交、超时当数据） |
| `test_e2_plan_dag` | DAG 计划 | **部分已迁** | 顺序 steps + `depends_on`；并行 DAG 执行未回迁 |
| `test_steering` | 运行中注入用户指令 | **待补** | kernel 无 steering 通道 |
| `test_deadline_degradation` | 超时降级 | **部分已迁** | wall 预算 → `BLOCKED`；"降级执行"未回迁 |
| `test_task_recipe` | 任务配方 | 已迁（模块仍在） | `engine/task_recipe.py` 有生产调用方 |
| `test_chaos_empty_tool_calls` / `test_chaos_malformed_json` / `test_chaos_tool_exception` / `test_chaos_huge_output` | 坏输入健壮性 | **已补** | `tests/kernel/test_robustness.py`；并修出真缺口：gateway 抛异常会打穿 loop |
| `test_r1_error_classification` | 瞬时/终端错误分类 | 已迁 | `repl/transient_errors.py` |
| `test_b4_max_tokens` / `test_b7_model_overrides` | 模型参数解析/透传 | 已迁 | `harness/llm._call_model` 测试 |
| `test_cache_telemetry` / `test_cache_prompt_stability` | 缓存计量 / 前缀稳定 | **部分已迁** | `utils/cache_telemetry.py` 仍在（REPL 用）；prompt 前缀由确定性 system prompt 保证，无独立测试 |
| `test_q2_trace` / `test_trace_linkage` / `test_perf_profile` | 引擎内部日志/计时 | 形态消失 | 实现随引擎删除，无等价概念 |
| `test_engine_*` / `test_remaining_engines` / `test_e1_scout` / `test_e2_mini_react` / `test_q9_combined_engines` / `test_combined_strategy_integration` / `test_strategy_engine_integration` / `test_strategy_guide*` / `test_r2_base_engine` / `test_verify_retry` / `test_interactive_tool_runtime` / `test_plan_panel_render` | 引擎架构本体 | 形态消失 | 主体已删 |
| `test_f5_native_degradation` / `test_deepseek_v4_protocol` | 原生 tool-call 协议兼容 | 形态消失 | 统一 `Action` 协议后无此路径 |

## 结论

- **误删 2 处，已在本轮补齐**：结构化压缩、坏输入健壮性（并修出 gateway 打穿 loop 的真 bug）。
- **待补 4 项，已登记债务**：#320（provider 事件）、#322（截断消费、上下文窗口）、steering、deadline 降级、DAG 并行。
- 其余 40+ 个属"机制已迁移"或"形态消失"，删除成立。

## 复现

```bash
pytest tests/kernel -q          # 194 passed
pytest -q                       # 3534 passed, 96 skipped
python scripts/check_kernel_purity.py
python scripts/check_engine_imports.py
```
