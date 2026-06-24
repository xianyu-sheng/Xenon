"""
REPL — 交互式命令行主循环。

提供类似 Claude Code 的交互体验：
- 直接输入文本进入多轮对话
- /command 执行斜杠命令
- 支持模型切换、范式切换、会话管理
- 底部状态栏实时显示上下文用量
- 输入指令自动重构为结构化 prompt
"""

from __future__ import annotations

import logging
import re
import signal
import sys
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.theme import Theme
from rich.rule import Rule

from omniagent.engine.context import AgentContext
from omniagent.engine.run_recorder import RecordingCallback, RunRecorder
from omniagent.repl.cards import (
    TOOL_ICONS,
    ApprovalCard,
    ModeHeader,
    ToolCallCard,
    render_shortcut_bar,
)
from omniagent.repl.commands import COMMANDS, dispatch_command
from omniagent.repl.context_manager import ContextManager
from omniagent.repl.file_links import linkify_file_paths
from omniagent.repl.model_registry import ModelRegistry
from omniagent.repl.output_renderer import OutputRenderer
from omniagent.repl.project_context import ProjectContext
from omniagent.repl.prompt_optimizer import get_intent_display, optimize_prompt
from omniagent.repl.shell_runner import format_shell_result, run_shell_command
from omniagent.repl.status_bar import StatusBar
from omniagent.repl.session import RuntimeSession, RuntimeSessionStore

# ── 自定义主题 ────────────────────────────────────────────
_theme = Theme({
    "user": "bold cyan",
    "assistant": "green",
    "system": "dim yellow",
    "error": "bold red",
    "command": "bold magenta",
})

console = Console(theme=_theme)


