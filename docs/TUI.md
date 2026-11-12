# Xenon 交互界面说明

> 适用版本：Xenon v0.9.2+；2026-11-11 起交互界面由 TS（Ink）前端提供，Python 侧只保留一个显式选择的内置编辑器。

## 定位

交互层不改变底层的模型路由、循环 profile、工具执行管线或上下文管理方式。
`/mode` 选择的是同一个循环的一组参数（见 `docs/ARCHITECTURE.md`），不是另一个引擎。

界面有两条入口，共用同一套内核、账本与协议语义：

| 入口 | 何时使用 | 渲染 |
|------|----------|------|
| TS 前端（默认，唯一产品界面） | `xenon`；缺少 `node` 或前端构建产物（wheel 自带的 `xenon/frontend/assets/xenon-ui.js`，或源码检出的 `frontend/dist/cli.js`）时**直接报错并给出构建步骤** | Ink；NDJSON 协议，见 `docs/design/frontend-protocol.md` |
| 内置编辑器（显式 opt-in） | `xenon --ui python`、`XENON_UI=python`（开发/无编辑器环境） | rich 文本 + 原始终端编辑器 |

Python 侧不再有 prompt_toolkit 会话：没有固定底栏、补全菜单或命令面板。
**不会**在没有前端构建时自动降级到内置编辑器——那会在用户不知情的情况下换一套界面。

## TS 前端布局

```text
对话历史和模型回复

  💭 2 步 · 1 个工具

● ReAct 结果
  这里是最终回复。

───────────────────────────────────────────────
  ❯ 用户输入
───────────────────────────────────────────────

· deepseek/deepseek-v4-pro  ·  default  ·  running  ·  context 5.0%
```

审批、计划确认和命令内提问（`/model`、`/resume`、`/permissions`、`/setup`）都渲染为面板，
通过 `approval.request` / `prompt.request` 与内核往返，不直接读进程 stdin。

| 操作 | TS 前端 |
|------|---------|
| 命令面板 | 输入 `/`（前缀或缩写过滤，如 `/prm` → `/permissions`；`Tab`/`↑/↓` 选择，`Enter` 补全、再回车执行） |
| 文件路径补全 | 输入 `@`（`↑/↓` 选择，`Tab` 补全） |
| 循环切换模式 | `Shift+Tab`（走 `/mode next`，与内置编辑器同一条命令） |
| 取消每轮 / 退出 | `Ctrl+C` |
| 首次运行 | 无凭证时状态栏下方提示 `输入 /setup` |

## 内置编辑器（显式 opt-in）

内置编辑器是逐字节读取的原始终端实现，支持：

| 操作 | 快捷键 |
|------|--------|
| 发送输入 | `Enter` |
| 输入换行 | `Shift+Enter` 或 `Alt+Enter` |
| 上一条 / 下一条输入 | `↑` / `↓`（持久化在 `~/.xenon/input_history.txt`） |
| 展开/折叠上一次执行详情 | `Ctrl+O` |
| 循环切换推理模式 | `Shift+Tab` |
| 中断当前任务 | `Ctrl+C` |
| 退出 Xenon | 空闲时连续两次 `Ctrl+C` |

长粘贴会被折叠成 `[Pasted #N +K chars]` 占位符，提交时还原为完整文本；
多行请求作为**一条** user turn 进入上下文（保持 prompt cache 前缀稳定）。

每轮开始前打印一次状态行（模型、模式、上下文用量、缓存命中、费用、工具数、时长）。
它是快照，不是常驻底栏——这是内置编辑器唯一丢失的视觉能力。

## 终端标签活动状态

Xenon 使用与启动动画同源的方块跑者标识更新终端标签标题（三格方块位，
`█` 代表奔跑的小方块）。标题更新只发送 OSC 控制序列，不移动光标，也不会向对话正文写入可见字符：

| 状态 | 标签标题 | 动画 |
|------|----------|------|
| 等待输入 / 任务完成 | `█·· Xenon` | 静止 |
| 模型思考 / 工具或命令执行 | `█··` → `·█·` → `··█` | 4 FPS 循环 |
| 等待工具权限 | `☆ Xenon · 等待命令确认` | 静止 |
| 等待记忆授权 | `☆ Xenon · 等待记忆确认` | 静止 |

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
| 意图、模式切换和 HTTP 日志 | 辅助信息，dim 显示 |
| 工具调用与探索过程 | 默认只显示步数/工具数摘要，完整内容折叠（`Ctrl+O` / 详情面板） |
| API 与会话状态 | TS 前端固定在底栏；内置编辑器下每轮打印一次 |

`/cost`、`/models` 等结构化命令可以继续使用表格或面板；“无边框”的约束主要针对高频的模型回复、提示词和执行轨迹。

## 实现位置

- `frontend/src/`：TS 前端的 App/状态栏/面板与协议编解码。
- `xenon/frontend/host.py`：协议宿主；复用 REPL 的配置、会话、命令与内核接线。
- `xenon/repl/repl_input.py`：内置原始终端编辑器（显式 opt-in），含历史与粘贴折叠。
- `xenon/repl/status_bar.py`：状态行的数据与一行人读格式（`get_toolbar_text`）。
- `xenon/repl/terminal_activity.py`：终端标签状态机、动画线程和跨平台降级。
- `xenon/engine/callbacks.py`：执行轨迹采集、折叠摘要和敏感参数脱敏。
- `tests/test_terminal_pty_e2e.py`：真实 ConPTY 下的输入、粘贴与 `Ctrl+O` 回归测试。
