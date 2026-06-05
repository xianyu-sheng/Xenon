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
        self.ctx_mgr = ctx_mgr or ContextManager()
        self.agent_context = AgentContext()
        self.system_prompt = system_prompt or self._default_system_prompt()
        self.streaming = streaming
        self.optimize_prompts = optimize_prompts
        self.verbose = verbose

        # 项目上下文
        self.project_ctx = ProjectContext()
        self._project_injected = False

        # 状态栏
        self.status_bar = StatusBar(console, self.ctx_mgr, self.registry)

        # 会话状态，供命令处理器共享
        self._session_state: dict[str, Any] = {
            "agent_context": self.agent_context,
            "_repl": self,
        }

    def _make_callback(self):
        """根据 verbose 状态创建引擎回调。"""
        from omniagent.engine.callbacks import ConsoleCallback
        return ConsoleCallback(verbose=self.verbose)

    @staticmethod
    def _default_system_prompt() -> str:
        return (
            "你是 OmniAgent-CLI 的 AI 编程助手。"
            "你可以帮助用户编写代码、调试问题、解释概念。"
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
                console.print("\n[yellow]再见！[/yellow]")
                break

            if not user_input:
                continue

            # 斜杠命令
            if user_input.startswith("/"):
                if self._handle_command(user_input):
                    console.print("[yellow]再见！[/yellow]")
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
            console.print("[yellow]检测到尚未配置任何 API Key[/yellow]")
            console.print("  输入 [bold cyan]/setup[/bold cyan] 进入配置向导\n")
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
                console.print(f"[green]已自动加载 {len(models)} 个模型[/green]")
                console.print("  输入 [bold cyan]/model[/bold cyan] 可切换模型\n")

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
        """读取用户输入。Shift+Enter 换行，Enter 发送。

        Windows: msvcrt.getwch() + GetAsyncKeyState 检测 Shift，
                 Console API 操作光标（兼容所有 Windows 终端）。
        POSIX:   input() 回退（不支持 Shift+Enter）。
        """
        import sys

        if sys.platform != "win32":
            try:
                return input("\033[1;36mYou\033[0m: ")
            except EOFError:
                raise KeyboardInterrupt

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

        # ── Prompt 优化（按需） ──────────────────────────
        if self.optimize_prompts:
            optimized, system_hint, was_optimized = optimize_prompt(user_input)
            intent = self._detect_intent(user_input)
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
            console.print("[error]❌ 未配置任何模型。请先使用 /set_model 添加模型。[/error]")
            return

        # 注入对话历史到 AgentContext，供引擎使用
        self.agent_context.set_conversation_messages(self.ctx_mgr.get_messages())

        # 根据当前思考范式选择执行方式
        mode = self.registry.current_mode

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
        else:
            # direct 模式 — 直接调 LLM
            self._run_direct(optimized, model_ids)

    def _run_direct(self, user_input: str, model_ids: list[str]) -> None:
        """直接对话模式。自动检测工具需求并委派给 ReAct 引擎。"""
        # 检测是否需要工具执行
        if self._detect_tool_need(user_input):
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

                # ── 响应后验证：检测 LLM 是否声称执行了文件操作 ──
                if response_text and self._detect_file_claim(response_text):
                    console.print()
                    console.print("[cyan]🔧 检测到 LLM 声称执行了操作但未使用工具，自动切换到 ReAct 模式重新执行...[/cyan]")
                    # 移除 LLM 的幻觉回复，避免污染上下文
                    self.ctx_mgr.trim_last_assistant()
                    # 用 ReAct 重新执行
                    self._run_react_engine(user_input, model_ids)
                    return

                return
            except Exception as e:
                last_error = e
                console.print(f"[yellow]模型 {model_id} 失败: {e}，尝试下一个...[/yellow]")

        console.print(f"[error]❌ 所有模型均调用失败: {last_error}[/error]")

    def _run_react_engine(self, user_input: str, model_ids: list[str]) -> None:
        """ReAct 引擎模式。"""
        from omniagent.engine.react_engine import ReActEngine

        console.print("[cyan]🔄 ReAct 模式: 思考 → 行动 → 观察 → 循环[/cyan]")

        engine = ReActEngine(model_priority=model_ids, max_iterations=10, callback=self._make_callback())
        try:
            result = engine.run(user_input, self.agent_context)
            self.ctx_mgr.add_assistant_message(result, model_used=model_ids[0])
            console.print(Panel(Markdown(result), title="[command]ReAct 结果[/command]", border_style="green"))
            self.status_bar.set_last_model(model_ids[0])
        except Exception as e:
            console.print(f"[error]❌ ReAct 引擎执行失败: {e}[/error]")

    def _run_plan_execute_engine(self, user_input: str, model_ids: list[str]) -> None:
        """Plan-Execute 引擎模式。"""
        from omniagent.engine.plan_execute_engine import PlanExecuteEngine

        console.print("[cyan]📋 Plan-Execute 模式: 规划 → 逐步执行[/cyan]")

        engine = PlanExecuteEngine(model_priority=model_ids, max_steps=20, callback=self._make_callback())
        try:
            result = engine.run(user_input, self.agent_context)
            self.ctx_mgr.add_assistant_message(result, model_used=model_ids[0])
            console.print(Panel(Markdown(result), title="[command]Plan-Execute 结果[/command]", border_style="green"))
            self.status_bar.set_last_model(model_ids[0])
        except Exception as e:
            console.print(f"[error]❌ Plan-Execute 引擎执行失败: {e}[/error]")

    def _run_reflection_engine(self, user_input: str, model_ids: list[str]) -> None:
        """Reflection 引擎模式。"""
        from omniagent.engine.reflection_engine import ReflectionEngine

        console.print("[cyan]🔍 Reflection 模式: 执行 → 审查 → 修正[/cyan]")

        engine = ReflectionEngine(model_priority=model_ids, max_rounds=3, callback=self._make_callback())
        try:
            result = engine.run(user_input)
            self.ctx_mgr.add_assistant_message(result, model_used=model_ids[0])
            console.print(Panel(Markdown(result), title="[command]Reflection 结果[/command]", border_style="green"))
            self.status_bar.set_last_model(model_ids[0])
        except Exception as e:
            console.print(f"[error]❌ Reflection 引擎执行失败: {e}[/error]")

    def _run_plan_react_engine(self, user_input: str, model_ids: list[str]) -> None:
        """Plan + React 组合引擎模式。"""
        from omniagent.engine.combined_engines import PlanReactEngine

        console.print("[cyan]📋🔄 Plan+React 模式: 全局规划 → 每步 ReAct 执行[/cyan]")

        engine = PlanReactEngine(model_priority=model_ids, max_steps=10, react_iterations=8, callback=self._make_callback())
        try:
            result = engine.run(user_input, context=self.agent_context)
            self.ctx_mgr.add_assistant_message(result, model_used=model_ids[0])
            console.print(Panel(Markdown(result), title="[command]Plan+React 结果[/command]", border_style="green"))
            self.status_bar.set_last_model(model_ids[0])
        except Exception as e:
            console.print(f"[error]❌ Plan+React 引擎执行失败: {e}[/error]")

    def _run_plan_reflection_engine(self, user_input: str, model_ids: list[str]) -> None:
        """Plan + Reflection 组合引擎模式。"""
        from omniagent.engine.combined_engines import PlanReflectionEngine

        console.print("[cyan]📋🔍 Plan+Reflection 模式: 规划执行 → 反思修正[/cyan]")

        engine = PlanReflectionEngine(model_priority=model_ids, max_steps=10, review_rounds=2, callback=self._make_callback())
        try:
            result = engine.run(user_input, context=self.agent_context)
            self.ctx_mgr.add_assistant_message(result, model_used=model_ids[0])
            console.print(Panel(Markdown(result), title="[command]Plan+Reflection 结果[/command]", border_style="green"))
            self.status_bar.set_last_model(model_ids[0])
        except Exception as e:
            console.print(f"[error]❌ Plan+Reflection 引擎执行失败: {e}[/error]")

    def _run_react_reflection_engine(self, user_input: str, model_ids: list[str]) -> None:
        """ReAct + Reflection 组合引擎模式。"""
        from omniagent.engine.combined_engines import ReactReflectionEngine

        console.print("[cyan]🔄🔍 React+Reflection 模式: ReAct 探索 → 反思审查[/cyan]")

        engine = ReactReflectionEngine(model_priority=model_ids, react_iterations=8, review_rounds=2, callback=self._make_callback())
        try:
            result = engine.run(user_input, context=self.agent_context)
            self.ctx_mgr.add_assistant_message(result, model_used=model_ids[0])
            console.print(Panel(Markdown(result), title="[command]React+Reflection 结果[/command]", border_style="green"))
            self.status_bar.set_last_model(model_ids[0])
        except Exception as e:
            console.print(f"[error]❌ React+Reflection 引擎执行失败: {e}[/error]")

    def _stream_response(self, model_id: str, messages: list[dict[str, str]]) -> str:
        """流式输出模型回复。返回完整响应文本。"""
        from omniagent.utils.llm_client import chat_completion_stream

        console.print()
        full_response = []

        for chunk in chat_completion_stream(model_id, messages):
            full_response.append(chunk)
            console.print(chunk, end="")

        console.print()

        response_text = "".join(full_response)
        self.ctx_mgr.add_assistant_message(response_text, model_used=model_id)

        console.print(f"[dim]└─ {model_id}[/dim]")
        return response_text

    def _blocking_response(self, model_id: str, messages: list[dict[str, str]]) -> str:
        """阻塞式输出模型回复。返回响应文本。"""
        from omniagent.utils.llm_client import chat_completion

        console.print(f"[dim]调用 {model_id}...[/dim]")
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
    def _detect_tool_need(cls, text: str) -> bool:
        """检测用户输入是否需要工具执行。"""
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

    @classmethod
    def _detect_file_claim(cls, text: str) -> bool:
        """检测 LLM 回复中是否声称执行了文件操作。"""
        text_lower = text.lower()
        return any(kw in text_lower for kw in cls._FILE_CLAIM_KEYWORDS)

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
