# 前后端分离协议（草案）：Python 内核 ⇄ TypeScript 前端

**状态**：步骤 1–4 已完成并有真实 ConPTY 证据；**2026-11-11 起 TS 是唯一的默认交互入口**（缺少 `node`/构建产物即报错，不再自动降级；`--ui python` / `XENON_UI=python` 是显式 opt-in）。

已完成迁移步骤 1-4：
1. 只读前端（TS 消费 progress/action/turn.end）已验证；
2. TS 接管输入（`turn.start`），Python REPL 退化为宿主；
3. 审批面板走 TS 实现，三档语义由内核保证；
4. Python 侧删除 prompt_toolkit 渲染层：`completer.py`、`command_catalog.py`（命令面板）、
   PT 会话/工具栏/选择器全部移除，`prompt-toolkit` 不再是依赖。内置编辑器
   （`repl_input.py`）保留为**显式选择**的开发/无编辑器环境入口
   （`--ui python`），自带历史（上/下）、多行、Ctrl+O、Shift+Tab。

2026-11-11 补齐的协议面：
- `turn.cancel` → `turn.cancel.result`（取消运行中的内核轮次；首个 Ctrl+C 取消，第二个退出）；
- **计划审批**走 `approval.request`（`kind: "plan"`）+ 既有 `approval.response`，不再调用 `Prompt.ask`；
- **命令内提问**：`prompt.request` / `prompt.response`，覆盖 `/resume`、`/permissions`、
  `/model`、`/setup` 等交互；`/setup` 的 API Key 输入也走这条通道（`secret: true`，前端掩码显示），
  不读进程 stdin；
- `session.select`（按名称恢复会话；分发的是 `/resume` 命令）；
- `files.request` / `files.result`：`@` 路径补全，宿主在工作区内有界列举（跳过 `.git`/`node_modules` 等，最多 50 条，只列名字不读内容）；
- `ready.setup_needed`：内核用本地事实（凭证 + 已配置模型）判断是否首次运行，前端据此提示 `/setup`；
- `status` 增加 `phase` / `steps` / `tool_calls` / `context_tokens` / `context_used_pct` / `elapsed_seconds` / `stalled_seconds`；
- 审批请求携带内核的 `advisories`（不可信内容提示，非结论）。
- `/mode next`（或 `cycle`）把 Shift+Tab 的循环切换暴露为命令，供前端直接调用。

**动机**：终端 UI 的脆弱不在 Python 语言，而在渲染/输入层。已在真实 ConPTY
里复现的问题都属于这一类：`prompt_toolkit` 与 `Prompt.ask` 抢 stdin、
winpty 把方向键的 ESC 拆成两次事件、面板输出靠 monkeypatch `console` 才能测。
换一个成熟的 TUI 框架能解决这些问题；把整个后端换掉则会让证据链多一条
跨进程通道，那正是 Xenon 最需要守住的地方。

**结论**：后端保持 Python，前端换成 TS（Ink），两者用下面的协议通信。

---

## 1. 切在哪条缝

内核侧已经有三个回调点，它们就是天然的协议边界：

| 现有回调 | 位置 | 方向 | 语义 |
|---|---|---|---|
| `progress_sink(seconds, chars)` | `harness/llm.py` · `ModelConfig` | 内核 → 前端 | 仍在生成，多久、多少字 |
| `request_sink(event: dict)` | 同上 | 内核 → 前端 | provider 阶段：尝试 / 重试 / 超时 / 失败 |
| `ApprovalProvider.request(view, action, decision) -> bool` | `kernel/protocols.py` | 内核 ⇄ 前端 | 需要人决定，阻塞等待 |

另外 `ReplEventSink` 已经把每一轮的结果送进前端，`status_snapshot()` 提供状态栏
所需的实时数字。**协议不新增内核概念，只把这三处改成可跨进程的形式。**

不跨进程的东西：Ledger、控制器、策略、工具执行、子进程编排。它们只在
Python 进程内运行，前端看不到也不需要看。

---

## 2. 传输

- 帧格式：一行一个 JSON 对象（NDJSON），UTF-8，`\n` 结尾。
- 双向流：内核 `stdin/stdout`，或本地 socket（`\\.\pipe\xenon-<pid>`）。
- 前端与内核是父子进程：前端 spawn Python，stderr 留给内核自身的日志。
- 每个消息带 `v`（协议版本）与 `id`（请求-应答配对）；通知类没有 `id`。

