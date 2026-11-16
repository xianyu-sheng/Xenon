# 本地可视化面板（Dashboard）

**状态**：Part E 已落地。默认入口仍是终端 `xenon`；`/dashboard`（或 `xenon --dashboard`）
在**同一进程内**再起一个只监听回收地址的本地 Web 面板。

面板是**同一个会话的第二个渲染器**，不是第二个真相源：

- 它不 spawn 内核、不 import `xenon.repl.*`、不直接读账本；
- 它只消费终端已经在消费的 NDJSON 帧（`ready` / `turn.*` / `action` / `approval.*` …），
  以及宿主提供的**只读查询帧**；
- 写操作只有四种，与终端键位一一对应：提交一句输入、运行一条斜杠命令、
  回答审批、回答问题。没有 shell/eval 端点。

```
                    ┌────────────────── 同一个 Node 进程 ──────────────────┐
xenon.exe (ConPTY) ⇄│ KernelHost（stdio NDJSON）                           │
                    │   │ frames                                          │
                    │   ├── Ink 终端 UI（默认入口，行为不变）              │
                    │   └── SessionController ──SSE──▶ 浏览器页面          │
                    │            ▲                                        │
                    │            └── POST /api/action（输入/命令/审批/提问）│
                    │                GET  /api/query（只读查询帧）         │
                    └─────────────────────────────────────────────────────┘
```

## 1. 启动与生命周期

| 入口 | 行为 |
|------|------|
| `/dashboard` | 终端里输入即可；浏览器打开后终端照常可用 |
| `xenon --dashboard` | 启动时自动起面板；启动摘要与欢迎卡不变 |
| `XENON_DASHBOARD=0` | `/dashboard` 明确报错「仪表盘已关闭」，`--dashboard` 不启动 |
| `xenon --ui python` + `--dashboard` | Python 编辑器界面不支持，退出码 2，并说明原因 |

- 地址形如 `http://127.0.0.1:<随机端口>/?t=<每进程随机 token>`；终端里以一条
  notice 显示，**仅监听本机回环地址**。
- 端口 `0` 由内核选空闲端口；token 每次进程启动重新生成（16 字节十六进制）。
- 会话退出（含 Ctrl+C 两次）时服务关闭：SSE 客户端被结束、定时器清理、
  `server.close()` + `closeAllConnections()`，不因一个开着的浏览器标签页而挂住退出。
- 面板起不来（端口占用等）只打印「仪表盘启动失败：…」，终端会话不受影响。

## 2. HTTP 端点

所有路由都要求 token；Host 头必须在允许列表内，否则 403。

| 方法 | 路径 | 作用 |
|------|------|------|
| GET | `/`、`/index.html` | 自包含 HTML（无 CDN、无构建产物；CSP `default-src 'none'`） |
| GET | `/api/state` | 当前完整状态快照（`rev` / `state` / `commandRunning` / `fileResults`） |
| GET | `/api/events` | SSE；每个事件是一份**完整状态快照** |
| GET | `/api/query?kind=…&params=<JSON>` | 只读查询帧；kind 必须在允许列表内 |
| POST | `/api/action` | 终端同款动作：`submit` / `command` / `approval` / `prompt` / `cancel` / `status` |

- `/api/query` 的 `params` 必须是 JSON 对象；未知 kind → 400。
  服务端最多等待 6 秒，前端 `SessionController` 的查询超时为 5 秒，
  超时/失败统一返回 `{ "error": "…" }` 而不是挂住页面。
- `/api/action` 请求体上限 64 KB（超限 413）；未知 action → 400。
  `command` 必须以 `/` 开头；`args` 与 `prompt.value` 可以省略或为空字符串
  （无参数命令 `/cost`、回车接受默认答案都是合法输入，不能只在终端成立）；
  `approval.choice` 只能是 `once` / `turn` / `allow` / `deny` / `cancel`。
- **提问期间的 `submit` 就是答案**：内核阻塞在 `prompt.request` 时（`/set_model`、
  `/setup`、`/skill create`），输入框提交的文本交给 `prompt.response`，不会开启
  新一轮；内核同时拒绝在命令线程存活时接受 `turn.start`，两侧都不会再出现
  「选择器卡住 + 数字被当成任务跑」。
