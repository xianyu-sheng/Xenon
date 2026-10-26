# Xenon 架构设计（v0.9.2）

> 中文 | [English](ARCHITECTURE.en.md)

> **设计哲学：证据我管、真伪你核。**
>
> 机器只对「过程真实性」负责——做了什么、成败几何、证据链是否完整；对
> 「内容正确性」做分级透明披露，把语义判断还给用户。这是 Xenon 与
> "直接相信模型输出"的 Agent 运行时最大的分野。

## 1. 三层可信度模型

| 层 | 机制 | 位置 | 机器职责 |
|---|---|---|---|
| **L0 证据层** | 会话事件日志 = append-only 哈希链（每条事件带内容哈希 + 前驱哈希）；`/evidence` 随时重算自证 | `session/events.py`、`repl/command_groups/evidence.py` | 证明过程真实；链完整性检查（无密钥锚点，见 README 诚实边界） |
| **L1 结构层** | 空洞回答（确定性、无语义盲点）= 唯一阻断类校验；命中才重试（半预算）/同因熔断 | `turn/gate.py`、`engine/hollow_detector.py` | 拦截"没干活"的回答 |
| **L2 语义层** | 宣称审计 / 完成准则 / 文件声称 / 写入后验证：不阻断发布，附「待人工核验清单」 | `turn/gate.py`、`repl/render.py` | 把核不到的醒目列给用户自核 |

> 术语：上表的 **L0/L1/L2 是可信度层级**（证据 / 结构 / 语义）。
> 执行内验证层级使用 **V0/V1/V2**（每次 receipt 的不变量 / 步骤后置条件 /
> 任务级覆盖审计），两者不要混用。设计见 `docs/adr/0001-single-loop-kernel.md`
> 与 #205。

不变式：**回合级 L1/L2 语义/结构调整只有一处**（`turn/gate.py`），由
`tests/test_semantics_single_source.py` / `test_contract_single_source.py`
以全仓扫描断言锁定；**工具成败只由工具回执的 `success` 字段决定**（单一
语义通道），面板/事件/渲染不用关键词猜。Reflection 引擎另有 reviewer
评分，属于输出质量审查，不是工具事实或 Claim 的证明。

## 2. 系统总览

```
                    ┌──────────────────────────────────────────┐
                    │        REPL / CLI（repl/）                │
                    │  输入渲染 · 命令组 · 审批面板 · 会话操作    │
                    └───────────────┬──────────────────────────┘
                                    │ 回合编排（engine_host / turn_flow）
              ┌─────────────────────┼───────────────────────┐
              ▼                     ▼                       ▼
      TurnContract              Engine（engine/）        Session（session/）
      意图分类 + 正则护栏         7 范式执行器              tree / events(哈希链)
      → 执行策略                 预算检查点 / 循环检测      / archive / projection
              │                     │                       │
              └──────────┬──────────┴───────────┬───────────┘
                         ▼                      ▼
                    ToolExecutor             TurnGate（turn/gate.py）
                    7 阶段管线                pass / remind / fail / fuse
                    policy·fence·retry              │
                         │                          ▼
                    ToolNode / 工具族           树节点落盘 + 事件链追加 + 归档
```

## 3. 回合生命周期（单一状态机）

```
用户输入
  → TurnContract：LLM 意图分类器主导 + 正则护栏约束（fail-closed 降级）
  → 引擎执行：预算检查点（有进展→审批续跑；无进展→熔断）、LoopDetector、
    断路器、截断诚实标注 + 续写
  → TurnGate 判定（唯一判定点）：
      pass   → 发布
      remind → 发布 + 待人工核验清单（L2，不阻断）
      fail   → 同回合反馈重试（预算 = 首轮步数/2，clamp[10,40]）
               耗尽 → 降级发布（degraded + 核验清单）
      fuse   → 同因结构失败熔断，按未完成交付
  → TurnTree 节点落盘 + 事件哈希链追加 + 结构化回合归档
```

节点状态五值：`passed / degraded / fused / interrupted / pending-approval`
（`session/tree.py`），可续接状态 = `fused / interrupted / failed-retried`；
裸"继续"从可续接节点继承原因与进度。

## 4. 模块边界

