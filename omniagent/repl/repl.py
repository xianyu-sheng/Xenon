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
import sys
from typing import Any

logger = logging.getLogger(__name__)

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.theme import Theme
from rich.rule import Rule

from omniagent.engine.context import AgentContext
from omniagent.repl.commands import COMMANDS, dispatch_command
from omniagent.repl.context_manager import ContextManager
from omniagent.repl.model_registry import ModelRegistry
from omniagent.repl.project_context import ProjectContext
from omniagent.repl.prompt_optimizer import get_intent_display, optimize_prompt
from omniagent.repl.status_bar import StatusBar

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
        # P3-Q1 续 / §8.8.1：开启真实 usage 跟踪——ContextManager 订阅
        # llm_client 的 usage 回调，current_token_usage() 优先用真实 total_tokens。
        self.ctx_mgr = ctx_mgr or ContextManager(track_real_usage=True)
        self.agent_context = AgentContext()
        self.system_prompt = system_prompt or self._default_system_prompt()
        self.streaming = streaming
        self.optimize_prompts = optimize_prompts
        self.verbose = verbose

        # 项目上下文
        self.project_ctx = ProjectContext()
        self._project_injected = False

        # 多小说管理器
        from omniagent.engine.novel_manager import NovelManager
        self._novel_manager = NovelManager()

        # 状态栏
        self.status_bar = StatusBar(console, self.ctx_mgr, self.registry)

        # 会话状态，供命令处理器共享
        self._session_state: dict[str, Any] = {
            "agent_context": self.agent_context,
            "_repl": self,
            "_novel_manager": self._novel_manager,
        }

    def _make_callback(self):
        """根据 verbose 状态创建引擎回调。"""
        from omniagent.engine.callbacks import ConsoleCallback
        return ConsoleCallback(verbose=self.verbose)

    def _render_engine_result(self, callback, result: str, title: str, border_style: str = "green") -> None:
        """渲染引擎结果：先思考面板，再最终答案。"""
        # 1. 渲染思考过程面板（如果有）
        panel = callback.get_thinking_panel()
        if panel is not None:
            console.print(panel)

        # 2. 渲染最终答案
        console.print(Panel(
            Markdown(result),
            title=f"[bold]{title}[/bold]",
            border_style=border_style,
            padding=(0, 1),
        ))

    @staticmethod
    def _default_system_prompt() -> str:
        from datetime import datetime
        now = datetime.now()
        weekdays_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        current_date = f"{now.year}年{now.month}月{now.day}日 {weekdays_cn[now.weekday()]}"
        return (
            "你是 OmniAgent-CLI 的 AI 编程助手。"
            "你可以帮助用户编写代码、调试问题、解释概念。"
            f"当前日期: {current_date}。"
            "当用户询问日期、时间等问题时，直接使用此信息回答，不要编造。"
            "请用中文回答，代码部分用英文。"
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
                console.print("\n[dim]再见！[/dim]")
                break

            if not user_input:
                continue

            # 斜杠命令
            if user_input.startswith("/"):
                if self._handle_command(user_input):
                    console.print("[dim]再见！[/dim]")
                    break
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
            console.print("[dim]· 尚未配置 API Key，输入 [bold cyan]/setup[/bold cyan] 进入配置向导[/dim]\n")
        elif not self.registry.list_models():
            # 有 Key 但没选模型 — 自动注册已配置厂商的模型
            for p in configured:
                if p.models:
                    model_id = f"{p.key}/{p.models[0]}"
                    alias = p.models[0].replace(".", "-")
                    self.registry.add_model(model_id, alias)
                    if "planner" not in self.registry.role_priority:
                        self.registry.role_priority["planner"] = []
                    self.registry.role_priority["planner"].append(alias)

            if self.registry.list_models():
                models = self.registry.list_models()
                console.print(f"[dim]· 已自动加载 {len(models)} 个模型，输入 [bold cyan]/model[/bold cyan] 切换[/dim]\n")

        # 加载自定义快捷指令和技能
        self._load_custom_commands()

    def _print_welcome(self) -> None:
        """打印 Claude Code 风格的欢迎界面。"""
        import random

        mode = self.registry.get_current_mode()
        models = self.registry.list_models()

        # ── ASCII Art Logo（清晰大字版）──
        logo = [
            "[bold cyan]   ___  __  __  __  __  _  _    __   __  __  ______[/bold cyan]",
            "[bold cyan]  / _ \\|  \\/  |/ _||  \\| |  / _\\ |  \\/  ||  ____|[/bold cyan]",
            "[bold cyan] | |_| || |\\/| | |_ | |  | | / |_ | |\\/| || |___[/bold cyan]",
            "[bold cyan] |  _  || |  | |  _|| |/\\| ||  _ || |  | ||  ___|[/bold cyan]",
            "[bold cyan] |_| |_||_|  |_||_|  |_| \\__||_|_\\|_|  |_||______|[/bold cyan]",
        ]

        # ── 版本信息 ──
        version = "v0.1.0"

        # ── 模型状态 ──
        if models:
            model_names = ", ".join(m.alias for m in models[:3])
            if len(models) > 3:
                model_names += f" +{len(models) - 3}"
            model_line = f"[bold green]{model_names}[/bold green]"
        else:
            model_line = "[dim]未配置 — 输入 [bold cyan]/setup[/bold cyan] 开始配置[/dim]"

        # ── 随机提示 ──
        tips = [
            "输入 [bold cyan]/help[/bold cyan] 查看所有可用命令",
            "输入 [bold cyan]/model[/bold cyan] 切换 AI 模型",
            "输入 [bold cyan]/mode[/bold cyan] 切换思考范式（direct/react/plan-execute）",
            "输入 [bold cyan]/setup[/bold cyan] 运行首次配置向导",
            "按 [bold cyan]Shift+Enter[/bold cyan] 可以输入多行内容",
            "输入 [bold cyan]/verbose[/bold cyan] 开启详细日志模式",
            "输入 [bold cyan]/tools[/bold cyan] 查看所有可用工具",
            "输入 [bold cyan]/mcp[/bold cyan] 管理 MCP 扩展服务器",
        ]
        tip = random.choice(tips)

        # ── 构建欢迎面板 ──
        logo_art = "\n".join(logo)
        content = f"""{logo_art}

  [dim]{version}[/dim]  ·  [bold white]Multi-Model AI Coding Assistant[/bold white]

  [bold]范式[/bold]    {mode.name} — {mode.description}
  [bold]模型[/bold]    {model_line}

  [bold yellow]提示[/bold yellow]    {tip}

  [dim]Ctrl+C 退出  ·  Shift+Enter 换行  ·  Enter 发送[/dim]"""

        console.print()
        
        console.print(Panel(content, border_style="cyan", padding=(0, 2)))
        console.print()

    def _read_input(self) -> str:
        """读取用户输入。Shift+Enter / Alt+Enter 换行，Enter 发送。

        Windows: msvcrt.getwch() + GetAsyncKeyState 检测 Shift，
                 Console API 操作光标（兼容所有 Windows 终端）。
        Linux/macOS: termios 原始模式，支持 Alt+Enter 换行、
                 方向键、Home/End、粘贴多行内容。
        """
        import sys

        if sys.platform != "win32":
            return self._read_input_unix()

        # ── Windows: 逐字符读取 ──
        import ctypes
        import ctypes.wintypes as wt
        import msvcrt

        VK_SHIFT = 0x10
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        GetAsyncKeyState = user32.GetAsyncKeyState
        GetAsyncKeyState.argtypes = [ctypes.c_int]
        GetAsyncKeyState.restype = ctypes.c_short

        # Console API 用于可靠地删除字符
        STD_OUTPUT_HANDLE = -11
        h_stdout = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)

        class COORD(ctypes.Structure):
            _fields_ = [("X", wt.SHORT), ("Y", wt.SHORT)]

        class SMALL_RECT(ctypes.Structure):
            _fields_ = [("Left", wt.SHORT), ("Top", wt.SHORT), ("Right", wt.SHORT), ("Bottom", wt.SHORT)]

        class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
            _fields_ = [
                ("dwSize", COORD),
                ("dwCursorPosition", COORD),
                ("wAttributes", wt.WORD),
                ("srWindow", SMALL_RECT),
                ("dwMaximumWindowSize", COORD),
            ]

        SetConsoleCursorPosition = kernel32.SetConsoleCursorPosition
        SetConsoleCursorPosition.argtypes = [wt.HANDLE, COORD]
        SetConsoleCursorPosition.restype = wt.BOOL

        FillConsoleOutputCharacterW = kernel32.FillConsoleOutputCharacterW
        FillConsoleOutputCharacterW.argtypes = [wt.HANDLE, wt.WCHAR, wt.DWORD, COORD, ctypes.POINTER(wt.DWORD)]
        FillConsoleOutputCharacterW.restype = wt.BOOL

        GetConsoleScreenBufferInfo = kernel32.GetConsoleScreenBufferInfo
        GetConsoleScreenBufferInfo.argtypes = [wt.HANDLE, ctypes.POINTER(CONSOLE_SCREEN_BUFFER_INFO)]
        GetConsoleScreenBufferInfo.restype = wt.BOOL

        def shift_held() -> bool:
            return bool(GetAsyncKeyState(VK_SHIFT) & 0x8000)

        def get_cursor_pos() -> COORD:
            info = CONSOLE_SCREEN_BUFFER_INFO()
            GetConsoleScreenBufferInfo(h_stdout, ctypes.byref(info))
            return info.dwCursorPosition

        def move_cursor(pos: COORD) -> None:
            SetConsoleCursorPosition(h_stdout, pos)

        def erase_char(ch: str) -> None:
            """删除一个字符。ASCII 用 ANSI（快），CJK 用 Console API（正确覆盖 2 列宽）。"""
            if ord(ch) > 0x7F:
                # CJK 等宽字符：占 2 列，用 Console API 覆盖
                pos = get_cursor_pos()
                if pos.X >= 2:
                    new_pos = COORD(pos.X - 2, pos.Y)
                    move_cursor(new_pos)
                    written = wt.DWORD(0)
                    FillConsoleOutputCharacterW(h_stdout, ' ', 2, new_pos, ctypes.byref(written))
            else:
                # ASCII：ANSI 一次搞定
                sys.stdout.write("\b \b")
                sys.stdout.flush()

        sys.stdout.write("\n\033[1;36mYou\033[0m: ")
        sys.stdout.flush()

        lines: list[str] = []
        current_line: list[str] = []
        cursor_pos: int = 0  # 光标在 current_line 中的位置索引

        def _redraw_from_cursor() -> None:
            """从光标位置重绘到行尾。"""
            # 打印光标右侧的所有字符
            tail = "".join(current_line[cursor_pos:])
            if tail:
                sys.stdout.write(tail)
            # 清除行尾残留字符（多出一个空格用于覆盖）
            sys.stdout.write(" ")
            # 把光标移回到正确位置
            back = len(tail) + 1
            if back > 0:
                sys.stdout.write(f"\033[{back}D")
            sys.stdout.flush()

        while True:
            ch = msvcrt.getwch()

            if ch in ('\r', '\n'):
                if shift_held():
                    # 多行模式：跳到行尾再换行
                    if cursor_pos < len(current_line):
                        sys.stdout.write(f"\033[{len(current_line) - cursor_pos}C")
                        cursor_pos = len(current_line)
                    lines.append("".join(current_line))
                    current_line = []
                    cursor_pos = 0
                    sys.stdout.write("\n\033[90m...\033[0m ")
                    sys.stdout.flush()
                else:
                    # 跳到行尾再回车（避免残留）
                    if cursor_pos < len(current_line):
                        sys.stdout.write(f"\033[{len(current_line) - cursor_pos}C")
                    break

            elif ch == '\x03':
                raise KeyboardInterrupt

            elif ch in ('\x08', '\x7f'):
                # Backspace: 删除光标左侧字符
                if cursor_pos > 0:
                    deleted = current_line.pop(cursor_pos - 1)
                    cursor_pos -= 1
                    # 光标左移一格
                    sys.stdout.write('\033[1D')
                    # 重绘后面的字符并清除行尾
                    _redraw_from_cursor()

            elif ch in ('\x00', '\xe0'):
                # 扩展键序列（方向键、Home/End 等）
                second = msvcrt.getwch()
                if second == 'K':        # ← 左箭头
                    if cursor_pos > 0:
                        cursor_pos -= 1
                        sys.stdout.write('\033[1D')
                        sys.stdout.flush()
                elif second == 'M':      # → 右箭头
                    if cursor_pos < len(current_line):
                        cursor_pos += 1
                        sys.stdout.write('\033[1C')
                        sys.stdout.flush()
                elif second == 'H':      # Home
                    if cursor_pos > 0:
                        sys.stdout.write(f"\033[{cursor_pos}D")
                        cursor_pos = 0
                        sys.stdout.flush()
                elif second == 'O':      # End
                    if cursor_pos < len(current_line):
                        sys.stdout.write(f"\033[{len(current_line) - cursor_pos}C")
                        cursor_pos = len(current_line)
                        sys.stdout.flush()
                elif second == 'S':      # Delete
                    if cursor_pos < len(current_line):
                        current_line.pop(cursor_pos)
                        _redraw_from_cursor()

            elif ch and ord(ch) >= 0x20:
                # 可见字符：在光标位置插入
                if cursor_pos >= len(current_line):
                    # 追加到末尾（常见情况，快速路径）
                    current_line.append(ch)
                    sys.stdout.write(ch)
                    sys.stdout.flush()
                else:
                    # 插入到中间位置
                    current_line.insert(cursor_pos, ch)
                    # 重绘插入点之后的内容
                    _redraw_from_cursor()
                cursor_pos += 1

        if current_line:
            lines.append("".join(current_line))

        result = "\n".join(lines)
        sys.stdout.write("\n")
        return result.strip()

    @staticmethod
    def _read_input_unix() -> str:
        """Linux/macOS 原始终端输入：支持 Alt+Enter 换行，方向键编辑。

        使用时将终端设为原始模式，逐字节读取并解析 ANSI 转义序列。
        粘贴多行文本会被自动检测并正确处理。
        """
        import sys
        import termios
        import tty
        import unicodedata
        from select import select

        PROMPT = "\033[1;36mYou\033[0m: "
        CONTINUATION = "\033[90m...\033[0m "

        # ── 显示宽度计算（CJK 字符占 2 列）────────────────
        def _char_width(ch: str) -> int:
            """返回单个字符的终端显示宽度。"""
            ea = unicodedata.east_asian_width(ch)
            if ea in ('W', 'F'):
                return 2
            return 1

        def _display_width(s: str) -> int:
            """计算字符串的终端显示宽度。"""
            return sum(_char_width(ch) for ch in s)

        def _prompt_printable(prompt_str: str) -> str:
            """剥离 ANSI 转义序列，得到 prompt 的可打印文本。"""
            import re
            return re.sub(r'\033\[[0-9;]*[a-zA-Z]', '', prompt_str)

        def _line_width_upto(chars: list[str], upto: int) -> int:
            """计算 current_line 中前 upto 个字符的终端显示宽度。"""
            return _display_width("".join(chars[:upto]))

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)

            lines: list[str] = []
            current_line: list[str] = []
            cursor_pos: int = 0
            prompt_active = True

            def _redraw_line() -> None:
                """清除当前行并重绘（正确处理 CJK 宽字符）。"""
                nonlocal prompt_active
                prompt_str = PROMPT if (prompt_active and not lines) else CONTINUATION
                # 光标移到行首并清除到行尾
                sys.stdout.write("\r\033[K")
                # 打印提示符和当前行内容
                sys.stdout.write(prompt_str)
                sys.stdout.write("".join(current_line))
                # 光标定位：回到行首 + 提示符宽度 + 光标前内容的显示宽度
                pw = _display_width(_prompt_printable(prompt_str))
                prefix_w = _display_width("".join(current_line[:cursor_pos]))
                sys.stdout.write(f"\r\033[{pw + prefix_w}C")
                sys.stdout.flush()

            def _prompt_width() -> int:
                """当前提示符的可打印宽度。"""
                prompt_str = PROMPT if (prompt_active and not lines) else CONTINUATION
                return _display_width(_prompt_printable(prompt_str))

            def _move_cursor_to(target: int) -> None:
                """移动光标到目标字符位置（正确处理 CJK 宽字符列偏移）。"""
                nonlocal cursor_pos
                cursor_pos = max(0, min(target, len(current_line)))
                pw = _prompt_width()
                prefix_w = _display_width("".join(current_line[:cursor_pos]))
                sys.stdout.write(f"\r\033[{pw + prefix_w}C")
                sys.stdout.flush()

            # 显示初始提示符
            sys.stdout.write(PROMPT)
            sys.stdout.flush()

            # 缓冲区，用于累积多字节序列
            seq_buffer = ""

            while True:
                # 用 select 检查是否有输入（超时处理粘贴检测）
                if select([sys.stdin], [], [], 0.01)[0]:
                    ch = sys.stdin.read(1)
                else:
                    continue

                # 处理转义序列
                if seq_buffer or ch == '\x1b':
                    seq_buffer += ch
                    if len(seq_buffer) == 1 and ch == '\x1b':
                        continue  # 等待更多字节

                    # 尝试匹配已知序列
                    # Alt+Enter: \x1b\r
                    if seq_buffer == '\x1b\r':
                        # 插入换行
                        lines.append("".join(current_line))
                        current_line = []
                        cursor_pos = 0
                        sys.stdout.write("\r\n")
                        sys.stdout.write(CONTINUATION)
                        sys.stdout.flush()
                        seq_buffer = ""
                        continue

                    # 方向键: \x1b[A (上), \x1b[B (下), \x1b[C (右), \x1b[D (左)
                    if seq_buffer == '\x1b[A':    # Up — 忽略
                        seq_buffer = ""
                        continue
                    if seq_buffer == '\x1b[B':    # Down — 忽略
                        seq_buffer = ""
                        continue
                    if seq_buffer == '\x1b[C':    # Right
                        if cursor_pos < len(current_line):
                            cursor_pos += 1
                            _move_cursor_to(cursor_pos)
                        seq_buffer = ""
                        continue
                    if seq_buffer == '\x1b[D':    # Left
                        if cursor_pos > 0:
                            cursor_pos -= 1
                            _move_cursor_to(cursor_pos)
                        seq_buffer = ""
                        continue

                    # Home: \x1b[H 或 \x1b[1~
                    if seq_buffer in ('\x1b[H', '\x1b[1~', '\x1bOH'):
                        _move_cursor_to(0)
                        seq_buffer = ""
                        continue

                    # End: \x1b[F 或 \x1b[4~ 或 \x1bOF
                    if seq_buffer in ('\x1b[F', '\x1b[4~', '\x1bOF'):
                        _move_cursor_to(len(current_line))
                        seq_buffer = ""
                        continue

                    # Delete: \x1b[3~
                    if seq_buffer == '\x1b[3~':
                        if cursor_pos < len(current_line):
                            current_line.pop(cursor_pos)
                            _redraw_line()
                        seq_buffer = ""
                        continue

                    # 未知转义序列 — 静默丢弃或超时后当作普通字符
                    # 如果序列长度 >= 8 或超时，丢弃
                    if len(seq_buffer) >= 8:
                        seq_buffer = ""
                        continue
                    # 否则继续累积
                    continue

                # ── 普通字符处理 ──

                if ch in ('\r', '\n'):
                    # Enter → 提交
                    # 将光标移到行尾
                    _move_cursor_to(len(current_line))
                    sys.stdout.write("\r\n")
                    sys.stdout.flush()
                    break

                elif ch == '\x03':   # Ctrl+C
                    sys.stdout.write("\r\n")
                    sys.stdout.flush()
                    raise KeyboardInterrupt

                elif ch == '\x04':   # Ctrl+D
                    if not current_line and not lines:
                        # 空行 Ctrl+D → EOF
                        sys.stdout.write("\r\n")
                        sys.stdout.flush()
                        raise KeyboardInterrupt
                    # 否则当 Delete 处理
                    if cursor_pos < len(current_line):
                        current_line.pop(cursor_pos)
                        _redraw_line()

                elif ch in ('\x7f', '\x08'):  # Backspace
                    if cursor_pos > 0:
                        current_line.pop(cursor_pos - 1)
                        _move_cursor_to(cursor_pos - 1)
                        _redraw_line()

                elif ch == '\t':     # Tab → 4 空格
                    for _ in range(4):
                        current_line.insert(cursor_pos, ' ')
                    cursor_pos += 4
                    _move_cursor_to(cursor_pos)
                    _redraw_line()

                elif ord(ch) >= 0x20:
                    # 可见字符
                    current_line.insert(cursor_pos, ch)
                    cursor_pos += 1
                    _redraw_line()

            if current_line:
                lines.append("".join(current_line))

            return "\n".join(lines)

        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

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
            console.print(Panel(output, title=f"[bold]{cmd_name}[/bold]", border_style="dim", padding=(0, 1)))
        return False

    def _sync_context_window(self, model_aliases: list[str]) -> None:
        """R4: 按激活模型的实际上下文窗口校准 ContextManager.max_tokens。

        取激活模型中 context_window 的最小值（瓶颈模型），保证最小窗口模型
        也不会超限；均未配置则保持默认。替代原先 128000 硬编码——8k 模型时
        needs_compact 永不触发（实际已超限），1M 模型时过早压缩。
        """
        window = self.registry.context_window_for(model_aliases)
        if window > 0:
            self.ctx_mgr.max_tokens = window

    def _handle_chat(self, user_input: str) -> None:
        """处理多轮对话，支持 prompt 优化和多种思考范式。"""
        # P2-修复2: 空输入防护 — 避免空 user 消息污染 history + 浪费 LLM token
        # run() 主循环 line 165 也有防护，但 _handle_chat 是独立可调用的方法，
        # 直接调（如测试或 API 入口）时无防护会 add_user_message("") 进入完整流程
        if not user_input or not user_input.strip():
            console.print("[dim]· 空输入已忽略[/dim]")
            return
        # R4: 按激活模型上下文窗口校准 token 阈值（须在 needs_compact 之前）
        self._sync_context_window(self.registry.get_role_priority("planner"))
        # 自动 compact 检查
        if self.ctx_mgr.needs_compact():
            console.print("[dim]· 对话较长，建议 [bold cyan]/compact[/bold cyan] 压缩[/dim]")

        # 保存 undo 快照
        self.ctx_mgr.save_snapshot()

        # ── 项目上下文注入（首次对话时） ──────────────────
        self._inject_project_context()

        # ── 记忆注入 ──────────────────────────────────
        self._inject_memories(user_input)

        # ── Prompt 优化（按需） ──────────────────────────
        # 意图检测始终执行（detect_intent 纯正则，开销可忽略）：供 direct 模式路由使用——
        # query 意图（天气/价格/汇率/新闻等实时数据）必然需要工具，direct 模式不向 API
        # 传工具，须路由到 ReAct（见 _detect_tool_need）。
        intent = self._detect_intent(user_input)
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

        # 获取模型列表
        model_ids = self.registry.get_role_priority("planner")
        if not model_ids:
            console.print("[red]· 未配置模型，请先 [bold cyan]/set_model[/bold cyan] 添加[/red]")
            return

        # 注入对话历史到 AgentContext，供引擎使用
        self.agent_context.set_conversation_messages(self.ctx_mgr.get_messages())

        # 根据当前思考范式选择执行方式
        mode = self.registry.current_mode

        try:
            if mode == "react":
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
                self._run_direct(optimized, model_ids, intent=intent)
        except KeyboardInterrupt:
            # B2: Ctrl+C 取消当前运行，返回提示符而非退出整个 REPL
            console.print("\n[dim]· 已中断，返回提示符[/dim]")

    def _run_direct(self, user_input: str, model_ids: list[str], intent: str | None = None) -> None:
        """直接对话模式。自动检测工具需求并委派给 ReAct 引擎。"""
        # 检测是否需要工具执行（编程/文件/命令任务，或 query 意图实时数据查询）
        if self._detect_tool_need(user_input, intent=intent):
            if intent == "query":
                console.print("[cyan]🔧 检测到信息查询（需实时数据），自动切换到 ReAct 模式...[/cyan]")
            else:
                console.print("[cyan]🔧 检测到需要工具执行，自动切换到 ReAct 模式...[/cyan]")
            self._run_react_engine(user_input, model_ids)
            return

        messages = self.ctx_mgr.get_messages()

        last_error = None
        for model_id in model_ids:
            try:
                if self.streaming:
                    response_text = self._stream_response(model_id, messages)
                else:
                    response_text = self._blocking_response(model_id, messages)
                self.status_bar.set_last_model(model_id)

                if response_text:
                    # ── 响应后验证 1：检测 LLM 是否声称执行了文件操作 ──
                    if self._detect_file_claim(response_text):
                        console.print()
                        console.print("[cyan]🔧 检测到 LLM 声称执行了操作但未使用工具，自动切换到 ReAct 模式重新执行...[/cyan]")
                        self.ctx_mgr.trim_last_assistant()
                        # P2-修复6 (观察项-1)：防御性 catch ——
                        # _run_react_engine 内部已加占位（修复5），但万一占位也失败
                        # （如 ctx_mgr 内部异常），这里再兜底一次防 user-only 序列。
                        try:
                            self._run_react_engine(user_input, model_ids)
                        except Exception as e:
                            console.print(f"[error]❌ ReAct 重试失败: {e}[/error]")
                            try:
                                self.ctx_mgr.add_assistant_message(
                                    f"[错误] ReAct 重试失败: {e}", model_used=model_ids[0],
                                )
                            except Exception:
                                pass
                        return

                    # ── 响应后验证 2：检测 LLM 是否回复了拒绝性内容 ──
                    if self._detect_denial(response_text):
                        console.print()
                        console.print("[dim]· LLM 无法完成任务 → ReAct 模式重试[/dim]")
                        self.ctx_mgr.trim_last_assistant()
                        # P2-修复6 (观察项-1)：与 file_claim 同根问题，同样防御性 catch
                        try:
                            self._run_react_engine(user_input, model_ids)
                        except Exception as e:
                            console.print(f"[error]❌ ReAct 重试失败: {e}[/error]")
                            try:
                                self.ctx_mgr.add_assistant_message(
                                    f"[错误] ReAct 重试失败: {e}", model_used=model_ids[0],
                                )
                            except Exception:
                                pass
                        return

                return
            except Exception as e:
                last_error = e
                console.print(f"[yellow]模型 {model_id} 失败: {e}，尝试下一个...[/yellow]")

        console.print(f"[error]❌ 所有模型均调用失败: {last_error}[/error]")

    def _run_react_engine(self, user_input: str, model_ids: list[str]) -> None:
        """ReAct 引擎模式。"""
        from omniagent.engine.react_engine import ReActEngine

        console.print("[dim]· ReAct 思考 → 行动 → 观察[/dim]")

        callback = self._make_callback()
        engine = ReActEngine(model_priority=model_ids, max_iterations=10, callback=callback, model_configs=dict(self.registry.models))
        try:
            result = engine.run(user_input, self.agent_context, ctx_mgr=self.ctx_mgr)
            self.ctx_mgr.add_assistant_message(result, model_used=model_ids[0])
            self._render_engine_result(callback, result, "ReAct 结果")
            self.status_bar.set_last_model(model_ids[0])
        except Exception as e:
            # P2-修复5: 引擎异常时清理 user 消息（repl.py:745 已 add 但无 assistant
            # 响应会留孤立），优先 add_assistant_message 占位错误消息，让 history 仍成对；
            # add_assistant_message 失败时回退 trim user 消息。
            console.print(f"[error]❌ ReAct 引擎执行失败: {e}[/error]")
            try:
                # 用 "[错误] ..." 作为 assistant 回应占位，让 history 仍成对
                self.ctx_mgr.add_assistant_message(
                    f"[错误] ReAct 引擎执行失败: {e}", model_used=model_ids[0],
                )
            except Exception:
                # 兜底：add_assistant_message 失败时回退 trim user
                try:
                    self.ctx_mgr.trim_last_user()
                except Exception:
                    pass

    def _run_plan_execute_engine(self, user_input: str, model_ids: list[str]) -> None:
        """Plan-Execute 引擎模式。"""
        from omniagent.engine.plan_execute_engine import PlanExecuteEngine

        console.print("[dim]· Plan-Execute 规划 → 逐步执行[/dim]")

        callback = self._make_callback()
        engine = PlanExecuteEngine(model_priority=model_ids, max_steps=20, callback=callback, model_configs=dict(self.registry.models))
        try:
            result = engine.run(user_input, self.agent_context, ctx_mgr=self.ctx_mgr)
            self.ctx_mgr.add_assistant_message(result, model_used=model_ids[0])
            self._render_engine_result(callback, result, "Plan-Execute 结果")
            self.status_bar.set_last_model(model_ids[0])
        except Exception as e:
            console.print(f"[error]❌ Plan-Execute 引擎执行失败: {e}[/error]")

    def _run_reflection_engine(self, user_input: str, model_ids: list[str]) -> None:
        """Reflection 引擎模式。"""
        from omniagent.engine.reflection_engine import ReflectionEngine

        console.print("[dim]· Reflection 执行 → 审查 → 修正[/dim]")

        callback = self._make_callback()
        engine = ReflectionEngine(model_priority=model_ids, max_rounds=3, callback=callback, model_configs=dict(self.registry.models))
        try:
            result = engine.run(user_input, context=self.agent_context, ctx_mgr=self.ctx_mgr)
            self.ctx_mgr.add_assistant_message(result, model_used=model_ids[0])
            self._render_engine_result(callback, result, "Reflection 结果")
            self.status_bar.set_last_model(model_ids[0])
        except Exception as e:
            console.print(f"[error]❌ Reflection 引擎执行失败: {e}[/error]")

    def _run_plan_react_engine(self, user_input: str, model_ids: list[str]) -> None:
        """Plan + React 组合引擎模式。"""
        from omniagent.engine.combined_engines import PlanReactEngine

        console.print("[dim]· Plan+React 全局规划 → 每步 ReAct 执行[/dim]")

        callback = self._make_callback()
        engine = PlanReactEngine(model_priority=model_ids, max_steps=10, react_iterations=8, callback=callback, model_configs=dict(self.registry.models))
        try:
            result = engine.run(user_input, context=self.agent_context, ctx_mgr=self.ctx_mgr)
            self.ctx_mgr.add_assistant_message(result, model_used=model_ids[0])
            self._render_engine_result(callback, result, "Plan+React 结果")
            self.status_bar.set_last_model(model_ids[0])
        except Exception as e:
            console.print(f"[error]❌ Plan+React 引擎执行失败: {e}[/error]")

    def _run_plan_reflection_engine(self, user_input: str, model_ids: list[str]) -> None:
        """Plan + Reflection 组合引擎模式。"""
        from omniagent.engine.combined_engines import PlanReflectionEngine

        console.print("[dim]· Plan+Reflection 规划执行 → 反思修正[/dim]")

        callback = self._make_callback()
        engine = PlanReflectionEngine(model_priority=model_ids, max_steps=10, review_rounds=2, callback=callback, model_configs=dict(self.registry.models))
        try:
            result = engine.run(user_input, context=self.agent_context, ctx_mgr=self.ctx_mgr)
            self.ctx_mgr.add_assistant_message(result, model_used=model_ids[0])
            self._render_engine_result(callback, result, "Plan+Reflection 结果")
            self.status_bar.set_last_model(model_ids[0])
        except Exception as e:
            console.print(f"[error]❌ Plan+Reflection 引擎执行失败: {e}[/error]")

    def _run_react_reflection_engine(self, user_input: str, model_ids: list[str]) -> None:
        """ReAct + Reflection 组合引擎模式。"""
        from omniagent.engine.combined_engines import ReactReflectionEngine

        console.print("[dim]· React+Reflection 探索 → 反思审查[/dim]")

        callback = self._make_callback()
        engine = ReactReflectionEngine(model_priority=model_ids, react_iterations=8, review_rounds=2, callback=callback, model_configs=dict(self.registry.models))
        try:
            result = engine.run(user_input, context=self.agent_context, ctx_mgr=self.ctx_mgr)
            self.ctx_mgr.add_assistant_message(result, model_used=model_ids[0])
            self._render_engine_result(callback, result, "React+Reflection 结果")
            self.status_bar.set_last_model(model_ids[0])
        except Exception as e:
            console.print(f"[error]❌ React+Reflection 引擎执行失败: {e}[/error]")

    def _run_novel_engine(self, user_input: str, model_ids: list[str]) -> None:
        """小说创作引擎模式（支持多小说隔离）。"""
        from omniagent.engine.novel_engine import NovelEngine

        console.print("[dim]· Novel 小说创作模式[/dim]")

        callback = self._make_callback()
        engine = NovelEngine(
            model_priority=model_ids,
            max_iterations=15,
            callback=callback,
            novel_manager=self._novel_manager,
            model_configs=dict(self.registry.models),
        )
        try:
            result = engine.run(user_input, context=self.agent_context, ctx_mgr=self.ctx_mgr)
            self.ctx_mgr.add_assistant_message(result, model_used=model_ids[0])
            self._render_engine_result(callback, result, "Novel 创作结果", border_style="magenta")
            self.status_bar.set_last_model(model_ids[0])
        except Exception as e:
            console.print(f"[error]❌ 小说创作引擎执行失败: {e}[/error]")

    def _stream_response(self, model_id: str, messages: list[dict[str, str]]) -> str:
        """流式输出模型回复，完成后 Markdown 渲染。返回完整响应文本。"""
        from omniagent.utils.llm_client import chat_completion_stream
        from rich.live import Live
        from rich.spinner import Spinner

        full_response = []

        # 流式阶段：显示 spinner + 实时 token 计数
        with Live(
            Spinner("dots", text="[dim]思考中…[/dim]"),
            console=console,
            refresh_per_second=10,
            transient=True,  # 结束后自动清除 spinner
        ) as live:
            for chunk in chat_completion_stream(model_id, messages):
                full_response.append(chunk)
                token_count = len("".join(full_response))
                live.update(
                    Spinner("dots", text=f"[dim]生成中… {token_count} tokens[/dim]")
                )

        response_text = "".join(full_response)

        # 流式完成后，用 Markdown Panel 渲染最终结果
        if response_text.strip():
            console.print(Panel(
                Markdown(response_text),
                title=f"[bold]Assistant[/bold] [dim]({model_id})[/dim]",
                border_style="green",
                padding=(0, 1),
            ))

        self.ctx_mgr.add_assistant_message(response_text, model_used=model_id)
        return response_text

    def _blocking_response(self, model_id: str, messages: list[dict[str, str]]) -> str:
        """阻塞式输出模型回复。返回响应文本。"""
        from omniagent.utils.llm_client import chat_completion

        console.print(f"[dim]· 调用 {model_id}…[/dim]")
        response = chat_completion(model_id, messages)

        self.ctx_mgr.add_assistant_message(response, model_used=model_id)

        console.print()
        console.print(Panel(
            Markdown(response),
            title=f"[assistant]Assistant[/assistant] [dim]({model_id})[/dim]",
            border_style="green",
        ))
        return response

    @staticmethod
    def _detect_intent(text: str) -> str | None:
        """检测用户意图。"""
        from omniagent.repl.prompt_optimizer import detect_intent
        return detect_intent(text)

    # ── 工具需求检测 ──────────────────────────────────────────
    # 只匹配明确的编程/文件/命令任务，不再枚举各种查询类型
    _TOOL_PATTERNS: list[re.Pattern[str]] = [
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
        # 命令执行（含代词：执行它/运行这个/跑一下）
        re.compile(r"(?:执行|运行|跑).{0,15}(?:命令|脚本|程序|命令行|测试|pytest|npm|pip|python|node)", re.I),
        re.compile(r"(?:执行|运行|跑|试试).{0,5}(?:它|他|她|这个|一下|看看|试试)", re.I),
        re.compile(r"(?:试试|试下).{0,3}(?:执行|运行|跑)", re.I),
        re.compile(r"(?:run|execute|exec).{0,15}(?:command|script|cmd|test|pytest|npm|pip|python|node|it|this)", re.I),
        re.compile(r"(?:run|execute|exec)\s+it", re.I),
        # Git 操作
        re.compile(r"\bgit\b.{0,20}(?:commit|push|pull|add|clone|checkout|branch|merge|stash)", re.I),
        re.compile(r"(?:提交|推送|拉取|克隆|分支|合并)", re.I),
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
    def _detect_tool_need(cls, text: str, intent: str | None = None) -> bool:
        """检测用户输入是否明确需要工具执行。

        - ``query`` 意图（天气/价格/汇率/新闻等实时数据）：必然需要工具。direct 模式
          不向 API 传工具，而 prompt_optimizer 会向其注入"使用工具获取实时数据"指令，
          LLM 无工具可调时只能给出前言式回复（如"我来帮你查询…"）而非真实数据，
          故 query 意图直接判 True，路由到 ReAct。
        - 其余意图：仅匹配编程/文件/命令类正则（``_TOOL_PATTERNS``）。
        """
        # P2-修复1: write_code 意图同样必然需要工具（写代码到文件 / 落盘执行），
        # 与 query 同根：_TOOL_PATTERNS 中编程类正则要求"帮我/请/给"前缀，
        # 无法覆盖"写一个 X"/"用 Y 写一个 Z"等自然语序。
        if intent in ("query", "write_code"):
            return True
        for pattern in cls._TOOL_PATTERNS:
            if pattern.search(text):
                return True
        return False

    _FILE_CLAIM_KEYWORDS: list[str] = [
        "已创建", "已经创建", "已生成", "已经生成", "已写入", "已经写入",
        "已保存", "已经保存", "已新建", "已经新建", "已建立", "已经建立",
        "创建了", "生成了", "写入了", "保存了", "新建了",
        "created", "written", "saved", "generated",
        "文件已", "目录已", "文件夹已",
    ]

    # LLM 拒绝性回复的关键词 — 表示它不知道怎么做，应该切换到 ReAct
    _DENIAL_KEYWORDS: list[str] = [
        "无法直接", "无法获取", "无法查询", "无法访问", "无法提供",
        "不能直接", "不能获取", "不能查询", "不能访问",
        "没有连接", "没有接入", "没有访问",
        "不具备", "不支持直接",
        "无法实时", "无法获取实时",
        "I cannot", "I can't", "I'm unable",
        "I don't have access", "I'm not able",
    ]

    @classmethod
    def _detect_file_claim(cls, text: str) -> bool:
        """检测 LLM 回复中是否声称执行了文件操作。"""
        text_lower = text.lower()
        return any(kw in text_lower for kw in cls._FILE_CLAIM_KEYWORDS)

    @classmethod
    def _detect_denial(cls, text: str) -> bool:
        """检测 LLM 是否回复了拒绝性内容（表示它无法完成任务）。"""
        return any(kw in text for kw in cls._DENIAL_KEYWORDS)

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
                console.print(f"[dim]· 已加载 {len(sm.list_all())} 个快捷指令[/dim]")
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
                console.print(f"[dim]· 已加载 {len(skm.list_all())} 个技能[/dim]")
        except Exception as e:
            logger.debug(f"加载技能失败: {e}")


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