- 不存在的 `/api/*` → 404；其他方法 → 405。
- SSE 并发连接上限 16；每 15 秒发一次 `: ping` 心跳；`retry: 3000`。

### 2.1 安全模型

1. **只监听回环地址**：`start()` 拒绝任何非 `127.0.0.1` / `localhost` / `::1` 的绑定。
2. **token**：URL `?t=` 或 `Authorization: Bearer`；比较用 `timingSafeEqual`。
   无 token → 401。
3. **Host 允许列表**：`127.0.0.1`、`localhost`、`::1`（含端口、IPv6 方括号形式），
   其他 Host 头 → 403。这是防 DNS rebinding 的第二道门：即使某个页面把域名解析到
   `127.0.0.1`，它带来的 Host 头也不会被接受。
4. **CSP / 响应头**：`default-src 'none'`（只允许内联样式/脚本与 `connect-src 'self'`）、
   `X-Frame-Options: DENY`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、
   `Cache-Control: no-store`。
5. **没有 shell/eval**：页面能做的只有终端本来就能做的四类动作。
6. `XENON_DASHBOARD=0` 一键关闭功能。

## 3. SSE 语义（`rev` 与断线恢复）

- 每次广播 `rev += 1`；事件为 `id: <rev>` + `event: state` + `data: <完整快照>`。
- **只有快照真的变化才广播**：查询应答（`<kind>.result`）与内容相同的 `status`
  应答不会推进 `rev`，也不会写 SSE 帧；侧栏刷新因此不会重绘主对话或让终端闪。
- `status` 的两个时钟（`elapsed_seconds` / `session_elapsed_seconds`）按状态栏
  实际显示的**整秒**存储：内核给的是 0.1 秒，照收会让每 500ms 一次的状态轮询
  都算「变化」，整屏随秒针抖动。
- **每个事件都是完整快照**，不发送增量。因此断线重连只有两种结果：
  需要当前快照（`Last-Event-ID` 缺失或更旧），或什么都不需要（已是最新）。
  不存在「丢了半条事件」或「重放导致重复渲染」。
- 首次连接**没有** `Last-Event-ID`，必须收到快照——早先把缺失当成 `0`，
  在 `rev == 0` 时会让新标签页空白。
- `Last-Event-ID >= rev` 时服务端只保持连接，不发历史。

## 4. 只读查询帧

查询是普通协议帧：请求 `{v, id, type: "<kind>", …params}`，宿主回
`{v, id, type: "<kind>.result", …payload}`（`id` 原样带回，前端按 id 配对）。
宿主对查询帧在独立 daemon 线程里执行，避免 git 等外部命令阻塞 stdin 主循环。

| kind | 数据来源 | 主要字段 |
|------|----------|----------|
| `dashboard.snapshot` | 最近一轮结果 + 宿主状态 | `cwd` `models` `mode` `turn{id,objective,running,elapsed_s}` `status` `ledger` `evidence{status,accepted}` `debt[{kind,reason,obligation_id,target,next_action,attempts,acceptable,user_accepted}]` `delivery{commit,verified_paths,unverified_paths,attributed_paths}` `approvals[]` `prompts[]` |
| `workspace.status` | `git status/rev-parse` + 会话工作记忆 | `is_git` `branch` `head` `dirty[{status,path}]` `dirty_count` `baseline[]` `baseline_count`（会话开始前就存在的改动） `session_created` `session_modified` `delivery` `ledger` |
| `diff.get` | `git status` + `git diff --no-ext-diff` + 未跟踪文件内容 | `files[{status,path,origin,verification}]` `diff` `baseline[]` `baseline_count` `delivery` `truncated` `ledger`（上限 200 KB / 100 文件；未跟踪文件最多内联 10 个） |
| `ledger.events` | L0 账本 JSONL | `events[{seq,kind,ts,source,hash,payload}]` `total` `chain{valid,reason,events,last_seq,last_hash,truncated_tail}` `ledger` |
| `evidence.chain` | 账本投影 | `facts[]` `verdicts[]` `claims[]` `delivered` `status` `projection` `chain` `ledger` |
| `plan.get` | 账本 `plan` 投影 + 会话事件里的用户决定 | `plan{plan,steps,versions}` `accepted` `proposed` `rejected` `current` `ledger` |
| `memory.list` | 会话记忆服务 | `items[]` `count` |
| `mcp.status` | MCP 注册表（只读） | `servers[]` `tools[]` |
| `report.get` | 会话费用统计 | `models[]` `totals`（无调用时 `error`） |
| `sessions.list` | 已保存会话索引 | `sessions[{name,saved_at,messages}]` |
| `sessions.tree` | 会话事件日志（`turn/user`、`fork`） | `nodes[{id,parent,turns,events,first_at,last_at}]` `roots[]`（最多 100 份日志） |

