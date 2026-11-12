# Xenon 架构设计（v1.0.0）

> 中文 | [English](ARCHITECTURE.en.md)

> **设计哲学：证据我管、真伪你核。**
>
> 机器只对「过程真实性」负责——做了什么、成败几何、证据链是否完整；对
> 「内容正确性」做分级透明披露，把语义判断还给用户。这是 Xenon 与
> "直接相信模型输出"的 Agent 运行时最大的分野。

## 0. 单循环内核（v0.9 当前控制流）

> 本节是内核**控制流**的权威视图，从 README 迁移而来；下文第 1 节起是
> 可信度分层与工程模块的**实现视角**。两者描述同一系统的不同切面。

### 0.1 为什么是单循环

旧版 Xenon 有 7 个“推理引擎”。`engine/registry.py` 自己承认：七种内置范式
**运行流程完全同构**，差异只有引擎类、少量参数、模式行和面板标题。结果是
同一套循环被复制 7 次，工具语义散落在 prompt、plan 校验、TaskState、
ClaimGate、delivery 五处。现在只剩一条控制流：

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

### 0.2 内核总览

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

### 0.3 七条不变量（I1–I7）

- **I1 单 writer**：同一条账本在任何时刻只有一个写者。进程内经 `Ledger.open_shared` 共享同一实例；
  跨进程 append 前比对文件末尾，发现被推进就重新加载（`_refresh_tail_if_moved`），绝不静默分叉。
  运行时、runner 与子进程生命周期记录都遵守这条。
- **I2 单入口**：所有工具副作用经过 `ToolGateway`；交付提交由 `Finalizer` 独占。
- **I3 单事实源**：ledger 是唯一真相；其他都是带 `input_seq` 的只读投影。
- **I4 语义接口**：kernel 只认 capability / obligation / receipt；工具名只活在 catalog。
- **I5 提案/事实分离**：LLM 输出是 `claim-*` / `hyp-*`；只有 binder 能产生 `fact-*`。
- **I6 单一决策权**：`VerificationController` 决定 repair / replan / abstain / abort。
- **I7 可回放**：同一 ledger 前缀产生逐字节一致的投影，回放过程零网络。

### 0.4 一个回合怎么走

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

### 0.5 验证层级（V0 / V1 / V2）

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

### 0.6 工具与 MCP

- `ToolCatalog` 用 **8 类 capability** 表达工具语义：`read / search / discover / edit / verify / vcs / shell / remote`；
- 工具名只在 catalog/spec 出现，kernel 不分支具体工具名；
- `discover` 能力（只读工具 `find_tools`）在调用前按意图查询目录、返回有界候选及完整参数但不执行，所有权限模式下免费；
- MCP 使用 **per-tool ToolSpec**，不是把所有工具都塞给模型；
- 工具预算按 objective 做确定性选择（例如 46 个 GitHub 工具只注入 16 个），未知工具 `unresolved`；
- approval 是外部 MCP 调用的边界；每次授权绑定目标并带 TTL，调用后立即撤销；
- `git push` 在显式 `TaskSpec.allow_push` 前保持 veto。

## 1. 三层可信度模型

> ⚠️ **【双轨现状 · 更新于 2026-11-11】** 本节及第 2、3 节的 L1/L2 口径是
> **历史**，`TurnGate / HollowDetector` 已退役；第 0 节的 `LoopRuntime`（kernel）
> 是唯一的执行与验证链。REPL 经
> `should_route_to_kernel` 分流：工具轮次走 `LoopRuntime`（账本在 `ledger/`）。
> **2026-11-11 审计确认 `TurnGate` 路径不可达**（`repl._render_engine_result`
> 零调用方 → `_verify_turn` → `TurnGate.evaluate` 只互相调用），`turn/gate.py`、
> `engine/task_verifier.py`、`engine/hollow_detector.py` 已删除。下表保留 L0/L1/L2
> 的历史口径，但**当前唯一的验证链是内核 + 账本**；未验证内容以内核证据标签与
> 「待人工核验」披露面板呈现，不阻断交付。

| 层 | 机制 | 位置 | 机器职责 |
|---|---|---|---|
| **L0 证据层** | **当前**：内核账本 = append-only 哈希链（`ledger/`），`verify_tree` 连子分支一起校验；**历史**：会话事件日志（`session/events.py`，仍支撑 `/resume`、`/evidence`） | `ledger/store.py`、`ledger/tree.py`；legacy `session/events.py`、`repl/command_groups/evidence.py` | 证明过程真实；链完整性检查（无密钥锚点，见 README 诚实边界） |
| **L1 结构层**（已退役 · 2026-11-11） | 历史：空洞回答检测。内核以工具回执与债务表达"是否干了活" | （原 `turn/gate.py`、`engine/hollow_detector.py`，已删除） | 不再有阻断类结构校验 |
| **L2 语义层**（内核对等物） | 宣称绑定回执、完成准则、覆盖审计：不阻断发布；partial/unverified 时附「待人工核验」面板 | `kernel/controller.py`、`repl/render.py`（`render_verification_reminders`） | 把核不到的醒目列给用户自核 |