```jsonc
{ "v": 1, "id": "42", "type": "approval.request", ... }   // 需要回答
{ "v": 1, "type": "progress", ... }                        // 单向通知
```

---

## 3. 消息类型

### 3.1 `turn.start` — 前端 → 内核

用户提交一句话。

```jsonc
{
  "v": 1, "id": "t-1", "type": "turn.start",
  "objective": "把 calc.py 的 add 改成支持负数",
  "model_ids": ["deepseek/deepseek-v4-flash"],
  "level": "auto-edit",          // read-only | auto-edit | scoped | bypass
  "context": { "cwd": "D:\\proj" }
}
```

### 3.2 `progress` — 内核 → 前端

对应 `progress_sink`。高频，前端自行节流。

```jsonc
{ "v": 1, "type": "progress", "turn": "t-1",
  "elapsed_seconds": 12.4, "chars": 830 }
```

### 3.3 `provider` — 内核 → 前端

对应 `request_sink`。**必须原样转发 `kind`/`phase`**，不要在前端重新推断：
真实重试与首次尝试看起来一样，只有内核知道区别。

```jsonc
{ "v": 1, "type": "provider", "turn": "t-1",
  "kind": "retry",            // start | retry | end | timeout
  "phase": "model",
  "attempt": 2, "provider_attempts": 3,
  "reason": "429" }
```

### 3.4 `approval.request` — 内核 → 前端（阻塞）

对应 `ApprovalProvider.request`。**这是唯一需要人回答的消息**，也是本次
改动的重点（技术文档 §9 三档 + 工具/执行路径绑定）。

```jsonc
{
  "v": 1, "id": "a-7", "type": "approval.request",
  "turn": "t-1",
  "capability": "shell",       // read|search|edit|verify|shell|vcs|remote
  "tool": "bash",              // 仅用于显示；内核按 capability 裁决
  "targets": ["python -c \"print(123)\""],   // 该授权的执行路径
  "reason": "shell requires approval",
  "risk": "CRITICAL",
  "options": ["once", "turn", "allow", "deny", "cancel"],
  "turn": "repl-a1b2c3d4e5f6",   // 选项 "turn" 的作用域就是这一轮
  "diff_preview": null,        // 写类操作给 unified diff 文本
  "advisories": [              // 可选：不可信内容来源
    { "source_type": "web", "source_ref": "https://…", "rule_id": "prompt-injection" }
  ]
}
```

前端可以只渲染 `options` 里给出的项（技术文档 §9 的四档）：

| 值 | 含义 | 作用域 | 失效点 |
|---|---|---|---|
| `once` | 本次允许 | 这一次调用 | 立即 |
| `turn` | 本轮允许 | 该 capability，本轮内任意执行路径 | 本轮结束 |
| `allow` | 全部允许 | 该工具 + 该路径 | 30 分钟 / 会话结束 |
| `deny` | 拒绝 | — | — |
| `cancel` | 取消整个任务 | — | — |

`turn` 存在的原因：真实任务里模型对同一意图写出四种命令拼写，按精确路径
每次都算"不同目标"，于是弹了四次面板。`turn` 只在**这一轮**内合并，下一轮
重新询问；它不跨 capability，也不会突破破坏性命令 / push 的硬规则。

**前端不得自行决定"记住"** —— 记忆只能由内核对 `allow` 的应答建立，
否则一个前端 bug 就能积累权限。

### 3.5 `approval.response` — 前端 → 内核

```jsonc
{ "v": 1, "id": "a-7", "type": "approval.response",
  "choice": "allow" }
```

### 3.6 `action` — 内核 → 前端

每一步发生了什么，用于时间线展示。数据来自账本事实，不是前端猜的。

```jsonc
{ "v": 1, "type": "action", "turn": "t-1",
  "index": 3,
  "kind": "tool",              // think | tool | plan | finish
  "capability": "shell",
  "target": "python -c \"print(123)\"",
  "status": "succeeded",       // pending|running|succeeded|failed|denied
  "receipt_id": "tr_c68190ed…",
  "effects": [{ "kind": "command_ran", "exit_code": 0 }] }
```

### 3.7 `turn.end` — 内核 → 前端

```jsonc
{
  "v": 1, "id": "t-1", "type": "turn.end",
  "phase": "finalized",        // finalized|blocked|failed|aborted|planned
  "accepted": true,
  "answer": "……",
  "ledger": "~/.xenon/ledger/repl/…jsonl",
  "evidence": { "status": "verified" },   // verified|partial|unverified（小写，枚举原值）

  // 仅失败帧（host 捕获异常时）：
  "error": "…",
  "error_kind": "llm",         // permission|network|llm|tool|integrity|internal|cancelled
  "retryable": true            // 客户端可据此决定是否重试，不必解析 provider 文案
}
```