class REPL:
    """
    交互式 REPL 主循环。

    支持两种输入模式：
    1. 以 / 开头 -> 斜杠命令
    2. 其他文本 -> 发送给当前模型进行多轮对话（自动优化 prompt）
    """

    def __init__(
        self,
        registry: ModelRegistry | None = None,
        ctx_mgr: ContextManager | None = None,
        system_prompt: str | None = None,
        *,
        streaming: bool = True,
        optimize_prompts: bool = True,
        verbose: bool = False,
    ) -> None:
        self.registry = registry or ModelRegistry()
        self.ctx_mgr = ctx_mgr or ContextManager()
        self.agent_context = AgentContext()
        self.system_prompt = system_prompt or self._default_system_prompt()
        self.streaming = streaming
        self.optimize_prompts = optimize_prompts
        self.verbose = verbose

        # 初始化工具图标（从统一来源）
        if not REPL._TOOL_ICONS:
            REPL._TOOL_ICONS = dict(TOOL_ICONS)

        # 项目上下文
        self.project_ctx = ProjectContext()
        self._project_injected = False

        # 多小说管理器
        from omniagent.engine.novel_manager import NovelManager
        self._novel_manager = NovelManager()

        # 状态栏
        self.status_bar = StatusBar(console, self.ctx_mgr, self.registry)
        self._prompt_session = None
        self._current_run_recorder: RunRecorder | None = None
        self.session_store = RuntimeSessionStore()
        self.runtime_session: RuntimeSession = self.session_store.create(title="OmniAgent interactive session")

        # 权限审批缓存 (session 级)
        self._approval_cache: dict[str, bool] = {}

        # ── 中断机制（Esc / Ctrl+C 中断任务执行）──
        self._interrupt_event = threading.Event()
        self._task_running = False
        self._setup_interrupt_handler()

        # 设置交互式审批处理器
        from omniagent.nodes.tool_node import ToolNode
        ToolNode.set_approval_handler(self._approval_handler)

        # 会话状态，供命令处理器共享
        self._session_state: dict[str, Any] = {
            "agent_context": self.agent_context,
            "_repl": self,
            "_novel_manager": self._novel_manager,
            "_runtime_session": self.runtime_session,
            "_session_store": self.session_store,
        }

    def _make_callback(self):
        """根据 verbose 状态创建引擎回调。"""
        from omniagent.engine.callbacks import ConsoleCallback
        callback = ConsoleCallback(verbose=self.verbose)
        recorder = self._current_run_recorder
        if recorder is None:
            return callback
        return RecordingCallback(callback, recorder)

    def _start_run(self, user_input: str, mode_name: str, model_ids: list[str], *, optimized: str | None = None, system_hint: str | None = None, was_optimized: bool | None = None, intent: str | None = None) -> RunRecorder:
        """为一次用户输入创建 run 记录器。"""
        recorder = RunRecorder(
            goal=user_input,
            mode=mode_name,
            model_ids=model_ids,
            root=self.runtime_session.runs_dir,
            session_id=self.runtime_session.id,
        )
        self._current_run_recorder = recorder
        self._session_state["_run_recorder"] = recorder
        recorder.start()
        recorder.emit(
            "chat.received",
            raw_input=user_input,
            optimized_input=optimized or user_input,
            optimized=bool(was_optimized),
            intent=intent,
            system_hint=system_hint,
        )
        return recorder

    def _finish_run(self, *, status: str, result: str = "", reason: str | None = None) -> None:
        """结束当前 run。"""
        recorder = self._current_run_recorder
        if recorder is not None and not recorder.is_finished:
            recorder.finish(status=status, result=result, reason=reason)
        self._current_run_recorder = None
        self._session_state.pop("_run_recorder", None)

    def _append_thread_message(
        self,
        role: str,
        content: str,
        *,
        run_id: str | None = None,
        model_used: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Persist a chat message to the current runtime session thread."""
        try:
            self.session_store.append_message(
                self.runtime_session.id,
                role=role,
                content=content,
                run_id=run_id,
                model_used=model_used,
                metadata=metadata,
            )
            self.runtime_session = self.session_store.get(self.runtime_session.id)
            self._session_state["_runtime_session"] = self.runtime_session
        except Exception as e:
            logger.debug("failed to append runtime session thread: %s", e)

    def _render_engine_result(self, callback, result: str, title: str, border_style: str = "green") -> None:
        """渲染引擎结果 — Claude Code 风格：答案优先 + 思考折叠。"""
        renderer = OutputRenderer(verbose=self.verbose)
        panel = callback.get_thinking_panel() if hasattr(callback, 'get_thinking_panel') else None
        renderer.render_answer(result, panel, title=title, border_style=border_style)

    @staticmethod
    def _default_system_prompt() -> str:
        from datetime import datetime
        now = datetime.now()
        weekdays_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        current_date = f"{now.year}年{now.month}月{now.day}日 {weekdays_cn[now.weekday()]}"
        return (
            "你是 OmniAgent-CLI，一个强大、直接的 AI 编程助手。\n"
            "你的知识涵盖编程语言、算法、系统设计、调试、DevOps 等领域。\n"
            "你对代码和概念问题的回答精确、结构化，直接切入要点。\n"
            f"当前日期: {current_date}。\n"
            "\n"
            "## 回答风格\n"
            "- 用中文回复，代码标识符保留英文。\n"
            "- 代码用 Markdown 代码块包裹，标注语言。\n"
            "- 先给核心答案，再展开细节——不啰嗦铺垫。\n"
            "- 遇到你知识范围内的问题，直接给出完整方案。\n"
            "- 如果被问「你是什么模型」或类似身份问题：根据系统注入的模型信息如实回答，"
            "例如「当前由 DeepSeek V4 Pro 驱动」。不要编造模型名——使用系统提供的信息。\n"
            "\n"
            "## 工具能力\n"
            "你具备文件读写、命令执行、网页搜索等工具能力。"
            "当前为默认模式——你可以直接用自身知识回答绝大多数问题。"
            "如果某个任务确实需要上述工具才能完成，"
            "在回复最开头单独一行写 [REQUIRES_TOOLS] 即可自动获得工具支持。"
            "不要滥用此标记——仅在确实需要时使用。"
        )

    @staticmethod
    def _set_console_title() -> None:
        """设置控制台窗口标题。"""
        import sys
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.kernel32.SetConsoleTitleW("🚀 OmniAgent-CLI")
            except Exception:
                pass
        else:
            # Linux/macOS 用 ANSI 转义
            sys.stdout.write("\033]0;🚀 OmniAgent-CLI\007")
            sys.stdout.flush()

    def _setup_interrupt_handler(self) -> None:
        """设置中断信号处理器和 SIGINT handler。

        - Ctrl+C (SIGINT) 设置中断事件，允许正在运行的任务优雅退出
        - 连续两次 Ctrl+C 强制终止
        """
        self._sigint_count = 0

        def _sigint_handler(signum, frame):
            self._sigint_count += 1
            if self._sigint_count >= 2:
                # 连续两次 Ctrl+C → 强制退出
                console.print("\n[red]⚠ 强制退出[/red]")
                sys.exit(1)
            if self._task_running:
                self._interrupt_event.set()
                console.print("\n[yellow]⏸  正在中断当前任务... (再按一次 Ctrl+C 强制退出)[/yellow]")
            else:
                # 没有任务运行 → 正常退出
                raise KeyboardInterrupt

        # 在 Windows 上，signal 只能在主线程工作
        try:
            signal.signal(signal.SIGINT, _sigint_handler)
        except (ValueError, OSError):
            # 非主线程或受限环境 → 回退到默认 KeyboardInterrupt
            pass

    def _shutdown(self) -> None:
        """安全关闭 REPL — 防止终端闪退。

        在退出前暂停，让用户看到告别信息，而不是终端立即关闭。
        """
        console.print()
        console.print("[yellow]再见！[/yellow]")
        console.print("[dim]按 Enter 退出...[/dim]", end="")

        try:
            # Windows: 用 msvcrt 读取一个按键（不用回车）
            if sys.platform == "win32":
                import msvcrt
                msvcrt.getch()
            else:
                input()
        except (KeyboardInterrupt, EOFError):
            pass

        console.print()

    def run(self) -> None:
        """启动 REPL 主循环。"""
        self._set_console_title()
        self._print_welcome()

        # 检查是否需要初始配置
        self._check_first_run()

        # 初始化系统消息
        if self.system_prompt:
            self.ctx_mgr.add_system_message(self.system_prompt)

        while True:
            # 显示状态栏
            self.status_bar.print_status()

            try:
                user_input = self._read_input()
            except (KeyboardInterrupt, EOFError):
                self._shutdown()
                break

            if not user_input:
                continue

            # ── Esc 键中断正在运行的任务 ──
            if user_input == "\x1b":
                if self._task_running:
                    self._interrupt_event.set()
                    console.print("\n[yellow]⏸  中断信号已发送，正在停止当前任务...[/yellow]")
                    continue
                else:
                    # 没有任务运行，Esc 无操作
                    continue

            # 斜杠命令
            if user_input.startswith("/"):
                if self._handle_command(user_input):
                    self._shutdown()
                    break
                continue

            # 直接运行终端命令：!python -V / !pytest tests -q
            if self._is_shell_input(user_input):
                self._handle_shell_input(self._extract_shell_command(user_input))
                continue

            # 多轮对话（带 prompt 优化）
            self._handle_chat(user_input)

    def _check_first_run(self) -> None:
        """首次启动时检测配置状态，自动引导。"""
        from omniagent.repl.provider_registry import get_configured_providers, load_credentials

        creds = load_credentials()
        configured = get_configured_providers()

        if not creds:
            # 完全没有配置 — 引导用户
            console.print("[yellow]检测到尚未配置任何 API Key[/yellow]")
            console.print("  输入 [bold cyan]/setup[/bold cyan] 进入配置向导\n")
        elif not self.registry.list_models():
            # 有 Key 但没选模型 — 自动注册已配置厂商的模型
            for p in configured:
                if p.models:
                    model_id = f"{p.key}/{p.models[0]}"
                    alias = p.models[0].replace(".", "-")
                    self.registry.add_model(model_id, alias, base_url=p.base_url)
                    if "planner" not in self.registry.role_priority:
                        self.registry.role_priority["planner"] = []
                    self.registry.role_priority["planner"].append(alias)

            if self.registry.list_models():
                models = self.registry.list_models()
                console.print(f"[green]已自动加载 {len(models)} 个模型[/green]")
                console.print("  输入 [bold cyan]/model[/bold cyan] 可切换模型\n")

        # 加载自定义快捷指令和技能
        self._load_custom_commands()

        # 自动清理过期会话数据
        self._auto_cleanup()

    def _print_welcome(self) -> None:
        """极氪风格欢迎界面 — 现代、简洁、高辨识度。"""
        import random

        mode = self.registry.get_current_mode()
        models = self.registry.list_models()

        # ── 渐变标题（纯 Rich markup，避免 Text 对象在 f-string 中丢失样式）──
        title_text = (
            "[bold bright_cyan]▲[/bold bright_cyan] "
            "[bold cyan]O M N I A G E N T[/bold cyan] "
            "[bold bright_cyan]C L I[/bold bright_cyan]"
        )

        # ── 版本标签 ──
        version = "v0.1.0"

        # ── 模型状态指示灯 ──
        if models:
            model_chips = []
            for m in models[:3]:
                chip = f"[on cyan] {m.alias} [/on cyan]"
                model_chips.append(chip)
            if len(models) > 3:
                model_chips.append(f"[dim]+{len(models)-3}[/dim]")
            model_line = "  ".join(model_chips)
        else:
            model_line = "[dim]未配置 — 输入 [bold cyan]/model[/bold cyan] 浏览添加模型[/dim]"

        # ── 随机提示 ──
        tips = [
            "输入 [bold cyan]/help[/bold cyan] 查看所有可用命令",
            "输入 [bold cyan]/model[/bold cyan] 管理 AI 模型（注册+切换一步完成）",
            "输入 [bold cyan]/mode[/bold cyan] 切换思考范式",
            "输入 [bold cyan]/setup[/bold cyan] 运行首次配置向导",
            "按 [bold cyan]Shift+Enter[/bold cyan] 可以输入多行内容",
            "输入 [bold cyan]!pytest tests -q[/bold cyan] 可直接运行终端命令",
            "输入 [bold cyan]/new_terminal[/bold cyan] 打开可观测子终端",
            "输入 [bold cyan]/verbose[/bold cyan] 开启详细日志模式",
            "输入 [bold cyan]/tools[/bold cyan] 查看所有可用工具",
            "输入 [bold cyan]/mcp[/bold cyan] 管理 MCP 扩展服务器",
        ]
        tip = random.choice(tips)

        # ── 构建欢迎面板 ──
        content = f"""{title_text}

  [dim]{version}[/dim]  [bold white]Multi-Model AI Coding Assistant[/bold white]

  [bold cyan]◆[/bold cyan] [bold]范式[/bold]  {mode.name}  [dim]— {mode.description}[/dim]
  [bold cyan]◆[/bold cyan] [bold]模型[/bold]  {model_line}

  [bold bright_cyan]▶[/bold bright_cyan]  [dim]{tip}[/dim]

  [dim]Ctrl+C 退出 · Shift+Enter 换行 · Enter 发送[/dim]"""

        console.print()
        console.print(Panel(
            content,
            border_style="bright_cyan",
            padding=(1, 2),
            subtitle="[dim]Powered by Rich[/dim]",
            subtitle_align="right",
        ))
        console.print()

    @staticmethod
    def _is_shell_input(text: str) -> bool:
        """Return True when the input should be treated as a shell command."""

        stripped = text.strip()
        return len(stripped) > 1 and stripped.startswith("!") and not stripped.startswith("!=")

    @staticmethod
    def _extract_shell_command(text: str) -> str:
        return text.strip()[1:].strip()

    def _handle_shell_input(self, command: str) -> None:
        """Run a shell command from the main OmniAgent input line."""

        result = run_shell_command(command, context=self.agent_context)
        rendered = linkify_file_paths(format_shell_result(result))
        self._session_state["_last_shell_result"] = result
        self.agent_context.set("_last_shell_command", result.command)
        self.agent_context.set("_last_shell_output", result.combined_output)
        self.ctx_mgr.add_user_message(f"[shell]\n$ {result.command}")
        self.ctx_mgr.add_assistant_message(
            f"Shell command {'succeeded' if result.success else 'failed'}.\n"
            f"Exit code: {result.returncode if result.returncode is not None else '-'}\n"
            f"{result.combined_output}"
        )
        border = "green" if result.success else "red"
        console.print(Panel(rendered, title="[command]shell[/command]", border_style=border))

    def _read_input_prompt_toolkit(self) -> str:
        """Read input with prompt_toolkit for robust cursor movement and editing."""

        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import InMemoryHistory
        from prompt_toolkit.styles import Style

        if self._prompt_session is None:
            self._prompt_session = PromptSession(
                history=InMemoryHistory(),
                key_bindings=self._prompt_key_bindings(),
                multiline=True,
                mouse_support=True,
                prompt_continuation=[("class:prompt", "..."), ("", " ")],
                wrap_lines=True,
                style=Style.from_dict({"prompt": "bold cyan"}),
            )

        return self._prompt_session.prompt([("class:prompt", "You"), ("", ": ")]).strip("\r\n")

    @staticmethod
    def _prompt_key_bindings():
        """Prompt key bindings: Enter submits, Shift+Enter inserts a new line."""

        from prompt_toolkit.filters import in_paste_mode
        from prompt_toolkit.key_binding import KeyBindings

        kb = KeyBindings()

        @kb.add("enter", filter=in_paste_mode)
        def _(event):
            event.current_buffer.insert_text("\n")

        @kb.add("enter", filter=~in_paste_mode)
        def _(event):
            event.current_buffer.validate_and_handle()

        def insert_newline(event):
            event.current_buffer.insert_text("\n")

        try:
            kb.add("s-enter")(insert_newline)
        except ValueError:
            pass

        @kb.add("escape", "enter")
        def _(event):
            event.current_buffer.insert_text("\n")

        return kb

    @staticmethod
    def _char_display_width(ch: str) -> int:
        """Return the terminal column width for one character."""

        import unicodedata

        if not ch:
            return 0
        if ch == "\t":
            return 4
        if unicodedata.combining(ch):
            return 0
        return 2 if unicodedata.east_asian_width(ch) in {"F", "W"} else 1

    @classmethod
    def _text_display_width(cls, text: str) -> int:
        """Return terminal column width, counting CJK full-width chars as two."""

        return sum(cls._char_display_width(ch) for ch in text)

    def _read_input(self) -> str:
        """读取用户输入。Shift+Enter 换行，Enter 发送。

        Windows: msvcrt.getwch() + GetAsyncKeyState 检测 Shift，
                 Console API 操作光标（兼容所有 Windows 终端）。
        POSIX:   input() 回退（不支持 Shift+Enter）。
        """
        import sys

        try:
            return self._read_input_prompt_toolkit()
        except ImportError:
            pass

        if sys.platform != "win32":
            try:
                return input("\033[1;36mYou\033[0m: ")
            except EOFError:
                raise KeyboardInterrupt

        # ── Windows: 逐字符读取 ──
        import ctypes
        import msvcrt

        VK_SHIFT = 0x10
        user32 = ctypes.windll.user32

        GetAsyncKeyState = user32.GetAsyncKeyState
        GetAsyncKeyState.argtypes = [ctypes.c_int]
        GetAsyncKeyState.restype = ctypes.c_short

        def shift_held() -> bool:
            return bool(GetAsyncKeyState(VK_SHIFT) & 0x8000)

        def move_left(cols: int) -> None:
            if cols > 0:
                sys.stdout.write(f"\033[{cols}D")

        def move_right(cols: int) -> None:
            if cols > 0:
                sys.stdout.write(f"\033[{cols}C")

        def paste_input_pending() -> bool:
            """Best-effort fallback for terminals without bracketed paste."""

            if msvcrt.kbhit():
                return True
            time.sleep(0.02)
            return msvcrt.kbhit()

        sys.stdout.write("\n\033[1;36mYou\033[0m: ")
        sys.stdout.flush()

        lines: list[str] = []
        current_line: list[str] = []
        cursor_pos: int = 0  # 光标在 current_line 中的位置索引

        def _redraw_from(index: int, desired_cursor: int, *, clear_cols: int = 4) -> None:
            """从指定位置重绘到行尾，并把光标放到 desired_cursor。"""

            segment = "".join(current_line[index:])
            sys.stdout.write(segment)
            sys.stdout.write(" " * clear_cols)
            total_width = self._text_display_width(segment) + clear_cols
            desired_width = self._text_display_width("".join(current_line[index:desired_cursor]))
            move_left(total_width - desired_width)
            sys.stdout.flush()

        while True:
            ch = msvcrt.getwch()

            if ch in ('\r', '\n'):
                if shift_held() or paste_input_pending():
                    # 多行模式：跳到行尾再换行
                    if cursor_pos < len(current_line):
                        move_right(self._text_display_width("".join(current_line[cursor_pos:])))
                        cursor_pos = len(current_line)
                    lines.append("".join(current_line))
                    current_line = []
                    cursor_pos = 0
                    sys.stdout.write("\n\033[90m...\033[0m ")
                    sys.stdout.flush()
                else:
                    # 跳到行尾再回车（避免残留）
                    if cursor_pos < len(current_line):
                        move_right(self._text_display_width("".join(current_line[cursor_pos:])))
                    break

            elif ch == '\x03':
                raise KeyboardInterrupt

            elif ch == '\x1b':
                # Esc 键 — 返回 Esc 字符，由主循环处理（中断任务或忽略）
                if current_line:
                    lines.append("".join(current_line))
                sys.stdout.write("\n")
                return "\x1b"

            elif ch in ('\x08', '\x7f'):
                # Backspace: 删除光标左侧字符
                if cursor_pos > 0:
                    deleted = current_line.pop(cursor_pos - 1)
                    cursor_pos -= 1
                    move_left(self._char_display_width(deleted))
                    _redraw_from(cursor_pos, cursor_pos)

            elif ch in ('\x00', '\xe0'):
                # 扩展键序列（方向键、Home/End 等）
                second = msvcrt.getwch()
                if second == 'K':        # ← 左箭头
                    if cursor_pos > 0:
                        cursor_pos -= 1
                        move_left(self._char_display_width(current_line[cursor_pos]))
                        sys.stdout.flush()
                elif second == 'M':      # → 右箭头
                    if cursor_pos < len(current_line):
                        move_right(self._char_display_width(current_line[cursor_pos]))
                        cursor_pos += 1
                        sys.stdout.flush()
                elif second == 'H':      # Home
                    if cursor_pos > 0:
                        move_left(self._text_display_width("".join(current_line[:cursor_pos])))
                        cursor_pos = 0
                        sys.stdout.flush()
                elif second == 'O':      # End
                    if cursor_pos < len(current_line):
                        move_right(self._text_display_width("".join(current_line[cursor_pos:])))
                        cursor_pos = len(current_line)
                        sys.stdout.flush()
                elif second == 'S':      # Delete
                    if cursor_pos < len(current_line):
                        current_line.pop(cursor_pos)
                        _redraw_from(cursor_pos, cursor_pos)

            elif ch and ord(ch) >= 0x20:
                # 可见字符：在光标位置插入
                if cursor_pos >= len(current_line):
                    # 追加到末尾（常见情况，快速路径）
                    current_line.append(ch)
                    cursor_pos += 1
                    sys.stdout.write(ch)
                    sys.stdout.flush()
                else:
                    # 插入到中间位置
                    old_cursor = cursor_pos
                    current_line.insert(cursor_pos, ch)
                    cursor_pos += 1
                    _redraw_from(old_cursor, cursor_pos)

        if current_line:
            lines.append("".join(current_line))

        result = "\n".join(lines)
        sys.stdout.write("\n")
        return result.strip("\r\n")

    def _handle_command(self, raw: str) -> bool:
        """处理斜杠命令。返回 True 表示需要退出。"""
        from omniagent.repl.commands import ExitSignal

        parts = raw.split(maxsplit=1)
        cmd_name = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        try:
            output = dispatch_command(
                cmd_name,
                args,
                registry=self.registry,
                ctx_mgr=self.ctx_mgr,
                session_state=self._session_state,
            )
        except ExitSignal:
            return True

        if output:
            console.print(Panel(output, title=f"[command]{cmd_name}[/command]", border_style="magenta"))
        return False

    def _handle_chat(self, user_input: str) -> None:
        """处理多轮对话，支持 prompt 优化和多种思考范式。"""
        # 自动 compact 检查
        if self.ctx_mgr.needs_compact():
            console.print("[yellow]⚠️  对话历史较长，建议执行 /compact 压缩。[/yellow]")

        # 保存 undo 快照
        self.ctx_mgr.save_snapshot()

        # ── 项目上下文注入（首次对话时） ──────────────────
        self._inject_project_context()

        # ── 记忆注入 ──────────────────────────────────
        self._inject_memories(user_input)

        # ── 当前 runtime session notes 注入 ───────────────
        self._inject_session_notes()

        # ── 意图检测（始终执行，用于路由决策）──
        intent = self._detect_intent(user_input)

        # ── Prompt 优化（按需） ──────────────────────────
        if self.optimize_prompts:
            optimized, system_hint, was_optimized = optimize_prompt(user_input)
            console.print(f"[dim]🎯 意图: {get_intent_display(intent)}[/dim]")

            if was_optimized:
                # 展示优化后的 prompt，帮助用户学习
                console.print(Panel(
                    optimized,
                    title="[dim]📝 优化后的 Prompt（供学习参考）[/dim]",
                    border_style="dim",
                    padding=(0, 1),
                ))
                if system_hint:
                    self.ctx_mgr.add_system_message(f"[指令上下文] {system_hint}")
            elif intent is not None:
                # 有明确任务意图，但提示词质量已足够好
                console.print("[dim]✅ 提示词质量良好，无需优化[/dim]")
                if system_hint:
                    self.ctx_mgr.add_system_message(f"[指令上下文] {system_hint}")
            else:
                # 通用对话，无明确任务意图
                console.print("[dim]💬 通用对话[/dim]")
        else:
            optimized = user_input

        # 添加用户消息
        self.ctx_mgr.add_user_message(optimized)

        # ── 用户消息回显：极氪风格面板 ──
        console.print(Panel(
            optimized,
            title="[bold bright_cyan]▸ You[/bold bright_cyan]",
            border_style="bright_cyan",
            padding=(0, 1),
        ))

        # 获取模型列表
        model_ids = self.registry.get_role_priority("planner")
        if not model_ids:
            console.print("[error]❌ 未配置任何模型。请先使用 /set_model 添加模型。[/error]")
            return

        # 注入对话历史到 AgentContext，供引擎使用
        self.agent_context.set_conversation_messages(self.ctx_mgr.get_messages())

        # ── 注入 MCP 注册表（修复 MCPCallTool 空注册表 bug）──
        if hasattr(self, '_mcp_registry') and self._mcp_registry is not None:
            self.agent_context.set("_mcp_registry", self._mcp_registry)

        # 根据当前思考范式选择执行方式
        mode = self.registry.current_mode
        recorder = self._start_run(
            user_input,
            mode,
            model_ids,
            optimized=optimized,
            system_hint=system_hint if self.optimize_prompts else None,
            was_optimized=was_optimized if self.optimize_prompts else False,
            intent=intent if self.optimize_prompts else None,
        )
        self._append_thread_message(
            "user",
            optimized,
            run_id=recorder.run_id,
            metadata={
                "raw_input": user_input,
                "optimized": bool(was_optimized) if self.optimize_prompts else False,
                "mode": mode,
            },
        )

        try:
            # ── 通用对话自动使用 direct 模式（忽略全局 mode 设置）──
            # PlanExecute/ReAct 等重型引擎对纯对话无益，且会错误地
            # 从对话历史中提取目录进行不必要的侦察分析。
            if intent is None:
                self._run_direct(optimized, model_ids)
            elif mode == "react":
                self._run_react_engine(optimized, model_ids)
            elif mode == "plan-execute":
                self._run_plan_execute_engine(optimized, model_ids)
            elif mode == "reflection":
                self._run_reflection_engine(optimized, model_ids)
            elif mode == "plan-react":
                self._run_plan_react_engine(optimized, model_ids)
            elif mode == "plan-reflection":
                self._run_plan_reflection_engine(optimized, model_ids)
            elif mode == "react-reflection":
                self._run_react_reflection_engine(optimized, model_ids)
            elif mode == "novel":
                self._run_novel_engine(optimized, model_ids)
            else:
                # direct 模式 — 直接调 LLM
                self._run_direct(optimized, model_ids)

            if not recorder.is_finished:
                recorder.finish(status="success")
        except Exception as e:
            if not recorder.is_finished:
                recorder.finish(status="error", reason=str(e))
            raise
        finally:
            if self._current_run_recorder is recorder:
                self._current_run_recorder = None
                self._session_state.pop("_run_recorder", None)

    def _run_direct(self, user_input: str, model_ids: list[str]) -> None:
        """直接对话模式。通过结构化信号协议委派工具任务给 ReAct 引擎。

        架构决策：
        - 输入侧用 regex 预筛选明确的工具操作（如"创建文件"）
        - 输出侧不再用 regex 猜测 LLM 意图（这是不可靠的），
          而是依赖 LLM 主动输出 [REQUIRES_TOOLS] 结构化信号来请求工具支持
        """
        # 预检测：明确的工具操作任务直接走 ReAct
        if self._detect_tool_need(user_input):
            console.print("[cyan]🔧 检测到需要工具执行，自动切换到 ReAct 模式...[/cyan]")
            self._run_react_engine(user_input, model_ids)
            return

        messages = self.ctx_mgr.get_messages()

        last_error = None
        for model_id in model_ids:
            try:
                # ── 注入当前模型身份到 LLM 上下文 ──
                # 这样 LLM 被问"你是什么模型"时能如实回答，而非猜测或拒绝
                model_id_msg = {"role": "system", "content": f"[会话信息] 当前驱动模型: {model_id}"}
                call_messages = list(messages) + [model_id_msg]

                if self.streaming:
                    response_text = self._stream_response(model_id, call_messages)
                else:
                    response_text = self._blocking_response(model_id, call_messages)
                self.status_bar.set_last_model(model_id)

                if response_text:
                    # ── 结构化信号：LLM 显式请求工具支持 ──
                    # [REQUIRES_TOOLS] 是 LLM 和系统之间的约定协议，
                    # 只有当 LLM 主动输出此标记时才会切换到 ReAct 模式。
                    # 这样避免了用 regex 从自然语言中"猜测"LLM 意图的不可靠性。
                    if response_text.lstrip().startswith("[REQUIRES_TOOLS]"):
                        console.print()
                        console.print("[cyan]🔧 LLM 请求工具支持，自动切换到 ReAct 模式...[/cyan]")
                        if self._current_run_recorder is not None:
                            self._current_run_recorder.emit(
                                "run.warning",
                                warning="LLM requested tools via [REQUIRES_TOOLS] signal",
                            )
                        self.ctx_mgr.trim_last_assistant()
                        self._run_react_engine(user_input, model_ids)
                        return

                self._append_thread_message(
                    "assistant",
                    response_text or "",
                    run_id=self._current_run_recorder.run_id if self._current_run_recorder else None,
                    model_used=model_id,
                )
                self._finish_run(status="success", result=response_text or "")
                return
            except Exception as e:
                last_error = e
                if self._current_run_recorder is not None:
                    self._current_run_recorder.emit("llm.call_failed", model=model_id, error=str(e))
                console.print(f"[yellow]模型 {model_id} 失败: {e}，尝试下一个...[/yellow]")

        console.print(f"[error]❌ 所有模型均调用失败: {last_error}[/error]")
        self._finish_run(status="error", reason=str(last_error))

    def _run_react_engine(self, user_input: str, model_ids: list[str]) -> None:
        """ReAct 引擎模式。"""
        from omniagent.engine.react_engine import ReActEngine

        iterations = self._estimate_react_iterations(user_input)
        console.print(ModeHeader("ReAct", iterations=iterations))

        # 追踪引擎状态
        self.status_bar.set_engine_status("running")
        self.status_bar.set_iteration(0, iterations)

        # 重置中断信号
        self._interrupt_event.clear()
        self._task_running = True
        self._sigint_count = 0

        callback = self._make_callback()
        engine = ReActEngine(
            model_priority=model_ids,
            max_iterations=iterations,
            callback=callback,
            interrupt_event=self._interrupt_event,
        )
        try:
            result = engine.run(user_input, self.agent_context)
            if self._interrupt_event.is_set():
                result = result + "\n\n⚠️ **任务被用户中断**" if result else "⚠️ 任务被用户中断"
            self.ctx_mgr.add_assistant_message(result, model_used=model_ids[0])
            self._append_thread_message("assistant", result, run_id=self._current_run_recorder.run_id if self._current_run_recorder else None, model_used=model_ids[0])
            self._render_engine_result(callback, result, "ReAct 结果")
            self.status_bar.set_last_model(model_ids[0])
            self._finish_run(status="success", result=result)
        except Exception as e:
            from omniagent.repl.cards import ErrorCard
            console.print(ErrorCard(str(e), title="ReAct 引擎执行失败"))
            self._finish_run(status="error", reason=str(e))
        finally:
            self._task_running = False
            self._interrupt_event.clear()
            self.status_bar.set_engine_status("done")
            console.print(render_shortcut_bar())

    def _run_plan_execute_engine(self, user_input: str, model_ids: list[str]) -> None:
        """Plan-Execute 引擎模式。"""
        from omniagent.engine.plan_execute_engine import PlanExecuteEngine

        console.print(ModeHeader("Plan-Execute"))
        self.status_bar.set_engine_status("running")

        callback = self._make_callback()
        engine = PlanExecuteEngine(model_priority=model_ids, max_steps=20, callback=callback)
        try:
            result = engine.run(user_input, self.agent_context)
            self.ctx_mgr.add_assistant_message(result, model_used=model_ids[0])
            self._append_thread_message("assistant", result, run_id=self._current_run_recorder.run_id if self._current_run_recorder else None, model_used=model_ids[0])
            self._render_engine_result(callback, result, "Plan-Execute 结果")
            self.status_bar.set_last_model(model_ids[0])
            self._finish_run(status="success", result=result)
        except Exception as e:
            from omniagent.repl.cards import ErrorCard
            console.print(ErrorCard(str(e), title="Plan-Execute 引擎执行失败"))
            self._finish_run(status="error", reason=str(e))
        finally:
            self.status_bar.set_engine_status("done")
            console.print(render_shortcut_bar())

    def _run_reflection_engine(self, user_input: str, model_ids: list[str]) -> None:
        """Reflection 引擎模式。"""
        from omniagent.engine.reflection_engine import ReflectionEngine

        console.print(ModeHeader("Reflection"))
        self.status_bar.set_engine_status("running")

        callback = self._make_callback()
        engine = ReflectionEngine(model_priority=model_ids, max_rounds=3, callback=callback)
        try:
            result = engine.run(user_input)
            self.ctx_mgr.add_assistant_message(result, model_used=model_ids[0])
            self._append_thread_message("assistant", result, run_id=self._current_run_recorder.run_id if self._current_run_recorder else None, model_used=model_ids[0])
            self._render_engine_result(callback, result, "Reflection 结果")
            self.status_bar.set_last_model(model_ids[0])
            self._finish_run(status="success", result=result)
        except Exception as e:
            from omniagent.repl.cards import ErrorCard
            console.print(ErrorCard(str(e), title="Reflection 引擎执行失败"))
            self._finish_run(status="error", reason=str(e))
        finally:
            self.status_bar.set_engine_status("done")
            console.print(render_shortcut_bar())

    def _run_plan_react_engine(self, user_input: str, model_ids: list[str]) -> None:
        """Plan + React 组合引擎模式。"""
        from omniagent.engine.combined_engines import PlanReactEngine

        console.print("[cyan]📋🔄 Plan+React 模式: 全局规划 → 每步 ReAct 执行[/cyan]")

        iterations = self._estimate_react_iterations(user_input)
        plan_steps = max(6, min(iterations, 15))

        callback = self._make_callback()
        engine = PlanReactEngine(model_priority=model_ids, max_steps=plan_steps, react_iterations=iterations, callback=callback)
        try:
            result = engine.run(user_input, context=self.agent_context)
            self.ctx_mgr.add_assistant_message(result, model_used=model_ids[0])
            self._append_thread_message("assistant", result, run_id=self._current_run_recorder.run_id if self._current_run_recorder else None, model_used=model_ids[0])
            self._render_engine_result(callback, result, "Plan+React 结果")
            self.status_bar.set_last_model(model_ids[0])
            self._finish_run(status="success", result=result)
        except Exception as e:
            console.print(f"[error]❌ Plan+React 引擎执行失败: {e}[/error]")
            self._finish_run(status="error", reason=str(e))

    def _run_plan_reflection_engine(self, user_input: str, model_ids: list[str]) -> None:
        """Plan + Reflection 组合引擎模式。"""
        from omniagent.engine.combined_engines import PlanReflectionEngine

        console.print("[cyan]📋🔍 Plan+Reflection 模式: 规划执行 → 反思修正[/cyan]")

        callback = self._make_callback()
        engine = PlanReflectionEngine(model_priority=model_ids, max_steps=10, review_rounds=2, callback=callback)
        try:
            result = engine.run(user_input, context=self.agent_context)
            self.ctx_mgr.add_assistant_message(result, model_used=model_ids[0])
            self._append_thread_message("assistant", result, run_id=self._current_run_recorder.run_id if self._current_run_recorder else None, model_used=model_ids[0])
            self._render_engine_result(callback, result, "Plan+Reflection 结果")
            self.status_bar.set_last_model(model_ids[0])
            self._finish_run(status="success", result=result)
        except Exception as e:
            console.print(f"[error]❌ Plan+Reflection 引擎执行失败: {e}[/error]")
            self._finish_run(status="error", reason=str(e))

    def _run_react_reflection_engine(self, user_input: str, model_ids: list[str]) -> None:
        """ReAct + Reflection 组合引擎模式。"""
        from omniagent.engine.combined_engines import ReactReflectionEngine

        console.print("[cyan]🔄🔍 React+Reflection 模式: ReAct 探索 → 反思审查[/cyan]")

        callback = self._make_callback()
        engine = ReactReflectionEngine(model_priority=model_ids, react_iterations=8, review_rounds=2, callback=callback)
        try:
            result = engine.run(user_input, context=self.agent_context)
            self.ctx_mgr.add_assistant_message(result, model_used=model_ids[0])
            self._append_thread_message("assistant", result, run_id=self._current_run_recorder.run_id if self._current_run_recorder else None, model_used=model_ids[0])
            self._render_engine_result(callback, result, "React+Reflection 结果")
            self.status_bar.set_last_model(model_ids[0])
            self._finish_run(status="success", result=result)
        except Exception as e:
            console.print(f"[error]❌ React+Reflection 引擎执行失败: {e}[/error]")
            self._finish_run(status="error", reason=str(e))

    def _run_novel_engine(self, user_input: str, model_ids: list[str]) -> None:
        """小说创作引擎模式（支持多小说隔离）。"""
        from omniagent.engine.novel_engine import NovelEngine

        console.print("[magenta]Novel 模式: 小说创作助手[/magenta]")

        callback = self._make_callback()
        engine = NovelEngine(
            model_priority=model_ids,
            max_iterations=15,
            callback=callback,
            novel_manager=self._novel_manager,
        )
        try:
            result = engine.run(user_input, context=self.agent_context)
            self.ctx_mgr.add_assistant_message(result, model_used=model_ids[0])
            self._append_thread_message("assistant", result, run_id=self._current_run_recorder.run_id if self._current_run_recorder else None, model_used=model_ids[0])
            self._render_engine_result(callback, result, "Novel 创作结果", border_style="magenta")
            self.status_bar.set_last_model(model_ids[0])
            self._finish_run(status="success", result=result)
        except Exception as e:
            console.print(f"[error]❌ 小说创作引擎执行失败: {e}[/error]")
            self._finish_run(status="error", reason=str(e))

    def _stream_response(self, model_id: str, messages: list[dict[str, str]]) -> str:
        """流式输出模型回复，完成后 Markdown 渲染。返回完整响应文本。"""
        from omniagent.utils.llm_client import chat_completion_stream
        from rich.live import Live
        from rich.spinner import Spinner

        full_response = []
        if self._current_run_recorder is not None:
            self._current_run_recorder.emit("llm.model_selected", model=model_id, strategy="planner_priority")

        # 流式阶段：显示 spinner + 实时 token 计数
        with Live(
            Spinner("dots", text="[cyan] 思考中...[/cyan]"),
            console=console,
            refresh_per_second=10,
            transient=True,  # 结束后自动清除 spinner
        ) as live:
            for chunk in chat_completion_stream(model_id, messages):
                full_response.append(chunk)
                if self._current_run_recorder is not None:
                    self._current_run_recorder.emit("llm.token", model=model_id, token=chunk)
                token_count = len("".join(full_response))
                live.update(f"[cyan] 生成中... {token_count} tokens[/cyan]")

        response_text = "".join(full_response)
        if self._current_run_recorder is not None:
            self._current_run_recorder.emit(
                "llm.usage",
                model=model_id,
                input_tokens=self.ctx_mgr.estimate_tokens("\n".join(m.get("content", "") for m in messages)),
                output_tokens=self.ctx_mgr.estimate_tokens(response_text),
            )

        # 极氪风格：流式完成后渲染增强 Markdown
        if response_text.strip():
            renderer = OutputRenderer(verbose=self.verbose)
            console.print(Panel(
                renderer._render_markdown_enhanced(response_text),
                title=f"[bold bright_green]◆ Assistant[/bold bright_green] [dim]· {model_id} · {len(response_text):,} chars[/dim]",
                border_style="bright_green",
                padding=(1, 2),
            ))
            console.print("[dim]──[/dim]")  # 视觉分隔

        self.ctx_mgr.add_assistant_message(response_text, model_used=model_id)
        return response_text

    def _blocking_response(self, model_id: str, messages: list[dict[str, str]]) -> str:
        """阻塞式输出模型回复。返回响应文本。"""
        from omniagent.utils.llm_client import chat_completion

        console.print(f"[dim]调用 {model_id}...[/dim]")
        if self._current_run_recorder is not None:
            self._current_run_recorder.emit("llm.model_selected", model=model_id, strategy="planner_priority")
        response = chat_completion(model_id, messages)
        if self._current_run_recorder is not None:
            self._current_run_recorder.emit("llm.message", model=model_id, content=response)
            self._current_run_recorder.emit(
                "llm.usage",
                model=model_id,
                input_tokens=self.ctx_mgr.estimate_tokens("\n".join(m.get("content", "") for m in messages)),
                output_tokens=self.ctx_mgr.estimate_tokens(response),
            )

        self.ctx_mgr.add_assistant_message(response, model_used=model_id)

        console.print()
        renderer = OutputRenderer(verbose=self.verbose)
        console.print(Panel(
            renderer._render_markdown_enhanced(response),
            title=f"[bold bright_green]◆ Assistant[/bold bright_green] [dim]· {model_id} · {len(response):,} chars[/dim]",
            border_style="bright_green",
            padding=(1, 2),
        ))
        console.print("[dim]──[/dim]")
        return response

    @staticmethod
    def _detect_intent(text: str) -> str | None:
        """检测用户意图。"""
        from omniagent.repl.prompt_optimizer import detect_intent
        return detect_intent(text)

    # ── 工具需求检测 ──────────────────────────────────────────
    _TOOL_PATTERNS: list[re.Pattern[str]] = [
        # 天气/时间/信息查询（需要工具获取实时数据）
        re.compile(r"(?:天气|气温|温度|热不热|冷不冷|穿衣|几度|多少度)", re.I),
        re.compile(r"(?:weather|temperature|forecast)", re.I),
        re.compile(r"(?:黄金|金价|股价|汇率|行情|价格)", re.I),
        re.compile(r"(?:今天|今日|现在|当前).{0,5}(?:天气|温度|时间|日期)", re.I),
        re.compile(r"(?:查询|查|看).{0,10}(?:天气|时间|日期|新闻)", re.I),
        re.compile(r"(?:现在几点|今天几号|今天星期几|今天日期)", re.I),
        # 文件写入/创建/保存（以下为原有模式）
        # 文件写入/创建/保存
        re.compile(r"(?:写入|创建|保存|新建|生成|输出).{0,20}(?:文件|到|至|为)", re.I),
        re.compile(r"(?:write|create|save|generate|output).{0,20}(?:file|to)", re.I),
        re.compile(r"(?:文件|file).{0,10}(?:写入|创建|保存|新建)", re.I),
        # 文件读取/查看
        re.compile(r"(?:读取|查看|打开|读|看).{0,20}(?:文件|内容|代码|配置)", re.I),
        re.compile(r"(?:read|open|show|cat|view).{0,20}(?:file|content)", re.I),
        # 文件修改/编辑
        re.compile(r"(?:修改|编辑|替换|改|更新).{0,20}(?:文件|代码)", re.I),
        re.compile(r"(?:edit|modify|update|replace|patch).{0,20}(?:file|code)", re.I),
        # 文件删除
        re.compile(r"(?:删除|移除|清除).{0,20}(?:文件|目录)", re.I),
        re.compile(r"(?:delete|remove).{0,20}(?:file|dir)", re.I),
        # 文件移动/复制/重命名 ← 新增
        re.compile(r"(?:移动|搬|转移|挪).{0,20}(?:文件|到|至|桌面|下载|文档)", re.I),
        re.compile(r"(?:复制|拷贝|备份).{0,20}(?:文件|到|至)", re.I),
        re.compile(r"(?:重命名|改名).{0,10}(?:文件|为)", re.I),
        re.compile(r"(?:move|copy|cp|mv|rename).{0,20}(?:file|to)", re.I),
        # 命令执行（含代词：执行它/运行这个/跑一下）
        re.compile(r"(?:执行|运行|跑).{0,15}(?:命令|脚本|程序|命令行|测试|pytest|npm|pip|python|node)", re.I),
        re.compile(r"(?:执行|运行|跑|试试).{0,5}(?:它|他|她|这个|一下|看看|试试)", re.I),
        re.compile(r"(?:试试|试下).{0,3}(?:执行|运行|跑)", re.I),
        re.compile(r"(?:run|execute|exec).{0,15}(?:command|script|cmd|test|pytest|npm|pip|python|node|it|this)", re.I),
        re.compile(r"(?:run|execute|exec)\s+it", re.I),
        # Git 操作
        re.compile(r"\bgit\b.{0,20}(?:commit|push|pull|add|clone|checkout|branch|merge|stash|status|log|diff|show|remote|fetch|init|rebase|reset|restore)", re.I),
        re.compile(r"(?:提交|推送|拉取|克隆|分支|合并|git)\b", re.I),
        # 搜索
        re.compile(r"(?:搜索|查找|grep|find).{0,20}(?:文件|内容|代码|文本|字符)", re.I),
        re.compile(r"(?:search|find|grep).{0,30}", re.I),
        # 网页抓取
        re.compile(r"(?:抓取|下载|获取|访问).{0,20}(?:网页|页面|url|网址)", re.I),
        re.compile(r"(?:fetch|download|scrape|crawl).{0,20}(?:web|page|url)", re.I),
        # 文件路径模式（./xxx, src/xxx, C:\xxx, .py, .js 等）
        re.compile(r"(?:^|\s)(?:\./|\.\./|src/|tests?/|lib/|app/|dist/|build/)\S+", re.I),
        re.compile(r"(?:^|\s)[A-Z]:\\[\w\\/.]+", re.I),
        re.compile(r"\b\w+\.(?:py|js|ts|jsx|tsx|java|c|cpp|h|go|rs|rb|php|html|css|json|yaml|yml|toml|xml|md|txt|sh|bat|ps1)\b", re.I),
        # 列出文件
        re.compile(r"(?:列出|显示|查看).{0,15}(?:文件|目录|文件夹|文件列表)", re.I),
        re.compile(r"(?:list|ls|dir|tree).{0,15}(?:file|dir|folder)", re.I),
        # 目录/文件夹操作
        re.compile(r"(?:创建|新建|建|mkdir).{0,10}(?:目录|文件夹|folder|dir)", re.I),
        re.compile(r"(?:create|make|mkdir).{0,10}(?:dir|folder|directory)", re.I),
        # 通用编程任务（容易涉及文件操作）
        re.compile(r"(?:帮我|请|给).{0,5}(?:写|做|创建|实现|开发|搭|建).{0,20}(?:一个|个|项目|工程|脚本|程序|代码)", re.I),
        re.compile(r"(?:help\s+me|please).{0,10}(?:write|create|build|implement|develop|make).{0,20}(?:a|an|the|project|script|app|code)", re.I),
        # 安装/包管理
        re.compile(r"(?:安装|install).{0,15}(?:包|库|依赖|package|pip|npm|yarn|cargo)", re.I),
    ]

    @classmethod
    def _detect_tool_need(cls, text: str) -> bool:
        """检测用户输入是否明确需要工具执行（仅匹配编程/文件/命令任务）。"""
        for pattern in cls._TOOL_PATTERNS:
            if pattern.search(text):
                return True
        return False

    @classmethod
    def _estimate_react_iterations(cls, text: str) -> int:
        """根据任务复杂度自适应估算 ReAct 迭代次数。

        这不是硬编码的数字分配，而是基于任务特征的多维度评估：
        - 操作类型（读/写/分析）
        - 项目规模（单文件 vs 多文件 vs 完整项目）
        - 任务广度（单一操作 vs 多步骤流程）

        返回值范围：5（简单） ~ 20（复杂项目分析）
        """
        score = 0

        # 维度 1：操作类型
        # 分析任务需要更多探索（list_files + 多次 read_file + 合成 final_answer）
        analysis_patterns = [
            r"分析.{0,30}(?:项目|代码|仓库|工程|架构|质量|性能|不足|问题|改进)",
            r"(?:评估|审查|检查|诊断).{0,20}(?:项目|代码|质量|安全)",
            r"(?:分析|评估).{0,10}(?:代码|结构|设计|模式)",
        ]
        for p in analysis_patterns:
            if re.search(p, text, re.I):
                score += 8  # 分析类任务基线就高
                break

        # 文件操作任务（需要创建/修改/删除）
        file_op_patterns = [
            r"(?:创建|新建|写入|修改|编辑|删除|移动|复制).{0,10}(?:文件|目录|项目)",
            r"(?:write|create|edit|delete|move|copy).{0,10}(?:file|dir|project)",
        ]
        for p in file_op_patterns:
            if re.search(p, text, re.I):
                score += 4
                break

        # 维度 2：项目规模
        # 提到具体项目路径或目录 → 多文件操作
        if re.search(r"[A-Z]:\\", text) or re.search(r"(?:项目|仓库|工程|代码库|workspace|project|repo)", text, re.I):
            score += 4
        # 提到了 list_files / read_file 等工具
        if re.search(r"(?:list_files|read_file|search_files|list|ls|dir)\b", text, re.I):
            score += 3

        # 维度 3：任务广度
        # 多步骤指令
        step_indicators = len(re.findall(r"(?:\d[\.\)、]|第\s*\d|首先|然后|接着|最后|其次|此外|另外)", text))
        if step_indicators >= 3:
            score += 4
        elif step_indicators >= 1:
            score += 2

        # 维度 4：有实时数据需求 → 轻量（天气、时间等只需要 1-2 步）
        realtime_patterns = [
            r"(?:天气|气温|温度|热不热|冷不冷|几度|多少度)",
            r"(?:weather|temperature|forecast)",
            r"(?:现在几点|今天几号|今天星期几|日期|时间)",
            r"(?:黄金|金价|股价|汇率|行情)",
        ]
        for p in realtime_patterns:
            if re.search(p, text, re.I):
                score -= 5  # 降低复杂度——这些通常 1-2 步就够
                break

        # 维度 5：简单对话（无工具需求）
        chat_patterns = [
            r"^(你好|hi|hello|嗨|嘿)\b",
            r"^(介绍一下|什么是|解释|说明)\b",
            r"^(帮我|请).{0,5}(?:写|实现|创建|生成).{0,20}(?:一个|个|函数|脚本|代码|类)",
        ]
        for p in chat_patterns:
            if re.search(p, text, re.I):
                score -= 2
                break

        # 映射到实际迭代数
        if score <= 0:
            return 5   # 极简任务（天气、时间查询）
        elif score <= 3:
            return 8   # 简单任务（单文件操作、简单代码生成）
        elif score <= 6:
            return 12  # 中等任务（多文件、一般分析）
        elif score <= 10:
            return 16  # 较复杂任务（项目代码分析）
        else:
            return 20  # 复杂项目分析、多步骤重构

    # ── 交互式权限审批 ──────────────────────────────────────

    # 工具图标映射（统一来源：omniagent.repl.cards.TOOL_ICONS）
    _TOOL_ICONS: dict[str, str] = {}  # 由 __init__ 从 TOOL_ICONS 填充

    def _approval_handler(self, tool_name: str, params_preview: str) -> bool:
        """交互式审批处理器 — 使用 ApprovalCard 卡片式 UI。

        类似 Claude Code 的权限提示：在工具执行前展示操作内容并询问用户。
        支持三种选择:
        - (y) 批准一次 — 仅本次放行
        - (a) 本次会话始终批准 — 缓存到会话结束
        - (n) 拒绝 — 阻止本次执行
        """
        # 检查缓存
        cache_key = f"{tool_name}:{params_preview}"
        if cache_key in self._approval_cache:
            return self._approval_cache[cache_key]

        # 使用卡片式审批 UI
        console.print()
        card = ApprovalCard(
            tool_name,
            params_preview,
            always_approved_count=len(self._approval_cache),
        )
        console.print(card)

        try:
            choice = Prompt.ask(
                f"[bold bright_cyan]▸[/bold bright_cyan] 是否允许?",
                choices=["y", "a", "n"],
                default="n",
            )
        except (KeyboardInterrupt, EOFError):
            console.print("[red]⛔ 已取消[/red]")
            return False

        if choice == "a":
            self._approval_cache[cache_key] = True
            console.print(f"[green]✓ 已批准（本次会话 · 已缓存 {len(self._approval_cache)} 项）[/green]")
            # 同步到状态栏
            self.status_bar.set_always_approved_count(len(self._approval_cache))
            return True
        elif choice == "y":
            console.print("[green]✓ 已批准（仅本次）[/green]")
            return True
        else:
            console.print("[red]⛔ 已拒绝[/red]")
            return False

    def _inject_project_context(self) -> None:
        """首次对话时注入项目上下文（类型、文件树、规则）。"""
        if self._project_injected:
            return
        self._project_injected = True

        try:
            self.project_ctx.detect()
            ctx_text = self.project_ctx.format_for_context()
            if ctx_text:
                self.ctx_mgr.add_system_message(ctx_text)
                logger.debug(f"注入项目上下文: {self.project_ctx.project_type}")
        except Exception as e:
            logger.debug(f"项目上下文检测失败: {e}")

    def _inject_memories(self, user_input: str) -> None:
        """将相关记忆注入上下文。"""
        try:
            from omniagent.repl.memory import MemoryStore
            store = MemoryStore()
            relevant = store.get_relevant(user_input, limit=3)
            if relevant:
                memory_text = store.format_for_context(relevant)
                self.ctx_mgr.add_system_message(memory_text)
                logger.debug(f"注入 {len(relevant)} 条相关记忆")
        except Exception:
            pass  # 记忆注入失败不影响对话

    def _inject_session_notes(self) -> None:
        """Inject current runtime session notes into the chat context."""
        try:
            notes = self.session_store.read_notes(self.runtime_session.id).strip()
            body = notes.replace("# Session Notes", "").strip()
            if body:
                if len(body) > 4000:
                    body = body[-4000:]
                self.ctx_mgr.add_system_message(f"[Session Notes]\n{body}")
        except Exception as e:
            logger.debug("session notes injection failed: %s", e)

    def _load_custom_commands(self) -> None:
        """加载自定义快捷指令和技能，动态注册为命令。"""
        from omniagent.repl.commands import register_command, _HANDLERS

        # 加载快捷指令
        try:
            from omniagent.repl.shortcut_manager import ShortcutManager
            sm = ShortcutManager()
            for sc in sm.list_all():
                cmd_name = f"/{sc.name}"
                if cmd_name not in _HANDLERS:
                    def make_shortcut_handler(sc_name):
                        def handler(*, args: str, **kwargs: Any) -> str:
                            return sm.execute(sc_name, args)
                        return handler
                    _HANDLERS[cmd_name] = make_shortcut_handler(sc.name)
                    register_command(cmd_name, f"[快捷] {sc.description}", cmd_name)
            if sm.list_all():
                console.print(f"[dim]已加载 {len(sm.list_all())} 个快捷指令[/dim]")
        except Exception as e:
            logger.debug(f"加载快捷指令失败: {e}")

        # 加载技能
        try:
            from omniagent.repl.skill_manager import SkillManager
            skm = SkillManager()
            for sk in skm.list_all():
                cmd_name = f"/{sk.name}"
                if cmd_name not in _HANDLERS:
                    def make_skill_handler(sk_name):
                        def handler(*, args: str, registry: ModelRegistry, **kwargs: Any) -> str:
                            model_ids = registry.get_role_priority("planner")
                            return skm.execute(sk_name, args, model_priority=model_ids)
                        return handler
                    _HANDLERS[cmd_name] = make_skill_handler(sk.name)
                    register_command(cmd_name, f"[技能] {sk.description}", cmd_name)
            if skm.list_all():
                console.print(f"[dim]已加载 {len(skm.list_all())} 个技能[/dim]")
        except Exception as e:
            logger.debug(f"加载技能失败: {e}")

    def _auto_cleanup(self) -> None:
        """启动时自动清理过期会话数据。"""
        try:
            from omniagent.engine.cleanup import SessionCleaner
            cleaner = SessionCleaner()
            stats = cleaner.cleanup()
            if stats.sessions_deleted or stats.runs_deleted or stats.checkpoints_deleted:
                logger.info(
                    f"自动清理: {stats.sessions_deleted} 会话, "
                    f"{stats.runs_deleted} 运行记录, "
                    f"{stats.checkpoints_deleted} checkpoint"
                )
        except Exception as e:
            logger.debug(f"自动清理跳过: {e}")


def start_repl(
    *,
    models: list[str] | None = None,
    mode: str | None = None,
    system_prompt: str | None = None,
    config_path: str | None = None,
    optimize: bool = True,
) -> None:
    """
    启动 REPL 的便捷入口。

    Args:
        models: 初始模型列表。
        mode: 初始思考范式。
        system_prompt: 自定义系统提示词。
        config_path: 配置文件路径。
        optimize: 是否启用 prompt 自动优化。
    """
    registry = ModelRegistry()

    if config_path:
        registry.load_from_file(config_path)

    if models:
        for i, model_id in enumerate(models):
            alias = model_id.split("/")[-1] if "/" in model_id else f"model_{i}"
            registry.add_model(model_id, alias)
            if "planner" not in registry.role_priority:
                registry.role_priority["planner"] = []
            registry.role_priority["planner"].append(alias)

    if mode:
        try:
            registry.set_mode(mode)
        except ValueError as e:
            console.print(f"[yellow]⚠️  {e}[/yellow]")

    repl = REPL(registry=registry, system_prompt=system_prompt, optimize_prompts=optimize)
    repl.run()
