"""OmniAgent Textual TUI — 现代化终端界面。

基于 Textual 框架的交互式 Agent 前端，提供:
- 分栏对话 + 思考面板
- 实时事件流
- 权限审批弹窗
- C/S daemon 连接
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, ScrollableContainer, Vertical
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Static,
)
from textual.widget import Widget
from textual.binding import Binding

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Custom Widgets
# ═══════════════════════════════════════════════════════════════


class ThinkingPanel(Widget):
    """实时思考过程面板 — 显示 Agent 的思考-行动-观察循环。"""

    DEFAULT_CSS = """
    ThinkingPanel {
        height: auto;
        min-height: 3;
        border: solid $primary 30%;
        padding: 1;
        margin: 0 0 1 0;
        background: #0d1117;
    }
    ThinkingPanel > Static {
        width: 100%;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.steps: list[dict[str, str]] = []

    def compose(self) -> ComposeResult:
        yield Static("🧠 思考过程 — 等待中...", id="thinking-content")

    def add_thought(self, thought: str) -> None:
        """记录思考。"""
        self.steps.append({"type": "thought", "content": thought[:200]})
        self._refresh_display()

    def add_action(self, action: str, params: dict) -> None:
        """记录行动。"""
        params_str = ", ".join(f"{k}={repr(v)[:40]}" for k, v in list(params.items())[:3])
        self.steps.append({"type": "action", "content": f"{action}({params_str})"})
        self._refresh_display()

    def add_observation(self, obs: str) -> None:
        """记录观察。"""
        self.steps.append({"type": "observe", "content": obs[:150]})
        self._refresh_display()

    def clear(self) -> None:
        """清空。"""
        self.steps.clear()
        self._refresh_display()

    def _refresh_display(self) -> None:
        """刷新显示。"""
        if not self.steps:
            self.query_one("#thinking-content", Static).update("🧠 思考过程 — 等待中...")
            return

        lines = []
        for i, step in enumerate(self.steps[-10:]):
            icon = {"thought": "🤔", "action": "🔧", "observe": "👀"}.get(step["type"], "•")
            content = step["content"].replace("\n", " ")
            lines.append(f"{icon} [{i + 1}] {content}")

        self.query_one("#thinking-content", Static).update("\n".join(lines))


class StatusPanel(Widget):
    """状态面板 — 显示当前引擎状态、工具调用、子任务等。"""

    DEFAULT_CSS = """
    StatusPanel {
        height: auto;
        min-height: 3;
        border: solid $secondary 30%;
        padding: 1;
        background: #0a0e14;
    }
    """

    model: reactive[str] = reactive("—")
    mode: reactive[str] = reactive("—")
    tokens: reactive[int] = reactive(0)
    tool_count: reactive[int] = reactive(0)
    active_tasks: reactive[int] = reactive(0)

    def compose(self) -> ComposeResult:
        yield Static("📊 状态面板", id="status-content")

    def watch_model(self, model: str) -> None:
        self._refresh()

    def watch_mode(self, mode: str) -> None:
        self._refresh()

    def watch_tokens(self, tokens: int) -> None:
        self._refresh()

    def watch_tool_count(self, count: int) -> None:
        self._refresh()

    def watch_active_tasks(self, count: int) -> None:
        self._refresh()

    def _refresh(self) -> None:
        lines = [
            f"🤖 模型: {self.model}",
            f"📋 范式: {self.mode}",
            f"📊 Tokens: {self.tokens:,}",
            f"🔧 工具调用: {self.tool_count}",
            f"📦 子任务: {self.active_tasks}",
            f"🕐 {datetime.now().strftime('%H:%M:%S')}",
        ]
        try:
            self.query_one("#status-content", Static).update("\n".join(lines))
        except NoMatches:
            pass


class ConversationLog(RichLog):
    """对话日志 — 渲染 Rich Markdown 的滚动日志。"""

    DEFAULT_CSS = """
    ConversationLog {
        height: 1fr;
        border: solid dim;
    }
    """

    def add_user_message(self, text: str) -> None:
        """极氪风格：用户消息 — 青色标识。"""
        self.write(Text())  # 空行分隔
        self.write(Panel(
            text,
            title="▸ You",
            title_align="left",
            border_style="bright_cyan",
            padding=(0, 1),
        ))

    def add_assistant_message(self, text: str, model: str = "") -> None:
        """极氪风格：助手消息 — 绿色标识 + Markdown 渲染。"""
        title = "◆ Assistant" + (f"  [{model}]" if model else "")
        md = Markdown(text, code_theme="monokai")
        self.write(Panel(
            md,
            title=title,
            title_align="left",
            border_style="bright_green",
            padding=(1, 2),
        ))
        self.write(Text())  # 空行分隔

    def add_system_message(self, text: str) -> None:
        """极氪风格：系统消息 — 细线前缀。"""
        self.write(Text(f"▎ {text}", style="dim bright_cyan"))

    def add_error(self, text: str) -> None:
        """极氪风格：错误消息 — 红色强调。"""
        self.write(Text(f"✖ {text}", style="bold red"))


# ═══════════════════════════════════════════════════════════════
# Modal Screens
# ═══════════════════════════════════════════════════════════════


class PermissionModal(ModalScreen[bool]):
    """权限审批弹窗 — 交互式工具调用审批。"""

    DEFAULT_CSS = """
    PermissionModal {
        align: center middle;
    }
    PermissionModal > Container {
        width: 60;
        height: auto;
        border: thick $error;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(self, tool_name: str, params_preview: str, reason: str = "") -> None:
        super().__init__()
        self.tool_name = tool_name
        self.params_preview = params_preview
        self.reason = reason

    def compose(self) -> ComposeResult:
        with Container():
            yield Label(f"⚠️ 权限审批", classes="title")
            yield Label(f"工具: {self.tool_name}")
            yield Label(f"参数: {self.params_preview}")
            if self.reason:
                yield Label(f"原因: {self.reason}", classes="reason")
            yield Label("")
            with Horizontal():
                yield Button("✓ 允许 (一次)", variant="primary", id="allow-once")
                yield Button("✓ 始终允许", variant="success", id="allow-always")
                yield Button("✗ 拒绝 (一次)", variant="error", id="deny-once")
                yield Button("✗ 始终拒绝", variant="warning", id="deny-always")

    @on(Button.Pressed, "#allow-once")
    def allow_once(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#allow-always")
    def allow_always(self) -> None:
        self.dismiss(True)  # caller should check and set persistent

    @on(Button.Pressed, "#deny-once")
    def deny_once(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#deny-always")
    def deny_always(self) -> None:
        self.dismiss(False)  # caller should check and set persistent


class HelpModal(ModalScreen[None]):
    """帮助弹窗 — 显示快捷键和命令列表。"""

    DEFAULT_CSS = """
    HelpModal {
        align: center middle;
    }
    HelpModal > Container {
        width: 50;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        with Container():
            yield Label("📖 帮助 — OmniAgent TUI", classes="title")
            yield Label("")
            yield Label("快捷键:")
            yield Label("  Ctrl+Q     — 退出")
            yield Label("  Ctrl+P     — 切换思考范式")
            yield Label("  Ctrl+M     — 切换模型")
            yield Label("  Ctrl+C     — 清空对话")
            yield Label("  Ctrl+S     — 保存会话")
            yield Label("  /          — 斜杠命令模式")
            yield Label("  !          — 终端命令模式")
            yield Label("  Shift+Enter — 换行")
            yield Label("  Enter      — 发送")
            yield Label("")
            yield Label("斜杠命令:", classes="subtitle")
            yield Label("  /help      — 显示此帮助")
            yield Label("  /model     — 切换模型")
            yield Label("  /mode      — 切换范式")
            yield Label("  /compact   — 压缩上下文")
            yield Label("  /clear     — 清空对话")
            yield Label("  /save      — 保存会话")
            yield Label("  /tools     — 列出工具")
            yield Label("  /setup     — 配置向导")
            yield Label("")
            yield Button("关闭", variant="primary")

    @on(Button.Pressed)
    def close_help(self) -> None:
        self.dismiss()


# ═══════════════════════════════════════════════════════════════
# Main App
# ═══════════════════════════════════════════════════════════════


class OmniAgentTUI(App):
    """OmniAgent Textual TUI 主应用。

    支持两种模式:
    1. 独立模式: 内置引擎（直接调用 LLM + 工具）
    2. C/S 模式: 连接到 omniagent-core daemon

    快捷键:
        Ctrl+Q: 退出
        Ctrl+P: 切换范式
        Ctrl+M: 切换模型
        /: 斜杠命令
        !: 终端命令
    """

    TITLE = "OmniAgent CLI"
    SUB_TITLE = "多模型 AI 编程助手 · Zeekr Edition"

    CSS = """
    /* ═══════════════════════════════════════════════════════════
       OmniAgent CLI — 极氪风格主题
       深色基底 + 青色强调 + 微妙的边框层次
       ═══════════════════════════════════════════════════════════ */

    Screen {
        layout: vertical;
        background: #0a0e14;
    }

    #main-container {
        layout: horizontal;
        height: 1fr;
        margin: 0 0 1 0;
    }

    #conversation-container {
        width: 2fr;
        height: 100%;
        border-right: solid $primary 20%;
        background: #0d1117;
    }

    #side-panel {
        width: 1fr;
        height: 100%;
        background: #0a0e14;
        padding: 0 1 0 0;
    }

    #input-container {
        height: auto;
        min-height: 3;
        max-height: 10;
        border-top: double $primary 60%;
        padding: 0 1;
        margin: 0 1 0 1;
    }

    #input-area {
        height: auto;
        min-height: 3;
        max-height: 10;
    }

    /* ── Conversation Log ── */
    ConversationLog {
        height: 1fr;
        border: none;
        padding: 1;
        background: #0d1117;
    }

    /* ── Header / Footer ── */
    Header {
        background: #0a0e14;
        color: $accent;
        text-style: bold;
        dock: top;
    }

    Footer {
        background: #0a0e14;
        color: $text-muted;
        dock: bottom;
    }

    /* ── Labels & Status ── */
    .title {
        text-style: bold;
        color: $accent;
    }

    .subtitle {
        color: $secondary;
        text-style: italic;
    }

    .reason {
        color: $warning;
    }

    .highlight {
        color: $accent;
        text-style: bold;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "退出", show=True),
        Binding("ctrl+p", "switch_mode", "切换范式", show=True),
        Binding("ctrl+m", "switch_model", "切换模型", show=True),
        Binding("ctrl+c", "clear_chat", "清空", show=True),
        Binding("ctrl+h", "show_help", "帮助", show=True),
        Binding("ctrl+s", "save_session", "保存", show=True),
        Binding("escape", "focus_input", "输入", show=False),
        Binding("ctrl+j", "submit_input", "发送", show=True, priority=True),
    ]

    # ── Reactive 属性 ────────────────────────────────────────

    current_model: reactive[str] = reactive("—")
    current_mode: reactive[str] = reactive("direct")
    token_count: reactive[int] = reactive(0)
    tool_call_count: reactive[int] = reactive(0)
    active_subagent_count: reactive[int] = reactive(0)

    def __init__(
        self,
        *,
        model_priority: list[str] | None = None,
        mode: str = "direct",
        system_prompt: str | None = None,
        connect_daemon: bool = False,
        daemon_host: str = "127.0.0.1",
        daemon_port: int = 9501,
    ) -> None:
        super().__init__()
        self._model_priority = model_priority or ["deepseek/deepseek-v4-pro"]
        self._mode = mode
        self._system_prompt = system_prompt
        self._connect_daemon = connect_daemon
        self._daemon_host = daemon_host
        self._daemon_port = daemon_port

        # 引擎组件（延迟初始化）
        self._engine: Any = None
        self._event_bus: Any = None
        self._tool_registry: Any = None
        self._permissions: Any = None
        self._context: Any = None
        self._socket_client: Any = None

        # 对话消息
        self._messages: list[dict[str, str]] = []

        # 状态
        self.current_mode = mode
        if self._model_priority:
            self.current_model = self._model_priority[0]

    # ── Compose ──────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        """构建 UI 组件树。"""
        yield Header(show_clock=True)

        with Horizontal(id="main-container"):
            with ScrollableContainer(id="conversation-container"):
                yield ConversationLog(id="conversation-log")

            with Vertical(id="side-panel"):
                yield ThinkingPanel()
                yield StatusPanel()

        with Container(id="input-container"):
            yield Input(
                placeholder="▸ 输入消息…  Enter 发送  │  / 命令  │  ! 终端",
                id="input-area",
            )

        yield Footer()

    # ── Lifecycle ────────────────────────────────────────────

    def on_mount(self) -> None:
        """应用挂载时初始化。"""
        self._init_engine()
        self._update_status()

        # 极氪风格欢迎消息
        log = self.query_one("#conversation-log", ConversationLog)
        log.add_system_message(f"⚡ OmniAgent CLI v0.1.0  ·  {self.current_mode} 模式  ·  {self.current_model}")
        log.add_system_message("输入消息开始对话  │  /help 帮助  │  Ctrl+Q 退出")

    def _init_engine(self) -> None:
        """初始化引擎组件。"""
        try:
            from omniagent.engine.context import AgentContext
            self._context = AgentContext()

            # 尝试初始化 EventBus
            try:
                from omniagent.events.bus import EventBus
                self._event_bus = EventBus()
            except Exception:
                pass

            # 尝试初始化 ToolRegistry
            try:
                from omniagent.tools.registry import ToolRegistry
                self._tool_registry = ToolRegistry()

                # 注册基本工具
                from omniagent.tools.command import CommandTool
                from omniagent.tools.file_ops import (
                    ReadFileTool, WriteFileTool, EditFileTool,
                    CreateDirectoryTool, ListFilesTool,
                )
                self._tool_registry.register(CommandTool())
                self._tool_registry.register(ReadFileTool())
                self._tool_registry.register(WriteFileTool())
                self._tool_registry.register(EditFileTool())
                self._tool_registry.register(CreateDirectoryTool())
                self._tool_registry.register(ListFilesTool())
            except Exception:
                pass

            # 订阅 EventBus 事件
            if self._event_bus:
                self._setup_event_subscriptions()

        except Exception as e:
            logger.warning(f"引擎初始化部分失败: {e}")

    def _setup_event_subscriptions(self) -> None:
        """订阅 EventBus 事件并更新 UI。"""
        if not self._event_bus:
            return

        async def on_thought(event: Any) -> None:
            thinking = self.query_one(ThinkingPanel)
            thinking.add_thought(getattr(event, "thought", ""))

        async def on_tool_start(event: Any) -> None:
            thinking = self.query_one(ThinkingPanel)
            thinking.add_action(
                getattr(event, "tool_name", "?"),
                getattr(event, "params", {}),
            )
            self.tool_call_count += 1
            self._update_status()

        async def on_tool_finish(event: Any) -> None:
            thinking = self.query_one(ThinkingPanel)
            thinking.add_observation(getattr(event, "output", "")[:150])

        async def on_permission_request(event: Any) -> None:
            """弹出权限审批对话框。"""
            tool_name = getattr(event, "tool_name", "?")
            params_preview = getattr(event, "params_preview", "")
            reason = getattr(event, "reason", "")

            def show_modal() -> None:
                self.push_screen(
                    PermissionModal(tool_name, params_preview, reason),
                    lambda result: self._handle_permission_result(event, result),
                )

            self.call_from_thread(show_modal)

        async def on_error(event: Any) -> None:
            log = self.query_one("#conversation-log", ConversationLog)
            log.add_error(getattr(event, "error", "Unknown error"))

        try:
            self._event_bus.subscribe("agent.thought", on_thought)
            self._event_bus.subscribe("tool.call_started", on_tool_start)
            self._event_bus.subscribe("tool.call_finished", on_tool_finish)
            self._event_bus.subscribe("permission.request", on_permission_request)
            self._event_bus.subscribe("run.error", on_error)
        except Exception as e:
            logger.debug(f"EventBus 订阅失败: {e}")

    def _handle_permission_result(self, event: Any, result: bool) -> None:
        """处理权限审批结果。"""
        tool_use_id = getattr(event, "tool_use_id", "")
        if self._permissions and tool_use_id:
            decision = "allow_once" if result else "deny_once"
            self._permissions.respond(tool_use_id, decision)

    def _update_status(self) -> None:
        """更新状态面板。"""
        try:
            status = self.query_one(StatusPanel)
            status.model = self.current_model
            status.mode = self.current_mode
            status.tokens = self.token_count
            status.tool_count = self.tool_call_count
            status.active_tasks = self.active_subagent_count
        except NoMatches:
            pass

    # ── Input Handling ───────────────────────────────────────

    @on(Input.Changed, "#input-area")
    def on_input_change(self, event: Input.Changed) -> None:
        """输入区域变化时检查是否需要自动调整大小。"""
        pass  # 可扩展: 自动调整输入区域高度

    @on(Input.Submitted, "#input-area")
    async def on_input_submit(self, event: Input.Submitted) -> None:
        """处理用户输入提交。"""
        text = event.value.strip()
        if not text:
            return

        event.input.clear()

        # 斜杠命令
        if text.startswith("/"):
            self._handle_command(text)
            return

        # 终端命令
        if text.startswith("!") and not text.startswith("!="):
            await self._handle_shell(text[1:].strip())
            return

        # 普通对话
        await self._handle_chat(text)

    async def _handle_chat(self, user_input: str) -> None:
        """处理对话消息。"""
        log = self.query_one("#conversation-log", ConversationLog)

        # 显示用户消息
        log.add_user_message(user_input)
        self._messages.append({"role": "user", "content": user_input})

        # 构建引擎输入
        engine_input = user_input
        if self._system_prompt:
            engine_input = f"{self._system_prompt}\n\n{user_input}"

        # 运行引擎
        try:
            if self._connect_daemon:
                result = await self._run_via_daemon(engine_input)
            else:
                result = await self._run_standalone(engine_input)

            log.add_assistant_message(result, self.current_model)
            self._messages.append({"role": "assistant", "content": result})

        except Exception as e:
            log.add_error(f"引擎执行失败: {e}")
            logger.error(f"引擎执行失败: {e}", exc_info=True)

    async def _run_standalone(self, user_input: str) -> str:
        """独立模式: 使用内置引擎。"""
        from omniagent.engine.async_engine import AsyncReActEngine

        engine = AsyncReActEngine(
            model_priority=self._model_priority,
            max_iterations=10,
            event_bus=self._event_bus,
            tool_registry=self._tool_registry,
            permission_manager=self._permissions,
        )
        return await engine.run(user_input, self._context)

    async def _run_via_daemon(self, user_input: str) -> str:
        """C/S 模式: 通过 SocketClient 发送到 daemon。"""
        if not self._socket_client:
            from omniagent.core.transport.socket_client import SocketClient
            self._socket_client = SocketClient(self._daemon_host, self._daemon_port)
            await self._socket_client.connect()

        response = await self._socket_client.request(
            "agent.run",
            {"goal": user_input, "mode": self.current_mode, "models": self._model_priority},
        )
        return response.get("result", {}).get("text", str(response))

    async def _handle_shell(self, command: str) -> None:
        """处理终端命令。"""
        log = self.query_one("#conversation-log", ConversationLog)

        import subprocess
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True, timeout=30,
            )
            output = result.stdout or result.stderr
            log.add_system_message(f"$ {command}\n{output[:2000]}")
        except subprocess.TimeoutExpired:
            log.add_error(f"命令超时: {command}")
        except Exception as e:
            log.add_error(f"命令执行失败: {e}")

    def _handle_command(self, raw: str) -> None:
        """处理斜杠命令。"""
        from omniagent.repl.commands import dispatch_command, ExitSignal

        parts = raw.split(maxsplit=1)
        cmd_name = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        log = self.query_one("#conversation-log", ConversationLog)

        try:
            # 尝试使用现有命令系统
            from omniagent.repl.model_registry import ModelRegistry
            from omniagent.repl.context_manager import ContextManager

            registry = ModelRegistry()
            for m in self._model_priority:
                alias = m.split("/")[-1]
                registry.add_model(m, alias)

            session_state = {}

            try:
                output = dispatch_command(
                    cmd_name, args,
                    registry=registry,
                    ctx_mgr=ContextManager(),
                    session_state=session_state,
                )
                if output:
                    log.add_system_message(output)
            except ExitSignal:
                self.exit()
                return

            # 特殊命令处理
            if cmd_name == "/help":
                self.push_screen(HelpModal())

            elif cmd_name == "/clear":
                log.clear()
                self._messages.clear()
                thinking = self.query_one(ThinkingPanel)
                thinking.clear()

            elif cmd_name == "/model":
                log.add_system_message(f"当前模型: {self.current_model}")

            elif cmd_name == "/mode":
                log.add_system_message(f"当前范式: {self.current_mode}")

            elif cmd_name == "/quit" or cmd_name == "/exit":
                self.exit()

        except Exception as e:
            logger.debug(f"命令处理异常: {e}")

            # 内置命令回退
            if cmd_name == "/help":
                self.push_screen(HelpModal())
            elif cmd_name == "/clear":
                log.clear()
                self._messages.clear()
                thinking = self.query_one(ThinkingPanel)
                thinking.clear()
            elif cmd_name in ("/quit", "/exit", "/q"):
                self.exit()
            else:
                log.add_error(f"未知命令: {cmd_name}，输入 /help 查看帮助")

    # ── Actions ──────────────────────────────────────────────

    def action_switch_mode(self) -> None:
        """切换思考范式。"""
        modes = ["direct", "react", "plan-execute", "reflection", "plan-react", "react-reflection"]
        try:
            idx = modes.index(self.current_mode)
            self.current_mode = modes[(idx + 1) % len(modes)]
        except ValueError:
            self.current_mode = "react"

        self._update_status()
        log = self.query_one("#conversation-log", ConversationLog)
        log.add_system_message(f"已切换到: {self.current_mode}")

    def action_switch_model(self) -> None:
        """切换到下一个模型。"""
        if self._model_priority:
            current = self._model_priority[0]
            try:
                idx = self._model_priority.index(current)
                self._model_priority = self._model_priority[idx + 1:] + self._model_priority[:idx + 1]
            except ValueError:
                pass
            self.current_model = self._model_priority[0]
        self._update_status()
        log = self.query_one("#conversation-log", ConversationLog)
        log.add_system_message(f"已切换模型: {self.current_model}")

    def action_clear_chat(self) -> None:
        """清空对话。"""
        log = self.query_one("#conversation-log", ConversationLog)
        log.clear()
        self._messages.clear()
        thinking = self.query_one(ThinkingPanel)
        thinking.clear()
        self.token_count = 0
        self.tool_call_count = 0
        self._update_status()

    def action_show_help(self) -> None:
        """显示帮助。"""
        self.push_screen(HelpModal())

    def action_save_session(self) -> None:
        """保存当前会话。"""
        log = self.query_one("#conversation-log", ConversationLog)
        try:
            from pathlib import Path
            save_dir = Path(".omniagent/sessions")
            save_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = save_dir / f"tui-session-{ts}.md"
            with path.open("w", encoding="utf-8") as f:
                for msg in self._messages:
                    role = msg["role"].upper()
                    content = msg["content"]
                    f.write(f"## {role}\n\n{content}\n\n---\n\n")
            log.add_system_message(f"会话已保存到: {path}")
        except Exception as e:
            log.add_error(f"保存失败: {e}")

    def action_focus_input(self) -> None:
        """聚焦输入区域。"""
        try:
            self.query_one("#input-area", Input).focus()
        except NoMatches:
            pass


# ═══════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════


def start_tui(
    *,
    models: list[str] | None = None,
    mode: str = "direct",
    system_prompt: str | None = None,
    connect_daemon: bool = False,
    daemon_host: str = "127.0.0.1",
    daemon_port: int = 9501,
) -> None:
    """启动 Textual TUI。

    Args:
        models: 模型优先级列表
        mode: 初始思考范式
        system_prompt: 自定义系统提示词
        connect_daemon: 是否连接到 omniagent-core daemon
        daemon_host: daemon 地址
        daemon_port: daemon 端口
    """
    app = OmniAgentTUI(
        model_priority=models,
        mode=mode,
        system_prompt=system_prompt,
        connect_daemon=connect_daemon,
        daemon_host=daemon_host,
        daemon_port=daemon_port,
    )
    app.run()


# ── 在 pyproject.toml 中注册: ─────────────────────────────────
# [project.scripts]
# omniagent-tui = "omniagent.tui.app:start_tui"