契约：

- **只读**。查询不执行工具、不改内核状态、不写盘。
- **按轮次取数**。`ledger.events` / `evidence.chain` / `plan.get` / `diff.get`
  读取的是**最近一次运行自己的账本**（响应里的 `ledger` 就是该文件路径），
  换一轮就换一个账本。所以“一次审批只记一条 `user_approval`”是**每个轮次账本
  内**的性质；把 `ledger.events` 的长度当成会话累计数会误判（E2E 曾因此误报
  “终端无法应答第二次审批”）。需要跨轮对比时，用每轮 `ledger` 路径区分。
- **形状稳定**：错误返回与正常返回携带同一组字段（`plan.get` 始终有
  `accepted/proposed/rejected/current/ledger`，`diff.get` 始终有 `baseline/delivery`，
  `evidence.chain` 始终有 `projection`），面板不需要按“是否有账本”分支。
- **计划同意来自用户决定，不是内核事件**：账本里的 `plan_accepted` 是运行时
  自己的标记（“已提出计划，停在 PLANNED 等待批准”），kernel 每次提出计划都会写。
  `accepted/proposed/rejected` 因此复用 `/plan` 的同一个解析器
  （`resolve_plan_approval`：会话事件 `plan/approved|rejected` 按 `task_id` 绑定，
  快照需 digest 匹配）。没有用户决定时：已交付/已结束的运行视为执行过，
  其余视为待批准。否则每个 plan-only 轮次都会在用户敲 `/plan approve` 之前
  被面板误报为“已批准”。
- 面板无法回答时（没有账本、不是 git 仓库、无模型调用）返回 `error` 字段，
  不抛异常、不结束会话；四类查询失败已在单测与真实入口中都验证过。
- `origin` 的判定来自契约而非关键词：路径在会话工作记忆的
  `session_created_files` → `created`，在 `session_modified_files` → `modified`，
  否则 → `baseline`。`verification` 来自交付账本的
  `finalize.verified_paths / attributed_paths`；baseline 且未归因的路径是
  `unverified`。
- 数字来自内核已有的真相：`evidence.status` 优先来自最近一轮结果；恢复会话
  （`/resume` 或启动 `--resume`）没有运行对象时，读账本旁的 `.result.json`——
  与终端 `/evidence` 用的是同一份事实，否则面板会对终端能看的证据报「未报告」。
  账本事件来自 `Ledger.read()`，哈希链来自 `ledger.verify()`，diff 来自 git
  本身。前端不自行推算证据或成本。

> git 子进程的 `stdin` 必须为 `DEVNULL`。宿主的 stdin 是 NDJSON 协议管道，
> 子进程继承它会让 git 一直等待输入，查询在 5 秒时超时；该问题在真实 ConPTY
> 入口复现并被回归测试锁定（`tests/frontend/test_dashboard_queries.py`）。

## 5. 面板契约（v1，九个面板）

面板全部只渲染事实；没有数据的区块显示「暂无」而不是猜测。

