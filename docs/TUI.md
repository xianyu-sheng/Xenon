# Xenon 交互界面说明

> 适用版本：Xenon v0.9.2+；2026-11-11 起交互界面由 TS（Ink）前端提供，Python 侧只保留一个显式选择的内置编辑器。

## 定位

交互层不改变底层的模型路由、循环 profile、工具执行管线或上下文管理方式。
`/mode` 只回答"当前循环"（Plan → React → Reflect 的固定阶段）与它的运行规则；
阶段是内核固定的，没有可切换的"模式"（见 `docs/ARCHITECTURE.md` §4）。

界面有两条入口，共用同一套内核、账本与协议语义：

| 入口 | 何时使用 | 渲染 |
|------|----------|------|
| TS 前端（默认，唯一产品界面） | `xenon`；缺少 `node` 或前端构建产物（wheel 自带的 `xenon/frontend/assets/xenon-ui.js`，或源码检出的 `frontend/dist/cli.js`）时**直接报错并给出构建步骤** | Ink；NDJSON 协议，见 `docs/design/frontend-protocol.md` |
| 内置编辑器（显式 opt-in） | `xenon --ui python`、`XENON_UI=python`（开发/无编辑器环境） | rich 文本 + 原始终端编辑器 |

Python 侧不再有 prompt_toolkit 会话：没有固定底栏、补全菜单或命令面板。
**不会**在没有前端构建时自动降级到内置编辑器——那会在用户不知情的情况下换一套界面。

## TS 前端布局

启动时先播放跑者动画（3 秒、12 FPS，任意按键跳过；按键本身仍照常进入输入，
不会因为跳过动画而丢失），随后落到常驻标识、欢迎卡片与启动摘要：

```text
        ▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄
             ···█  Xenon  ·  D:\proj
        ╭──────────────────────────────────────────╮
        │  Xenon · v1.1.0                          │
        │  让每一步都留下证据。                     │
        │  LOOP   单循环 · Plan → React → Reflect  │
        │  MODEL  deepseek/deepseek-v4-flash …     │
        ╰──────────────────────────────────────────╯
        · 模型按需加载，首次调用时再探测 provider
```

轮次运行中，输入区上方是实时状态（spinner、阶段、步数、工具数、秒表、
吞吐），底栏显示身份、token 条与缓存/费用：

```text
对话历史和模型回复

  💭 3 步 · 2 个工具 · 证据 partial

● Assistant
  这里是最终回复（Markdown：代码块、标题、列表）。

  ❯ 修一下 calc.py
  ⠋ 运行中 · thinking · 2 步 · 1 个工具 · 12s · 830 字

· deepseek/deepseek-v4-flash · direct · auto-edit · D:\proj
  ██████░░░░ 61.2K/128K   缓存 42%   费用 ¥0.0034
```

轮次结束后，结果块保留：`💭 N 步 · N 个工具 · 证据` 摘要、Markdown 答案、
`⚠️ 待人工核验清单`（内核 `debt`）、账本路径；`Ctrl+O` 展开完整执行详情。
退出时内核发一次 `session.report`，前端把它写成「本次会话省钱报告」。

审批、计划确认和命令内提问（`/model`、`/resume`、`/permissions`、`/setup`）都渲染为面板，
通过 `approval.request` / `prompt.request` 与内核往返，不直接读进程 stdin。
审批卡直接按数字键（`1`–`5`）即可作答，不需要回车；卡片本身会把这句话印出来。

### 审批只问"批准了能跑"的调用

权限策略回答的是"这一轮能不能做"；工具边界回答的是"就算人点了同意，这条命令
会不会被工具自己拦下"。两者不同，混在一起就会给出**买不到东西的同意**：实测一轮
只读计数，用户为 `python -c ...` 批准了升级，回执却是"被宿主安全过滤拦截"，
模型随后重发两次，第 3 次拒绝触发上限、整轮 `blocked` 且答案为空。

现在升级请求在弹卡前先过 `LegacyToolGateway.preflight()`（与工具同一套规则集）：
工具必然拒绝的调用不会打扰用户，而是返回一条**可继续**的拒绝，并点名可运行的
替代写法（`run_script` / 先写临时文件等），循环据此改道。被拒绝的调用仍然
永远不会执行——"不打扰"不等于"放行"。

| 操作 | TS 前端 |
|------|---------|
| 命令面板 | 输入 `/`（前缀或缩写过滤，如 `/prm` → `/permissions`；`Tab`/`↑/↓` 选择，`Enter` 补全、再回车执行） |
| 文件路径补全 | 输入 `@`（`↑/↓` 选择，`Tab` 补全） |
| 展开 / 折叠执行详情 | `Ctrl+O`（时间线与历史结果块同步生效） |
| 查看循环说明 | `Shift+Tab`（走 `/mode`，与内置编辑器同一条命令；没有切换语义） |
| 取消每轮 / 退出 | `Ctrl+C` |
| 首次运行 | 无凭证时欢迎卡片提示 `输入 /setup` |
| 本地 Web 面板 | `/dashboard`（终端打印地址，浏览器打开）；也可用 `xenon --dashboard` 随会话启动 |

## 本地可视化面板（Dashboard）

