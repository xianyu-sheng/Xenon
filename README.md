# Xenon

一个交互式 **AI 编码代理（Agent Harness）**：在终端里与多模型协作完成任务——读写代码、执行命令、验证结果，每一步都经过确定性校验与可审计的权限边界。

> Agent Harness：决定 AI Agent 输出是否可信的运行时层。Xenon 的核心不是"又一个 LLM 客户端"，而是围绕 **单一语义通道、回合级校验门、可回溯会话树** 构建的代理骨架。

## 核心差异

| 能力 | 说明 |
|---|---|
| **TurnGate 回合校验门** | 唯一校验层：宣称审计（失败回执 vs "已完成"宣称）、文件声称审计（声称写了但没落盘）、写入后验证、空洞回答检测；判定 pass 发布 / fail 反馈重试 / fuse 同因熔断。模型"自己说做完了"不再等于任务成立 |
| **会话树（TurnTree）** | 每个用户回合是一个节点（状态/产物/判定/事件指针）。`/rewind` 回退、`/fork` 分叉、裸"继续"自动从**中断/熔断节点**续接（注入原因与进度摘要） |
| **预算检查点（非固定步数）** | 固定步数不终止任务：窗口耗尽时有进展 → 挂起审批"是否继续"；原地打转 → LoopDetector 熔断；同因基础设施错误（如 402 欠费）连续两轮 → 快速熔断并给出指引 |
| **结构化回合归档** | 长对话自动压缩：旧回合确定性折叠为意图/工具成败/失败日志/决策/产物归档块，零 LLM 开销；同时是检索摘要源 |
| **事件日志（全量记录 + 快车道）** | 每回合完整记录持久化（`~/.xenon/sessions/events/`），可查询/回放；会话树节点只存指针与摘要——回溯走树，真相走日志 |
| **边界审批** | 工作区内写免问；越界写与命令询问（统一面板 + diff 预览 + `a` 登记带 TTL 的会话规则）。审批是封闭词表、fail-closed |
| **子代理默认只读** | `spawn_agent` 委派的子代理执行级别封顶 READ_ONLY（永不提升），`read_only=false` 显式放行 |
| **诚实截断标注** | 输出因长度限制被截断时，回复尾部可见标记，不伪装完整 |
| **hooks** | 用户/项目 `.xenon/hooks.yaml`：PreToolUse / PostToolUse / Stop / SubagentStop（exit 2 阻断，JSON stdin） |

## 快速开始

```bash
# 克隆源码本地安装（editable，源码即运行版）
git clone https://github.com/xianyu-sheng/Xenon.git
cd Xenon
python -m venv .venv
.venv/Scripts/python -m pip install -e .        # Windows
source .venv/bin/activate && pip install -e .   # Linux/macOS

# 启动
xenon
# 首次运行会引导配置（/setup：Key、模型、范式）
```

常用命令：`/plan`（计划模式）`/goal`（目标投影）`/review`（本会话改动 + revert）`/rewind <n>` `/fork [n]` `/compact` `/sessions` `/permissions` `/tools` `/help`

## 会话状态机（每回合）

```
用户输入 → 单一语义通道（TurnContract，LLM 意图分类器 + 正则护栏）
  → 引擎执行（纯执行器：预算检查点护栏 + 中断 + 循环检测）
  → TurnGate 回合校验（唯一判定点）
      pass → 发布
      fail → 同回合反馈重试（预算=首轮实际步数/2，clamp[10,40]）
      fuse → 同因熔断 / 无进展熔断，诚实交付
  → 树节点状态落盘 + 回合归档 + 事件日志
```

校验语义只有一处（`xenon/turn/gate.py`），引擎内部没有第二套验证循环。

## 环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `XENON_APPROVAL_TTL` | 会话级 `a` 授权规则 TTL（秒） | 1800 |
| `XENON_VERIFY_RETRIES` | 发布门失败后同回合重试次数 | 2 |
| `XENON_RETRY_BUDGET_MIN/MAX` | 重试轮预算 clamp | 10 / 40 |
| `XENON_GATE_FUSE_THRESHOLD` | 同因校验失败熔断阈值 | 2 |
| `XENON_MAX_ITERATIONS` | 单次运行步数（检查点窗口基准） | 40 |
| `XENON_CHECKPOINT_STEPS` | 检查点窗口大小 | = 步数上限 |
| `XENON_MAX_TOKENS` | 单次 LLM 调用输出上限 | 8192 |
| `XENON_MAX_CONTINUATIONS` | 截断自动续写次数 | 3 |
| `XENON_READ_ONLY` | 只读模式（禁止一切写/执行工具） | 关 |
| `XENON_COMMAND_ALLOWLIST` | 命令首词白名单（逗号分隔） | 空=不限 |
| `XENON_SESSION_EVENTS(_DIR)` | 事件日志开关/目录 | 开 |
| `XENON_HOOKS` | hooks 开关 | 开 |
| `XENON_INTENT_CLASSIFIER_ENABLED` | LLM 意图分类器 | 开 |

## 架构分层

```
xenon/
├── repl/        交互层：REPL 主循环、引擎宿主(engine_host)、渲染(render)、
│                回合契约(turn_contract)、任务状态(task_state)、命令组
├── turn/        回合层：TurnGate 唯一校验层
├── session/     结构层：tree(会话树) / events(事件日志) / archive(回合归档)
│                / projection(目标投影) / query(查询)
├── engine/      引擎层：react / plan_execute / 组合引擎（纯执行器）
├── nodes/       工具层：tool_executor / approval_policy / path_targets
├── hooks/       本地 hooks 执行器
└── sandbox/     沙箱 seam：只读模式 + 命令白名单 + WSL/Docker 检测
```

## Windows 说明

- Windows Terminal (ConPTY) + PowerShell 全功能支持（粘贴、中文 IME、方向键）。
- `XENON_TERMINAL_ANIMATION=0` 可关闭动画。
- 推送 Git 若网络异常：`git -c http.proxy= -c https.proxy= -c http.sslBackend=openssl push`（或走代理 `http://127.0.0.1:3067`）。

## 诚实边界

- **沙箱是检测 + 软件边界，不是真隔离**：不可信代码请在 WSL/Docker 容器内运行 Xenon。
- **发布门是审计级**：校验失败不发布成功结论，但不会无限自动重试（熔断兜底）。
- `/review` 的 revert 只覆盖有 `.bak` 备份的编辑；用 git 管理不可替代。
- 意图分类器默认走最快可用模型，有 ~1s 延迟；不可用自动降级正则（保守）。

## License

MIT