`evidence` 是产品主张所在，**必须由内核计算并原样透传**，前端只负责展示。
任何"前端自己判 VERIFIED"的实现都是错的。

> 逐条 claim/debt 投影**尚未上线**：`TaskResult` 已有 `claims` / `debt`，但 wire 上目前只传
> `evidence.status` 与 `ledger`。前端不要自行推断逐条状态；需要时再按本协议追加字段。

### 3.8 `status` — 内核 → 前端（周期推送）

```jsonc
{ "v": 1, "type": "status",
  "mode": "direct", "model": "deepseek/deepseek-v4-flash",
  "context_used_pct": 0.034, "cache_hit_pct": 0.51,
  "cost_cny": 0.0034, "elapsed_seconds": 48 }
```

---

## 4. 顺序与取消

- 同一个 `turn` 内，`action` 严格按序；`approval.request` 出现时前端应暂停渲染
  该轮的时间线并聚焦面板。
- 前端取消：`{ "type": "turn.cancel", "turn": "t-1" }` → 内核走既有的取消语义
  （先优雅终止，超时强杀，未完成动作记 `interrupted`）。
- 内核崩溃：前端读到 EOF 即视为失败，**不得**把上一轮的 `answer` 当成结果。

## 5. 兼容与版本

- `v` 不匹配时前端必须报错退出，不做静默降级。
- 新增字段是向后兼容的；新增消息类型需要 bump `v`。
- 内核侧现有 Python 前端继续可用：协议层只是把三个回调改成转发器，
  回调签名不变（`docs/design/` 之外的代码不需要改）。

---

## 6. 迁移顺序（建议）

1. **只读前端**：TS 进程只消费 `progress` / `action` / `turn.end`，渲染时间线，
   不管输入。用真实任务对比 Python 前端的输出。 → 验证协议够用。
2. **接管输入**：TS 负责读一行输入并 `turn.start`。Python REPL 退化成内核宿主。
3. **接管审批**：`approval.request` 走 TS 面板；三档语义由内核保证，前端只画。
4. **删除 Python TUI 渲染层**（已完成）：prompt_toolkit 组件（补全器、命令面板、选择器菜单、
   工具栏）全部删除。`approval_ux.py` / `selector.py` / `status_bar.py` 作为**无头辅助**保留：
   它们只做 rich 文本与状态数据，`--ui python` 显式路径和协议桥接仍在使用。

每一步都能单独回滚：协议适配器在 Python 侧，删掉即回到现状。

---

## 7. 明确不做的事

- 不把 Ledger 或证据计算搬到前端 —— 那会让"证据"变成可以被前端影响的东西。
- 不让前端持有或缓存授权 —— 记忆属于内核/前端宿主的内存态，且永不落盘（§9）。
- 不做"前端直接调 LLM"的旁路 —— 所有模型调用都要经过内核才有账本。

---

## 8. 运行方式（当前实现）

```bash
# 1) 后端宿主：NDJSON 协议，只读写 stdin/stdout，日志走 stderr
python -m xenon.frontend.host --model deepseek/deepseek-v4-flash

# 2) TS 前端：会 spawn 上面的宿主
cd frontend
npm install
npm run build
npm start                      # = node dist/cli.js
# 可选：--model <id>  --level read-only|auto-edit|bypass  --python <exe>  --cwd <dir>  --resume <序号|名称>
```

`xenon` 现在只有这一个产品入口。TypeScript 侧的类型检查：
`cd frontend && npx tsc -p tsconfig.json --noEmit`。

**默认入口**（2026-11-11 起）：`xenon` 拉起 TS 前端
（`node <bundle> --python <当前解释器> [--resume <目标>]`）。bundle 优先取 wheel 内的
`xenon/frontend/assets/xenon-ui.js`，源码检出则用 `frontend/dist/cli.js`。
`--resume` / `xenon resume` 也走这里：目标作为 `--resume` 传给宿主，宿主启动后执行一次
`/resume`，与运行中输入 `/resume` 是同一条路径。

缺少 `node` 或前端构建产物时**报错并退出（code 1）**，打印构建步骤；
Piped stdin（非 TTY）同样报错并指向 `xenon exec`。不会静默降级到另一套界面。
`xenon --ui python` / `XENON_UI=python` 是显式的开发/无编辑器环境选项，功能少于 TS 前端。
