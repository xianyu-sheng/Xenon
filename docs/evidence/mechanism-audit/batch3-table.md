# Batch 3 判定表：仍引用引擎骨架的 39 个测试文件（396 个用例）

判定依据不是"文件里有没有 `engine`"，而是**断言对象今天还在不在**：

- **A｜主体已删**：断言的就是引擎/引擎提示词/引擎注册表的内部行为 → 删。
- **B｜夹具可换**：断言的是仍保留的器官（工具执行、turn gate、上下文管理、证据门）→ 迁移夹具，保留覆盖。
- **C｜静态守卫**：扫源码的架构不变量 → 去掉引擎依赖后保留。

| 文件 | 用例 | 断言对象 | 判定 | 说明 |
|---|---:|---|---|---|
| test_execution_policy | 36 | 分级执行策略（organ 仍在，被 `nodes/tool_executor` 使用） | **B** | 只把 `ReActEngine` 夹具换成策略直测 |
| test_input_boundary_validation | 26 | 输入校验（引擎族的 `run` 断言已删） | **B** | 其余保留 |
| test_evidence_gate | 24 | `EvidenceCaptureGate` / `FileClaimGate`（organ 在，`nodes/tool_executor`、`turn/gate` 使用） | **B** | 去除引擎夹具 |
| test_document_tools | 21 | `BUILTIN_TOOLS` 文档工具行为（organ） | **B** | 只需 `AgentContext` 直测 |
| test_tool_schema_registry | 21 | 工具 schema 注册表（organ） | **B** | 同上 |
| test_f2_mercy_synthesis | 19 | 引擎的"宽恕合成"决策 | **A** | 该决策只存在于引擎循环内 |
| test_permission_integration | 17 | 权限分级（kernel `PermissionPolicy` 已接） | **B** | 迁到策略直测 |
| test_llms_txt | 13 | `BUILTIN_TOOLS` 文本工具（organ） | **B** | 无需引擎 |
| test_tool_execution_lifecycle | 13 | 工具执行生命周期（organ） | **B** | 去掉 `plan_dag/plan_execute` 依赖 |
| test_online_evidence_gate | 12 | 在线证据门（organ） | **B** | 去引擎夹具 |
| test_phase2c_contract_consumption | 12 | 引擎从 `TurnContract` 读取等级 | **A** | kernel 走 `PermissionPolicy`，该读取路径已不存在 |
| test_task_state_projection | 12 | `EventBus` 驱动投影 | **A** | `event_bus` 已删；投影改由 ledger 折叠 |
| test_engine_error_recording | 11 | 引擎错误落账 | **A** | 引擎已删 |
| test_checkpoint_budget | 10 | 引擎 checkpoint 步数 | **A** | 引擎已删 |
| test_run_script_tool | 10 | 脚本工具（organ） | **B** | 去引擎夹具 |
| test_context_continuity | 9 | 上下文连续性（REPL 上下文管理器 organ） | **B** | 夹具换 `ctx_mgr` 直测 |
| test_execution_policy_golden | 9 | `strategy_guide` 的输出文本 | **A** | 提示词骨架已删 |
| test_f4_ctx_mgr_injection | 8 | 引擎注入 ctx_mgr | **A** | 注入点随引擎消失 |
| test_tool_profiles | 8 | 工具档位（organ） | **B** | 去引擎夹具 |
| test_update_plan_tool | 8 | 计划更新工具（organ + kernel 计划生命周期） | **B** | 迁到 kernel 计划 API |
| test_xenon_comprehensive | 8 | 全引擎综合 | **A** | 主体全删 |
| test_b8_verify_file_claims | 7 | `PlanExecuteEngine` 的 claim 校验 | **A** | 引擎已删（claim 由 kernel 审计覆盖） |
| test_pull_request_tool | 7 | PR 工具（organ） | **B** | 去 `react_prompts` |
| test_runtime_resilience | 7 | `base_module` 计时 + `ReActEngine` | **A** | 两者皆删 |
| test_step_budget | 7 | `builtin_engines` 的 `STANDARD_STEP_BUDGET` | **A** | 预算改由 kernel `Budget` |
| test_turn_gate | 7 | `TurnGate` 空洞检测（organ，`repl` 在用） | **C** | 去掉 `registry.EngineSpec` 依赖 |
| test_issue165_report_intent | 6 | `strategy_guide` 建议文本 | **A** | 提示词骨架已删 |
| test_mcp_management_tools | 6 | MCP 管理工具（organ） | **B** | 去 `react_prompts` |
| test_output_completeness | 6 | `_answer_looks_incomplete`（引擎私有） | **A** | 引擎已删 |
| test_context_projection | 5 | `ReActEngine` 上下文投影 | **A** | 引擎已删 |
| test_evidence_gate_chain | 5 | 证据门链（organ） | **B** | 去引擎夹具 |
| test_execution_contract | 5 | 三份提示词共享同一契约文本 | **A** | 提示词骨架已删；kernel 自带协议 |
| test_tool_result_read | 5 | 读工具结果（organ） | **B** | 去引擎夹具 |
| test_tracker_detail_log | 4 | `ToolExecutionTracker` 明细（organ） | **B** | 仅用 `PlanExecuteEngine` 作夹具 |
| test_agent_result | 3 | `AgentResult` 终态 | **A** | 终态判定在 kernel（`TaskResult`），旧结果类型仅兼容 |
| test_contract_single_source | 3 | 架构不变量（扫源码） | **C** | 去掉 `react_engine` 导入 |
| test_loop_hint_no_force_stop | 2 | ReAct 循环提示 | **A** | 循环守卫已换（#323） |
| test_react_loop_exit | 2 | ReAct 循环终止原因 | **A** | 同上，`RepeatedCallGuard` 覆盖 |
| test_research_resilience | 2 | 引擎韧性 | **A** | 引擎已删 |

## 结论

- **A 类（删）18 个文件 / 约 130 用例**：断言对象是引擎内部行为或引擎提示词文本。
- **B 类（迁）17 个文件 / 约 240 用例**：断言对象是仍在使用的器官，**不能删**——删掉会丢约三分之二覆盖。
- **C 类（守卫）2 个文件 / 10 用例**：只需移除引擎导入。

先执行 A 类（证据充分、无覆盖损失），B/C 类逐文件迁移。