`/dashboard` 或 `xenon --dashboard` 会在同一个进程内起一个只监听 `127.0.0.1` 的
Web 面板，终端打印带随机 token 的地址（`http://127.0.0.1:<端口>/?t=<token>`）。
面板与终端共享同一个会话：两边都能输入、运行斜杠命令、回答审批和提问，
谁先动作谁生效，另一侧实时同步；同一审批的第二次回答不会产生第二个授权。

面板提供概览、主交互、文件 Diff、执行过程、证据链、计划、审批、成本、
工作区/会话九个视图，数据全部来自会话已有的帧与只读查询帧。
`XENON_DASHBOARD=0` 可关闭该功能（`/dashboard` 会明确报错，终端不受影响）；
`--ui python` 的内置编辑器界面不支持面板。架构与安全模型见 `docs/design/dashboard.md`。

## 内置编辑器（显式 opt-in）

内置编辑器是逐字节读取的原始终端实现，支持：

| 操作 | 快捷键 |
|------|--------|
| 发送输入 | `Enter` |
| 输入换行 | `Shift+Enter` 或 `Alt+Enter` |
| 上一条 / 下一条输入 | `↑` / `↓`（持久化在 `~/.xenon/input_history.txt`） |
| 展开/折叠上一次执行详情 | `Ctrl+O` |
| 查看循环说明 | `Shift+Tab` |
| 中断当前任务 | `Ctrl+C` |
| 退出 Xenon | 空闲时连续两次 `Ctrl+C` |

长粘贴会被折叠成 `[Pasted #N +K chars]` 占位符，提交时还原为完整文本；
多行请求作为**一条** user turn 进入上下文（保持 prompt cache 前缀稳定）。

每轮开始前打印一次状态行（模型、循环、上下文用量、缓存命中、费用、工具数、时长）。
它是快照，不是常驻底栏——这是内置编辑器唯一丢失的视觉能力。

## 终端标签活动状态

Xenon 使用与启动动画同源的方块跑者标识更新终端标签标题（三格方块位，
`█` 代表奔跑的小方块）。标题更新只发送 OSC 控制序列（Windows 走
`process.title` → `SetConsoleTitleW`），不移动光标，也不会向对话正文写入可见字符。

**TS 入口下这段实现位于 `frontend/src/terminalTitle.ts`**：协议宿主的 stdout
被 NDJSON 占用，内核不能再写转义序列。内置编辑器入口仍由
`xenon/repl/terminal_activity.py` 提供同样行为。

| 状态 | 标签标题 | 动画 |
|------|----------|------|
| 等待输入 / 任务完成 | `█·· Xenon` | 静止 |
| 模型思考 / 工具或命令执行 | `█··` → `·█·` → `··█` → `·░█` → `░█·` | 4 FPS 循环 |
| 等待审批 | `☆ Xenon · 等待确认` | 静止 |
| 命令内提问 | `☆ Xenon · 等待输入` | 静止 |
| 出错 / 已中断 | `█ Xenon · 出错` / `☆ Xenon · 已中断` | 静止 |

默认仅在交互式 TTY 中启用；CI、`TERM=dumb` 和重定向输出会自动关闭。
可用 `XENON_TERMINAL_ANIMATION=0` 手动关闭，用
`XENON_TERMINAL_ASCII=1` 切换为 `*..` / `.*.` / `..*` ASCII 帧。
Windows 会恢复启动前的窗口标题；Unix 终端无法可靠读取旧标题，退出时会使用
当前目录和 Shell 名作为安全回退，之后通常会被 Shell 自己的提示符再次更新。

## 视觉层级

| 内容 | 显示方式 |
|------|----------|
| 最终回复 | 无大边框，正常亮度，以 `● Assistant` 或引擎结果名作为轻量锚点 |
| 优化后的 Prompt | 无边框、缩进排版、整体 dim，不与回复争夺注意力 |
| 意图、路由说明和 HTTP 日志 | 辅助信息，dim 显示 |
| 工具调用与探索过程 | 默认只显示步数/工具数摘要，完整内容折叠（`Ctrl+O` / 详情面板） |
| API 与会话状态 | TS 前端固定在底栏；内置编辑器下每轮打印一次 |

`/cost`、`/models` 等结构化命令可以继续使用表格或面板；“无边框”的约束主要针对高频的模型回复、提示词和执行轨迹。

## 实现位置

- `frontend/src/`：TS 前端的 App/状态栏/面板与协议编解码。
  - `brand.ts` + 生成的 `brand-frames.ts`：启动跑者动画（帧与 Python 播放器同源，
    由 `scripts/brand` 一次生成两份产物）。
  - `terminalTitle.ts`：终端标签状态机（OSC / `process.title`）。
  - `markdown.ts`：答案面板的最小 Markdown 解析（代码块、标题、列表、行内样式）。
- `xenon/frontend/host.py`：协议宿主；复用 REPL 的配置、会话、命令与内核接线，
  并负责 `ready` / `status` / `turn.end` / `session.report` 的事实投影。
- `xenon/repl/repl_input.py`：内置原始终端编辑器（显式 opt-in），含历史与粘贴折叠。
- `xenon/repl/status_bar.py`：状态行的数据与一行人读格式（`get_toolbar_text`）。
- `xenon/repl/terminal_activity.py`：终端标签状态机、动画线程和跨平台降级。
- `xenon/engine/callbacks.py`：执行轨迹采集、折叠摘要和敏感参数脱敏。
- `tests/test_terminal_pty_e2e.py`：真实 ConPTY 下的输入、粘贴与 `Ctrl+O` 回归测试。