> 术语：上表的 **L0/L1/L2 是可信度层级**（证据 / 结构 / 语义）。
> 执行内验证层级使用 **V0/V1/V2**（每次 receipt 的不变量 / 步骤后置条件 /
> 任务级覆盖审计），两者不要混用。设计见 `docs/adr/0001-single-loop-kernel.md`
> 与 #205。

不变式：**证据只是标签，不是闸门**——内核决定是否交付，`FinalizeGuard` 是唯一的
确定性硬判定；**工具成败只由工具回执的 `success` 字段决定**（单一语义通道），
面板/事件/渲染不用关键词猜。`tests/test_contract_single_source.py` 以全仓扫描
锁定契约构建入口。反思阶段另有 reviewer 评分，属于输出质量审查，不是工具事实或
Claim 的证明。

## 2. 系统总览

```
                    ┌──────────────────────────────────────────┐
                    │        REPL / CLI（repl/）                │
                    │  输入渲染 · 命令组 · 审批面板 · 会话操作    │
                    └───────────────┬──────────────────────────┘
                                    │ 回合编排（kernel_host / turn_flow）
              ┌─────────────────────┼───────────────────────┐
              ▼                     ▼                       ▼
      TurnContract           Kernel（kernel/，单循环）   Session（session/，legacy）
      意图分类 + 正则护栏      LoopRuntime + 阶段配置      tree / events（仍供
      → 执行策略               投影 / 压缩 / 循环信号        /resume · /evidence）
              │                token 预算                    │
              └──────────┬──────────┴───────────┬───────────┘
                         ▼                      ▼
                    ToolExecutor             FinalizeGuard + 证据标签
                    7 阶段管线                verified / partial / unverified
                    policy·fence·retry              │
                         │                          ▼
                    ToolNode / 工具族           账本追加（ledger/）+ 可回放投影
```

## 3. 回合生命周期（单一状态机）

```
用户输入
  → TurnContract：LLM 意图分类器主导 + 正则护栏约束（fail-closed 降级）
  → 循环执行（kernel/）：阶段推进、循环信号（`loop_signals`）、断路器、
    token 预算与压缩、截断诚实标注 + 续写
  → 内核交付（唯一确定性闸门是 FinalizeGuard）：
      finalized → 交付，证据按 verified / partial / unverified 如实标注；
                  partial 与 unverified 附「待人工核验」披露面板（不阻断）
      blocked / aborted / failed → 标题如实显示已阻塞 / 已中止 / 失败，
                  即使已收集的证据恰好是 verified 也不伪装成完成
  → 账本事件追加（ledger/，只读投影可回放）；
    legacy 会话路径另有 TurnTree 节点落盘与结构化回合归档
```

节点状态五值：`passed / degraded / fused / interrupted / pending-approval`
（`session/tree.py`），可续接状态 = `fused / interrupted / failed-retried`；
裸"继续"从可续接节点继承原因与进度。

## 4. 模块边界

```
xenon/
├── kernel/      单循环内核（唯一判定点）：runtime · controller · phases · contracts ·
│                policies（权限模式）· strategies · view（只读投影）· finalize ·
│                audit · session_grants · session_history · loop_advice
├── ledger/      证据链：store（单写者 + 崩溃恢复）· events（哈希链）· tree（分支校验）·
│                retention（可选归档）· projections · migrate（显式导入 legacy）· conversation（对话回合）
├── catalog/     能力目录（工具名唯一真相源）：catalog · specs · gateway · finalizer · worktree
├── harness/     无头编排：runner · llm（提示词/工具调用）· child（子进程 + 分支封存）·
│                intake · plan · cli · sink · summarizer
├── repl/        交互层：repl 主循环 · runtime_adapter（内核回合）· turn_flow（legacy 回合）·
│                direct_chat · render · approval_ux · session_ops ·
│                turn_contract · task_state · command_groups（自动发现）
├── frontend/    前后端协议宿主：host（NDJSON）；TS 前端构建产物随 wheel 分发
│                （assets/xenon-ui.js，运行时需 Node.js 20+）
├── session/     legacy L0 链（仍支撑 /resume · /evidence · /rewind · /goal）：
│                tree（会话树）· events（哈希链事件日志）· archive · projection · query
├── engine/      工具执行层（历史目录名）：callbacks · context · circuit_breaker ·
│                evidence_gate（仅 GateVerdict）· tool_runtime · tool_tracker ·
│                tool_profiles · tool_gate · scheduler · task_recipe · task_state
├── nodes/       工具层：tool_executor（7 阶段：分类→校验→审批→执行→
│                重试→审计）· tool_node（路径围栏/命令黑名单）·
│                tool_registry · tool_families（12 族）· approval_policy ·
│                param_validation · path_targets
├── hooks/       本地 hooks：4 事件 · 项目级信任指纹 · PreToolUse fail-closed
├── sandbox/     沙箱 seam：只读模式 · 命令白名单（链感知）· backends（process/docker/wsl）
├── memory/      记忆：user / project-local / project-shared / session 四作用域
├── mcp/         MCP：客户端（stdio 传输 · registry · 工具注入）· server
│                （stdio JSON-RPC，对外提供 任务/证据/审计 三个工具）
└── utils/       llm_client（传输/续写）· llm_transport · llm_usage · redact（脱敏）·
                 errors（统一错误信封）· credentials_io（加密凭证）· project_root ·
                 github_auth · atomic_write · code_index
```