```
xenon/
├── repl/        交互层：repl 主循环 · engine_host（回合编排）· turn_flow ·
│                direct_chat · render · approval_ux · session_ops ·
│                turn_contract · task_state · command_groups（自动发现）
├── turn/        回合校验层：gate.py（L1 阻断 / L2 提醒 / 同因熔断）
├── session/     结构层：tree（会话树）· events（哈希链事件日志）·
│                archive（回合归档）· projection（目标投影）· query
├── engine/      执行层：registry + builtin_engines（7 范式，direct 非引擎）；
│                react / plan_execute / reflection / combined；base（协议）；
│                evidence_runtime（L0 账本）· evidence_gate · task_verifier ·
│                hollow_detector · loop_detector · circuit_breaker · budget
├── nodes/       工具层：tool_executor（7 阶段：分类→校验→审批→执行→
│                重试→审计）· tool_node（路径围栏/命令黑名单）·
│                tool_registry · tool_families ×9 · approval_policy ·
│                path_targets
├── hooks/       本地 hooks：4 事件 · 项目级信任指纹 · PreToolUse fail-closed
├── sandbox/     沙箱 seam：只读模式 · 命令白名单（链感知）· 后端检测
├── memory/      记忆：user / project-local / project-shared / session 四作用域
├── mcp/         MCP：stdio 传输（select + 墙钟超时）· registry · 工具注入
└── utils/       llm_client（传输/续写）· llm_transport · llm_usage ·
                 github_auth · atomic_write · code_index
```

## 5. 安全边界（真实现，逐条）

| 边界 | 机制 | 关键位置 |
|---|---|---|
| 路径围栏 | 解析符号链接与 junction 取真实路径；UNC / `\\?\` / 8.3 拒绝；环与超长路径回退 | `nodes/tool_node.py` `_validate_path` |
| 危险命令 | 引号感知 + 段锚定黑名单；shell 替换/变量拼接拒绝 | `tool_node._validate_command` |
| 边界审批 | 工作区内写免问；执行级工具（command/spawn_agent/执行型 mcp_call）一律询问；`y` 授权绑定 `(工具, 目标路径)`，`a` 记会话 TTL 规则；非交互 fail-closed | `repl/approval_ux.py`、`nodes/approval_policy.py` |
| 子代理 | 默认只读（min(父级, READ_ONLY)）；审批通道继承父上下文 | `engine/react_engine.py` |
| register_tool | 模块白名单 + 危险模块拒绝 + 凭证/配置模块点名拒绝 | `tool_node._validate_register_module` |
| hooks | 用户级始终信任；项目级需指纹信任（`/trust-hooks`）；PreToolUse 超时/崩溃 fail-closed | `hooks/runner.py` |
| 沙箱 | 只读模式 + 命令白名单（链感知）的确定性拒绝；诚实声明"检测 + 软件边界，非真隔离" | `sandbox/__init__.py` |

## 6. 扩展契约

```python
register_engine(name, factory, ...)       # engine/registry.py（7 范式唯一来源）
register_tool(name, handler, params)      # 工具注册（含安全白名单）
register_command("/name", desc, usage)    # command_groups 自动发现，无需中心文件
# 本地 hooks：<user|project>/.xenon/hooks.yaml（PreToolUse / PostToolUse /
#           Stop / SubagentStop，JSON stdin，exit 2 阻断 PreToolUse）
# 自定义 provider：register_custom_provider → credentials.yaml _custom_providers
# MCP：stdio server 注册后工具注入引擎（与 ToolExecutor 共用证据链）
```

引擎注册表是范式元数据的唯一来源（`engine/builtin_engines.py`）：`direct`
的 factory 为 None——它是 REPL 的纯对话分支，不是引擎；其余 6 个（react /
plan-execute / reflection + 3 个组合）为真实引擎实现。

## 7. 大文件拆分状态

| 阶段 | 目标 | 状态 |
|---|---|---|
| A | ToolNode → ToolRegistry + 工具族模块（9 族） | ✅ |
| B | REPL 输入提取（repl_input） | ✅ |
| C | Slash commands 按主题拆分（command_groups 自动发现） | ✅ |
| R | REPL 拆分（repl 2376 行 → 8 模块 + Mixin 组合） | ✅ |
| D | ReAct/Base 引擎拆协议解析/工具循环/子代理 | ⏳ 待定（react_engine 1872 行 / base 1857 行） |
| E | LLM client 按传输拆分（已拆 llm_transport/llm_usage，主文件 1423 行） | ⏳ 部分 |

## 8. 质量门禁（CI）

- Python 3.10/3.11/3.12 矩阵 + e2e + 三平台（Ubuntu/Windows/macOS）安装冒烟
- 覆盖率门槛 **70%**（实测 ~74%，`omit = []`）
- Ruff 规则合同显式钉死（`select = E4/E7/E9/F/I/B`，`I`/`B` 分别为 #127 phase 1/2 的 isort 与 bugbear），工具版本可自由升级
- 版本单一来源 `xenon.__version__`（pyproject dynamic 读取）
- 单一真相源不变量测试（semantics / contract single source）随 CI 常跑