| # | 面板 | 内容 | 数据 |
|---|------|------|------|
| 1 | 概览 | 会话事实、最近一轮、证据角标、债务、审批/提问收件箱 | `dashboard.snapshot` + 实时帧 |
| 2 | 主交互 | 对话块（Markdown、证据角标、降级发布提示）、输入框、`/` 斜杠补全（↑/↓ + Tab/Esc）、实时轮次 | SSE + `submit` + `commands[]` |
| 3 | Diff | 改动文件列表（origin + verification 角标）、unified / 并排切换、baseline 污染区、截断提示、交付 commit | `diff.get` |
| 4 | 执行过程 | 瀑布条：逐事件耗时、>30s 停滞、连续同类事件标记重试、取消/中断、token 合计；与 `step_started`…`step_closed` 一一对应 | `ledger.events` + SSE |
| 5 | 证据链 | L0 哈希链校验结果、fact → verdict → claim → delivery、单条 JSON、投影 JSON | `evidence.chain` |
| 6 | 计划 | 步骤流（`depends_on` 依赖）、状态、当前步骤高亮、待批准/已批准/已驳回横幅、版本号 | `plan.get` |
| 7 | 审批 | 五档按钮（本次/本轮/30 分钟/拒绝/取消）与提问输入框；与终端互斥且幂等 | SSE + `approval`/`prompt` action |
| 8 | 成本 | 各模型调用数/token/命中率/费用 | `report.get` |
| 9 | 工作区 / 会话 | 分支、脏文件（来源角标）、baseline 污染、已核验 vs 归因 vs 未核验、交付 commit；会话 fork 树 + 已保存会话 + 记忆/MCP | `workspace.status` `sessions.tree` `sessions.list` `memory.list` `mcp.status` |

布局：窗口 ≥1400px 时右侧为四宫格（总览 / Diff / 执行过程 / 证据链，2.5s 节流刷新），
可点「▦ 宫格」切回；点击任一标签进入标签模式。窄屏退化为左侧单栏 + 标签页。

页面交互：`Tab` 切换面板、`j/k` 滚动、`?` 帮助、数字键直达、`#event=<seq>` 深链。
页面是单个自包含 HTML 字符串（内联样式与脚本），不依赖 CDN 或前端构建产物中的额外文件。

## 6. 双端一致性

- 终端与浏览器共用同一个 `SessionController`：`submit`、`runCommand`、
  `answerApproval`、`answerPrompt`、`cancelTurn` 走同一套状态机。
- 同一审批请求只有一个「第一次回答」有效；第二次回答（终端与浏览器抢答、
  浏览器双击）不产生第二个 `user_approval`，页面与终端都看到同一结果。
- 浏览器提交的任务在终端实时可见，反之亦然；取消同理。
- 终端删除审批卡的条件与页面一致：`turn.approval` 被内核清除，而非本地隐藏。

## 7. 测试与验收

| 层次 | 位置 | 覆盖 |
|------|------|------|
| Python 查询 | `tests/frontend/test_dashboard_queries.py` | 每个面板的 payload、错误返回的稳定形状、`origin/verification/baseline/delivery`、计划 flags、`sessions.tree`、哈希链、diff 与 git 一致、git 不继承 stdin、`/dashboard` 注册 |
| 页面脚本 | `frontend/src/dashboard/page.test.ts` | 在 VM 里执行内联脚本：布局/宫格、斜杠补全、并排 diff、瀑布停滞/重试、计划当前步与依赖、五档审批、baseline/交付、fork 树 |
| Node 服务 | `frontend/src/dashboard/server.test.ts` | 401/403/404/405/413、SSE 首包与更新、Last-Event-ID、query 转发、未知 kind、`XENON_DASHBOARD=0`、close 后端口不可达 |
| 会话控制 | `frontend/src/session.test.ts` | 查询按 id 配对、超时、`/dashboard` 前端命令、关闭时停服 |
| 真实入口 | `C:\Users\Administrator\.xenon-e2e-harness\dash_*.py` | ConPTY 起 `xenon` → `/dashboard` → HTTP/SSE 断言；浏览器提交任务；双端审批；`dash_gaps_e2e.py` 跑真实写文件任务并校验交付 commit 与 baseline 归因 |

真实 ConPTY 已验收：URL/token 生效、无 token 401、页面可打开、`dashboard.snapshot`
与 `workspace.status` 应答、SSE 首包、浏览器 `/status` 在终端可见、非法 action 400、
`XENON_DASHBOARD=0` 明确拒绝；浏览器提交真实任务并在终端渲染；终端审批卡与
浏览器共享同一 request id，浏览器回答后轮次完成，第二次回答无副作用。

**明确不做的事**：不在浏览器里执行任意脚本、不把账本或证据计算搬到前端、
不提供外网监听、不在页面里缓存 token 或授权。