## 5. 安全边界（真实现，逐条）

| 边界 | 机制 | 关键位置 |
|---|---|---|
| 路径围栏 | 解析符号链接与 junction 取真实路径；UNC / `\\?\` / 8.3 拒绝；环与超长路径回退 | `nodes/tool_node.py` `_validate_path` |
| 危险命令 | 引号感知 + 段锚定黑名单；shell 替换/变量拼接拒绝 | `tool_node._validate_command` |
| 边界审批 | 工作区内写免问；执行级工具（command/spawn_agent/执行型 mcp_call）一律询问；`y` 授权绑定 `(工具, 目标路径)`，`a` 记会话 TTL 规则；非交互 fail-closed | `repl/approval_ux.py`、`nodes/approval_policy.py` |
| 子代理 | 默认只读（min(父级, READ_ONLY)）；审批通道继承父上下文；子任务只能继承授权、不能创建授权 | `harness/child.py`、`harness/child_worker.py` |
| register_tool | 模块白名单 + 危险模块拒绝 + 凭证/配置模块点名拒绝 | `tool_node._validate_register_module` |
| hooks | 用户级始终信任；项目级需指纹信任（`/trust-hooks`）；PreToolUse 超时/崩溃 fail-closed | `hooks/runner.py` |
| 项目配置 | 项目 `.xenon/config.yaml` 只能覆盖 `intent_classifier / auto_route / limits / interaction / retention`；`paths` / `sandbox` / `validation` / `development` 被忽略并告警 | `repl/system_config.py` |
| 凭证 | Windows DPAPI 静态加密（keyring 可选，明文兜底并标注）；所有读者走同一入口；`models.yaml` 不再保存 key | `utils/credentials_io.py` |
| 沙箱 | 只读模式 + 命令白名单（链感知）的确定性拒绝；`sandbox.backend` 可选 docker/wsl（不可用 fail closed）；`process` 后端如实声明非安全边界 | `sandbox/__init__.py`、`sandbox/backends.py` |

## 6. 扩展契约

```python
MODE_CATALOG[mode].loop                     # xenon/modes.py（7 个模式名 / 6 个 profile 的唯一来源）
register_tool(name, handler, params)      # 工具注册（含安全白名单）
register_command("/name", desc, usage)    # command_groups 自动发现，无需中心文件
# 本地 hooks：<user|project>/.xenon/hooks.yaml（PreToolUse / PostToolUse /
#           Stop / SubagentStop，JSON stdin，exit 2 阻断 PreToolUse）
# 自定义 provider：register_custom_provider → credentials.yaml _custom_providers
# MCP：stdio server 注册后工具注入循环（与 ToolExecutor 共用证据链）
```

`xenon/modes.py` 是 `/mode` 与 kernel 路由的**唯一**元数据来源。每个模式只是一个
`LoopProfile`（跑哪些阶段、每步多少调用、反思几轮），不存在第二个执行器：
`react` 与 `direct` 的 profile 完全相同，因此 `react` 只是 `direct` 的别名
（保留名字以兼容 `--mode react`）。

## 7. 大文件拆分状态

| 阶段 | 目标 | 状态 |
|---|---|---|
| A | ToolNode → ToolRegistry + 工具族模块（12 族） | ✅ |
| B | REPL 输入提取（repl_input） | ✅ |
| C | Slash commands 按主题拆分（command_groups 自动发现） | ✅ |
| R | REPL 拆分（repl 2383 行 → 8 模块 + Mixin 组合） | ✅ |
| D | 旧多引擎类拆分 | ✅ 已不需要：引擎已删除，只剩 `LoopRuntime` 与阶段配置 |
| E | LLM client 按传输拆分（已拆 llm_transport/llm_usage，主文件 1532 行） | ⏳ 部分 |

## 8. 质量门禁（CI）

- Python 3.10/3.11/3.12 矩阵 + e2e + 三平台（Ubuntu/Windows/macOS）安装冒烟
- 覆盖率门槛 **70%**（实测 ~74%，`omit = []`）
- Ruff 规则合同显式钉死（`select = E4/E7/E9/F/I/B`，`I`/`B` 分别为 #127 phase 1/2 的 isort 与 bugbear），工具版本可自由升级
- 版本单一来源 `xenon.__version__`（pyproject dynamic 读取）
- 单一真相源不变量测试（semantics / contract single source）随 CI 常跑
