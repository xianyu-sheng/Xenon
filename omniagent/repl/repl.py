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

# ── prompt_toolkit（可选依赖，不可用时回退自建输入）────────────
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.styles import Style
    from prompt_toolkit.formatted_text import HTML
    from pathlib import Path as _Path
    _HISTORY_DIR = _Path.home() / ".omniagent"
    _HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    _HAS_PROMPT_TOOLKIT = True
except ImportError:
    _HAS_PROMPT_TOOLKIT = False

# ── 自定义主题 ────────────────────────────────────────────
_theme = Theme({
    "user": "bold cyan",
    "assistant": "green",
    "system": "dim yellow",
    "error": "bold red",
    "command": "bold magenta",
})

console = Console(theme=_theme)


class _ShiftTabSignal(Exception):
    """由 _read_input_unix 抛出，指示 Shift+Tab 按下，切换思考范式。"""


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

        # v0.4.0: Auto router + model pool (replaces role_priority)
        from omniagent.repl.model_pool import ModelPool
        from omniagent.repl.auto_router import AutoRouter
        self.model_pool = ModelPool()
        self.auto_router = AutoRouter(self.model_pool, context_manager=self.ctx_mgr)
        self.status_bar._auto_router = self.auto_router  # for "auto" display

        # 会话状态，供命令处理器共享
        self._session_state: dict[str, Any] = {
            "agent_context": self.agent_context,
            "_repl": self,
            "_novel_manager": self._novel_manager,
            "model_pool": self.model_pool,
            "auto_router": self.auto_router,
        }

        # v0.3.0+ 修复（C-3）：bash 风格——单次 Ctrl+C 重画 prompt 继续，
        # 连续两次 Ctrl+C 才退出 REPL。修复前空 prompt + Ctrl+C 直接退出，
        # 5/9 终端类型（xterm256color/alacritty/gnome-256color/screen-256color/vt100）
        # 在空行 Ctrl+C 时丢失输入机会。
        self._pending_exit: bool = False

        # v0.5.0: prompt_toolkit 会话（命令历史 + Tab 补全 + 固定状态栏）
        self._pt_session: Any = None
        self._init_prompt_toolkit()

        # v0.5.2: 会话内失败模型集合——一次失败后本会话不再重试
        self._failed_models: set[str] = set()
        self._preferred_model_ids: list[str] = []  # v0.5.3: 用户 -m 指定的模型

        # v0.5.3: 折叠思考过程 — 默认隐藏，Ctrl+O 展开
        self._show_thinking: bool = False
        self._last_thinking_panel: Any = None
        self._captured_log: str = ""       # 引擎执行期间捕获的日志文本
        self._last_mode_line: str = ""     # 上次引擎的模式行

        # v0.6.0: 首轮对话标记 — 用于视觉分隔
        self._first_turn: bool = True

        # v0.5.0: 工具权限门控
        from omniagent.repl.permissions import PermissionGate, PermissionMode
        self._permission_gate = PermissionGate(mode=PermissionMode.DEFAULT)
        self._permission_gate.set_confirm_callback(self._confirm_tool)

    def _init_prompt_toolkit(self) -> None:
        if not _HAS_PROMPT_TOOLKIT:
            return

        from omniagent.repl.completer import OmniCompleter
        cmd_names = list(COMMANDS.keys())
        self._completer = OmniCompleter(cmd_names)

        kb = KeyBindings()

        @kb.add("s-tab")
        def _(event):
            self._handle_shift_tab()
            if hasattr(event, 'app'):
                event.app.invalidate()

        @kb.add("escape", "enter", eager=True)
        def _(event):
            event.current_buffer.insert_text("\n")

        @kb.add("c-o")
        def _(event):
            """v0.5.3: Ctrl+O 切换显示/隐藏上次引擎执行的完整过程。

            折叠 → 展开：重新打印模式行、捕获的日志、推理面板。
            展开 → 折叠：重新打印折叠摘要行。
            """
            self._show_thinking = not self._show_thinking
            console.print()
            if self._show_thinking:
                # ── 展开：重现完整执行过程 ──
                if self._last_mode_line:
                    console.print(f"[dim]{self._last_mode_line}[/dim]")
                if self._captured_log:
                    console.print(self._captured_log, end="")
                if self._last_thinking_panel is not None:
                    console.print(self._last_thinking_panel)
                console.print("[dim]  💭 已展开  [Ctrl+O 折叠][/dim]")
            else:
                # ── 折叠：仅一条摘要行 ──
                panel = self._last_thinking_panel
                if panel is not None:
                    parts = []
                    if panel.steps:
                        parts.append(f"{len(panel.steps)} 步")
                    if panel.tool_call_count:
                        parts.append(f"🔧{panel.tool_call_count}")
                    summary = " · ".join(parts) if parts else "—"
                else:
                    summary = "—"
                console.print(f"[dim]  💭 {summary}  [Ctrl+O][/dim]")
            console.print()
            if hasattr(event, 'app'):
                event.app.invalidate()

        style = Style.from_dict({
            "prompt": "bold #00e5a0",
            "input-box.border": "#555555",
            "input-box.status": "#888888",
            "bottom-toolbar": "#888888",
            "bottom-toolbar.status": "#888888",
        })

        history_path = _HISTORY_DIR / "input_history.txt"

        try:
            self._pt_session = PromptSession(
                history=FileHistory(str(history_path)),
                completer=self._completer,
                key_bindings=kb,
                style=style,
                bottom_toolbar=self._get_pt_bottom_toolbar,
            )
        except Exception:
            logger.debug("prompt_toolkit 初始化失败，回退自建输入", exc_info=True)
            self._pt_session = None

    def _get_pt_toolbar(self) -> str:
        """获取状态栏文本，带缓存避免每次按键都重算。

        prompt_toolkit 的 bottom_toolbar 回调在每次渲染时触发
        （包括每次按键），缓存上次结果避免不必要的字符串拼接。
        """
        try:
            if hasattr(self, 'status_bar') and self.status_bar:
                text = self.status_bar.get_toolbar_text()
                if text != getattr(self, '_cached_toolbar_text', ''):
                    self._cached_toolbar_text = text
                return self._cached_toolbar_text
        except Exception:
            pass
        return ""

    def _get_pt_bottom_toolbar(self):
        """Claude Code 风格：状态行紧接在输入行下方。

        与 _read_input_pt 的双分隔线 + ❯ 提示符配合：

          ──────────────────────────────   ← 底部分隔线（console.print）
          ──────────────────────────────   ← 上分隔线（prompt message）
          ❯ user types here               ← 输入行
            model · react · 12k/128k      ← 状态行（bottom_toolbar）
        """
        status = self._get_pt_toolbar().strip()
        if status:
            return [("class:input-box.status", "  " + status)]
        return []

    def _confirm_tool(self, tool_name: str, params: dict, risk: str) -> tuple[bool, str]:
        import os
        from rich.prompt import Confirm, Prompt
        from omniagent.repl.permissions import PermissionGate

        if not sys.stdin.isatty() or os.environ.get("OMNIAGENT_ASSUME_YES") == "1":
            return True, ""

        msg = PermissionGate.format_confirm_message(tool_name, params, risk)
        console.print()
        console.print(Panel(msg, border_style="yellow", padding=(0, 1)))

        try:
            choice = Prompt.ask(
                "选择", choices=["y", "n", "a", "q"], default="n",
                show_choices=False,
            )
        except (KeyboardInterrupt, EOFError):
            return False, "用户取消"

        if choice == "y":
            return True, ""
        elif choice == "a":
            self._permission_gate.allow_always(tool_name)
            return True, ""
        elif choice == "q":
            return False, "用户取消任务"
        else:
            return False, "用户拒绝"

    def _start_log_capture(self) -> None:
        """v0.5.3: 拦截引擎执行期间的日志输出。

        保存并移除目标 logger 的所有现有 handler，替换为写入内存缓冲区的
        StringIO handler。这样日志不会输出到 stderr，而是被收集供折叠/展开使用。
        """
        import io as _io
        self._log_buffer = _io.StringIO()
        self._log_handler = logging.StreamHandler(self._log_buffer)
        self._log_handler.setFormatter(
            logging.Formatter(
                '%(asctime)s [%(name)s] %(levelname)s: %(message)s',
                datefmt='%H:%M:%S',
            )
        )
        # 第 4 级（ERROR）：只捕获错误级日志进折叠面板，不再刷屏例行 INFO
        # （工具调用/迭代/响应摘要等）。与 main.py 默认 ERROR 一致。
        self._log_handler.setLevel(logging.ERROR)
        # 保存原有 handler 并清空，确保日志只进缓冲区
        self._saved_handlers: dict[str, list[logging.Handler]] = {}
        for _name in [
            'omniagent.engine',
            'omniagent.engine.tool_tracker',
            'omniagent.engine.react_engine',
            'httpx',
        ]:
            _lg = logging.getLogger(_name)
            self._saved_handlers[_name] = list(_lg.handlers)
            _lg.handlers.clear()
            _lg.addHandler(self._log_handler)

    def _stop_log_capture(self) -> str:
        """v0.5.3: 恢复原有 handler，返回捕获的日志文本。"""
        _captured = ""
        try:
            for _name in [
                'omniagent.engine',
                'omniagent.engine.tool_tracker',
                'omniagent.engine.react_engine',
                'httpx',
            ]:
                _lg = logging.getLogger(_name)
                try:
                    _lg.removeHandler(self._log_handler)
                except Exception:
                    pass
                # 恢复原有 handler
                for _h in self._saved_handlers.get(_name, []):
                    try:
                        _lg.addHandler(_h)
                    except Exception:
                        pass
        finally:
            try:
                self._log_handler.close()
            except Exception:
                pass
            try:
                _captured = self._log_buffer.getvalue()
            except Exception:
                pass
        return _captured

    def _make_callback(self):
        """根据 verbose 状态创建引擎回调。"""
        from omniagent.engine.callbacks import ConsoleCallback
        return ConsoleCallback(verbose=self.verbose)

    def _auto_save_session(self) -> None:
        """v0.4.0 Step 14: 退出时自动保存会话状态。"""
        try:
            from omniagent.repl.session import auto_save, cleanup_expired_sessions
            history = [{"role": m["role"], "content": m.get("content", "")}
                       for m in self.ctx_mgr.get_messages() if m.get("role") != "system"]
            context_store = {"mode": self.registry.current_mode}
            model_config = self.model_pool.to_config()

            auto_save(
                history=history,
                context_store=context_store,
                model_config=model_config,
                extra={"paradigm": self.registry.current_mode},
            )
            cleanup_expired_sessions()
        except Exception:
            pass  # 保存失败不影响退出

    def _check_auto_resume(self) -> None:
        """v0.4.0 Step 14: 启动时检查可恢复的会话。"""
        try:
            from omniagent.repl.session import get_auto_session, get_session_age
            data = get_auto_session()
            if data:
                age = get_session_age(data) or "未知时间"
                msgs = len(data.get("history", []))
                mode = data.get("extra", {}).get("paradigm", "direct")
                console.print(
                    f"\n[dim]┌─ 上次会话 ({age}) · {msgs} 条消息 · 范式: {mode}[/dim]"
                )
                console.print("[dim]│  输入 [bold]/resume[/bold] 恢复，或直接开始新对话[/dim]")
        except Exception:
            pass

    def _handle_shift_tab(self) -> None:
        """Shift+Tab 按下：循环切换到下一个可用思维范式。"""
        mode_names = list(self.registry.modes.keys())
        current = self.registry.current_mode
        try:
            idx = mode_names.index(current)
        except ValueError:
            idx = 0
        next_idx = (idx + 1) % len(mode_names)
        next_mode_name = mode_names[next_idx]
        try:
            mode = self.registry.set_mode(next_mode_name)
            self.status_bar.set_mode_notification(mode.name)
            console.print(
                f"\n[dim]┌─ Shift+Tab 切换范式 → [bold]{mode.name}[/bold]"
                f" — {mode.description}[/dim]"
            )
        except ValueError:
            pass

    def _render_engine_result(self, callback, result: str, title: str, border_style: str = "green") -> None:
        """渲染引擎结果：默认折叠执行过程，仅显示最终答案。

        v0.5.3: 执行日志（工具调用/HTTP 请求/引擎信息）默认全部隐藏。
        仅显示一条折叠摘要行（含迭代次数和工具调用次数）。
        用户通过 Ctrl+O 或 /thinking on 可展开查看完整执行过程。
        """
        panel = callback.get_thinking_panel()
        if panel is not None:
            self._last_thinking_panel = panel
            step_count = len(panel.steps)
            tool_count = panel.tool_call_count
            for _ in range(tool_count):
                self.status_bar.add_tool_call()
            # v0.5.4: 从成功的工具调用中提取文件路径，更新工作记忆
            self._track_session_files(panel)
        else:
            step_count = 0
            tool_count = 0

        # v0.5.3: 诊断日志 — 记录结果长度，便于排查空结果问题
        if not result or not result.strip():
            logger.warning(
                f"_render_engine_result: 引擎返回空结果 "
                f"(result={result!r}, steps={step_count}, tools={tool_count}, "
                f"title={title!r})"
            )
            result = "任务已执行，但未生成明确的回复内容。请尝试重新提问或使用更具体的指令。"

        if self._show_thinking:
            # ── 展开模式：重现完整执行过程 ──
            if self._last_mode_line:
                console.print(f"[dim]{self._last_mode_line}[/dim]")
            if self._captured_log:
                console.print(self._captured_log, end="")
            if panel is not None:
                console.print(panel)
            console.print("[dim]  💭 已展开  [Ctrl+O 折叠][/dim]")
        else:
            # ── 折叠模式（默认）：仅一条摘要行 ──
            parts = []
            if step_count:
                parts.append(f"{step_count} 步")
            if tool_count:
                parts.append(f"🔧{tool_count}")
            summary = " · ".join(parts) if parts else "—"
            console.print(f"[dim]  💭 {summary}  [Ctrl+O][/dim]")

        # 最终答案始终显示
        console.print()
        console.print(Panel(
            Markdown(result),
            title=f"[bold]{title}[/bold]",
            border_style=border_style,
            padding=(0, 1),
        ))

    # v0.5.4: 从成功的工具调用中提取文件路径，更新工作记忆，
    # 使后续对话能知道"刚刚创建/修改了哪些文件"。
    _FILE_CREATE_TOOLS = {"write_file", "create_directory", "batch_write"}
    _FILE_MODIFY_TOOLS = {"edit_file", "command"}

    def _track_session_files(self, panel) -> None:
        """从 ThinkingPanel 中提取文件路径，更新 ContextManager 工作记忆。"""
        import os as _os

        created: list[str] = []
        modified: list[str] = []

        for step in panel.steps:
            if not step.action or step.is_error:
                continue
            action = step.action
            ai = step.action_input if isinstance(step.action_input, dict) else {}

            # 提取 file_path / file_paths / path / target_directory
            paths: list[str] = []
            for key in ("file_path", "file_paths", "path", "target_directory"):
                val = ai.get(key)
                if isinstance(val, str):
                    paths.append(val)
                elif isinstance(val, list):
                    paths.extend([str(v) for v in val if isinstance(v, str)])

            # 特殊处理 batch_write: files 是 [{path: ..., content: ...}, ...]
            if action == "batch_write" and "files" in ai:
                files = ai["files"]
                if isinstance(files, list):
                    for f in files:
                        if isinstance(f, dict) and "path" in f:
                            paths.append(str(f["path"]))

            if not paths:
                continue

            for p in paths:
                # 标准化为绝对路径
                abs_path = p if _os.path.isabs(p) else _os.path.abspath(p)

                if action in self._FILE_CREATE_TOOLS:
                    if abs_path not in created:
                        created.append(abs_path)
                elif action in self._FILE_MODIFY_TOOLS:
                    if abs_path not in modified:
                        modified.append(abs_path)

        if created or modified:
            # 合并到工作记忆中（保留历史记录）
            prev = self.ctx_mgr.get_working_memory()
            all_created = list(prev.get("session_created_files", []))
            all_modified = list(prev.get("session_modified_files", []))

            for p in created:
                if p not in all_created:
                    all_created.append(p)
            for p in modified:
                if p not in all_modified:
                    all_modified.append(p)

            self.ctx_mgr.update_working_memory("session_created_files", all_created)
            self.ctx_mgr.update_working_memory("session_modified_files", all_modified)

            # 同时跟踪最近一次操作的关键目录
            dirs = set()
            for p in created:
                d = _os.path.dirname(p)
                if d:
                    dirs.add(d)
            if dirs:
                prev_dirs = list(prev.get("session_active_dirs", []))
                for d in dirs:
                    if d not in prev_dirs:
                        prev_dirs.insert(0, d)  # 最近的在前
                self.ctx_mgr.update_working_memory("session_active_dirs", prev_dirs[:5])

    @staticmethod
    def _default_system_prompt() -> str:
        from datetime import datetime
        now = datetime.now()
        weekdays_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        current_date = f"{now.year}年{now.month}月{now.day}日 {weekdays_cn[now.weekday()]}"
        return (
            "你是 OmniAgent-CLI 的 AI 编程助手。"
            "你可以帮助用户编写代码、调试问题、解释概念。\n\n"
            f"当前日期: {current_date}。"
            "当用户询问日期、时间等问题时，直接使用此信息回答，不要编造。\n\n"
            "## 内置能力\n"
            "- 终端命令执行（command）—— 运行 shell 命令\n"
            "- 文件读写（read_file/write_file/edit_file）—— 读写和编辑文件\n"
            "- 代码搜索（search_files/list_files）—— 搜索和浏览代码库\n"
            "- Git 操作（git）—— 提交、日志、分支管理\n"
            "- 网页抓取（web_fetch）—— 获取网页内容\n"
            "- MCP 扩展（mcp_call）—— 调用外部 MCP 工具\n\n"
            "## 可用命令\n"
            "- /mcp add <name> <command> [args...] —— 添加 MCP 服务器（如 /mcp add 12306 npx -y 12306-mcp）\n"
            "- /mcp list —— 列出已连接的 MCP 服务器\n"
            "- /mcp tools —— 列出所有 MCP 工具\n"
            "- /mode —— 切换思考范式（ReAct/Plan-Execute/Reflection 等）\n"
            "- /tools —— 查看可用工具列表\n"
            "- /help —— 查看所有命令\n\n"
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

        # v0.4.0 Step 14: 检查可恢复的会话
        self._check_auto_resume()

        # 初始化系统消息
        if self.system_prompt:
            self.ctx_mgr.add_system_message(self.system_prompt)

        while True:
            # 状态栏：PT 模式由 bottom_toolbar 自动渲染，非 PT 模式仅在值变化时打印
            if self._pt_session is None:
                self.status_bar.print_status()

            try:
                user_input = self._read_input()
            except (KeyboardInterrupt, EOFError):
                # v0.3.0+ 修复（C-3）：bash 风格——单次 Ctrl+C 重画 prompt，
                # 连续两次才退出。修复前空行 Ctrl+C 在 5/9 终端类型（xterm256color/
                # alacritty/gnome-256color/screen-256color/vt100）直接退出 REPL。
                if self._pending_exit:
                    self._auto_save_session()
                    console.print("\n[dim]再见！[/dim]")
                    break
                self._pending_exit = True
                console.print("\n[dim]· 已中断，按 Ctrl+C 再次退出[/dim]")
                continue

            # 成功读取输入 → 重置 pending_exit
            self._pending_exit = False

            if not user_input:
                continue

            # 斜杠命令
            if user_input.startswith("/"):
                if self._handle_command(user_input):
                    self._auto_save_session()
                    console.print("[dim]再见！[/dim]")
                    break
                continue

            # 多轮对话（带 prompt 优化）
            self._handle_chat(user_input)

    def _check_first_run(self) -> None:
        """首次启动时检测配置状态，自动引导。

        v0.3.0+ 修复（C-2）：从纯 yaml 检查改为 get_configured_providers 检查，
        兼容 env 变量（Claude Code 内 ANTHROPIC_AUTH_TOKEN 也能触发自动加载）。
        """
        from omniagent.repl.provider_registry import get_configured_providers, load_credentials

        creds = load_credentials()
        configured = get_configured_providers()

        if not creds and not configured:
            # 完全没有配置（既没 yaml 也没 env）— 引导用户
            console.print("[dim]· 尚未配置 API Key，输入 [bold cyan]/setup[/bold cyan] 进入配置向导[/dim]\n")
        else:
            # v0.4.0: always populate model pool from ALL configured providers
            pool_count = 0
            import os as _os
            _max_per_provider = int(_os.environ.get("OMNIAGENT_MAX_MODELS_PER_PROVIDER", "3"))
            for p in configured:
                if not p.models or "(auto-fetch" in str(p.models[0]):
                    continue
                if not p.key or not p.key.strip():
                    logger.warning(f"跳过空 key 的 provider（name={p.name!r}），model_id 会变成 /model_name 导致路由失败")
                    continue
                for model_name in p.models[:_max_per_provider]:  # top N per provider (P0: 可配置)
                    model_id = f"{p.key}/{model_name}"
                    alias = model_name.replace(".", "-")
                    # Register to pool (if not already there)
                    if not self.model_pool.get(alias):
                        self.model_pool.register(
                            model_id, alias=alias, weight=3.0,
                            api_key=p.api_key, base_url=p.base_url,
                        )
                        pool_count += 1
                    # Also ensure registry has it (backward compat)
                    if not self.registry.list_models() or alias not in {m.alias for m in self.registry.list_models()}:
                        self.registry.add_model(model_id, alias)
                        if "planner" not in self.registry.role_priority:
                            self.registry.role_priority["planner"] = []
                        if alias not in self.registry.role_priority["planner"]:
                            self.registry.role_priority["planner"].append(alias)

            if pool_count > 0:
                console.print(f"[dim]· 已加载 {pool_count} 个模型到调用池[/dim]")
                if len(configured) > 1:
                    console.print(f"[dim]· auto 模式: 根据任务难度自动选择模型[/dim]")
            console.print()

        # 加载自定义快捷指令和技能
        self._load_custom_commands()

        # v0.5.3: 自动连接持久化的 MCP 服务器
        self._auto_connect_mcp_servers()

    def _print_welcome(self) -> None:
        """打印 Claude Code 风格欢迎界面。

        设计原则：极简。一条线展示版本和关键状态，无装饰框。
        信息密度高、视觉噪音低。
        """
        import random

        mode = self.registry.get_current_mode()
        models = self.registry.list_models()

        # ── 模型状态 ──
        if models:
            model_display = f"[bold green]{models[0].alias}[/bold green]"
            if len(models) > 1:
                model_display += f" [dim]+{len(models) - 1}[/dim]"
        else:
            model_display = "[dim]未配置 — /setup 开始[/dim]"

        # ── 随机提示 ──
        tips = [
            "/help 命令  ·  /mode 范式  ·  Shift+Tab 切换",
            "Shift+Enter 多行  ·  Enter 发送  ·  Ctrl+C 退出",
            "/setup 配置  ·  /tools 工具  ·  /mcp 扩展",
        ]
        tip = random.choice(tips)

        # ── 版本号 ──
        try:
            from importlib.metadata import version as _pkg_version
            _ver = _pkg_version("omniagent-cli")
        except Exception:
            _ver = "0.6.0"

        console.print()
        console.print(
            f"  [bold white]OmniAgent[/bold white] [dim]v{_ver}[/dim]"
            f"  [dim]—[/dim]  [dim]{mode.name}[/dim]  [dim]·[/dim]  {model_display}"
        )
        console.print(f"  [dim]{tip}[/dim]")
        console.print()

    def _read_input(self) -> str:
        """读取用户输入。优先使用 prompt_toolkit，不可用时回退自建输入。"""
        if self._pt_session is not None:
            try:
                return self._read_input_pt()
            except _ShiftTabSignal:
                self._handle_shift_tab()
                return ""

        import sys
        if sys.platform != "win32":
            try:
                return self._read_input_unix()
            except _ShiftTabSignal:
                self._handle_shift_tab()
                return ""

        return self._read_input_windows()

    def _read_input_pt(self) -> str:
        """prompt_toolkit 输入 — Claude Code 风格双线 + ❯ 提示符。

        底部分隔线由 console.print 渲染（上一轮输出边界），
        上分隔线由 prompt message 渲染，状态行由 bottom_toolbar 渲染：

          ──────────────────────────────   ← 底部分隔线（console.print）
          ──────────────────────────────   ← 上分隔线（prompt message）
          ❯ user types here               ← 输入行
            model · react · 12k/128k      ← 状态行（bottom_toolbar）
        """
        import shutil

        if hasattr(self, '_completer'):
            self._completer.update_commands(list(COMMANDS.keys()))
            if hasattr(self, 'model_pool') and self.model_pool:
                self._completer.update_models(
                    [e.alias for e in self.model_pool.list_all()]
                )

        tw = shutil.get_terminal_size().columns
        separator = "─" * tw

        # 底部分隔线：上一轮输出与当前输入区域之间的视觉边界
        console.print(f"[dim]{separator}[/dim]")

        # prompt message：上分隔线 + ❯ 提示符
        message: list[tuple[str, str]] = [
            ("class:input-box.border", separator + "\n"),
            ("class:prompt", "❯ "),
        ]

        try:
            text = self._pt_session.prompt(message)
        except KeyboardInterrupt:
            raise KeyboardInterrupt
        except EOFError:
            raise KeyboardInterrupt

        return text.strip()

    def _read_input_windows(self) -> str:
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

        sys.stdout.write("\n\033[1;92m>\033[0m ")
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
                elif second == '\x0f':  # Shift+Tab
                    self._handle_shift_tab()
                    # 不向 current_line 插入任何字符

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

        PROMPT = "\033[1;92m>\033[0m "
        CONTINUATION = "\033[90m·\033[0m "

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

            # ── 启用终端粘贴括号模式 ────────────────────
            # 粘贴时终端发送 \x1b[200~ 开始 / \x1b[201~ 结束，
            # 从而在粘贴期间批量处理字符，避免每字符一次重绘。
            sys.stdout.write('\x1b[?2004h')
            sys.stdout.flush()

            lines: list[str] = []
            current_line: list[str] = []
            cursor_pos: int = 0
            prompt_active = True

            # ── 粘贴模式状态 ─────────────────────────────
            paste_mode = False
            # v0.3.0 修复（Bug：粘贴结束信号丢失时 paste_mode 死锁）：
            # 某些终端/网络/SSH 会把 \x1b[201~ 结束信号切碎或丢失，导致 paste_mode 永远 True，
            # 后续用户按键（空格/字母）进入 paste_mode 分支被插入 current_line 但不重绘，
            # 表现为"按空格不显示 + 字符累积成重复粘贴"。超时退出机制：paste_mode 期间
            # select 0.3s 无新字节 → 自动退出 paste_mode + 强制 _redraw_line()。
            # 通用机制改进，不针对特定任务/终端加白名单。
            import time as _time
            paste_last_byte_at: float | None = None
            PASTE_TIMEOUT_S = 0.3

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
                # 用 select 检查是否有输入（超时处理粘贴检测 + paste_mode 超时退出）
                if select([sys.stdin], [], [], 0.01)[0]:
                    ch = sys.stdin.read(1)
                else:
                    # v0.3.0 修复：paste_mode 期间 select 0.3s 无新字节 → 自动退出
                    # 解决"结束信号 \x1b[201~ 丢失导致 paste_mode 死锁"问题。
                    if (
                        paste_mode
                        and paste_last_byte_at is not None
                        and _time.monotonic() - paste_last_byte_at > PASTE_TIMEOUT_S
                    ):
                        paste_mode = False
                        paste_last_byte_at = None
                        _redraw_line()
                    continue

                # 处理转义序列
                # v0.3.0+ 修复（C-1）：转义序列累积器**总是**累积。
                # 但有双守卫保证 paste_mode 状态机不死锁：
                #   ① paste end \x1b[201~ **总是**优先识别并关闭 paste_mode
                #      （否则粘贴内容里的 ESC 字节会让累积器错位、最终丢失
                #      paste end → paste_mode 永远 True → REPL 挂死）
                #   ② paste_mode 期间累积到 8 字节**子串搜索** paste end：
                #      - 含 paste end → 前部分追加 buffer + 关闭 paste_mode
                #      - 不含 → 整批追加 buffer（保留用户主动复制的 ESC 字节）
                if seq_buffer or ch == '\x1b':
                    seq_buffer += ch
                    # 守卫 ①：paste end 总是截留（精确 6 字符匹配）
                    if seq_buffer == '\x1b[201~':
                        paste_mode = False
                        seq_buffer = ''
                        paste_last_byte_at = None
                        _redraw_line()
                        continue
                    if paste_mode:
                        # 守卫 ②：累积 8 字节时子串搜索 paste end
                        if '\x1b[201~' in seq_buffer:
                            idx = seq_buffer.index('\x1b[201~')
                            for c in seq_buffer[:idx]:
                                current_line.insert(cursor_pos, c)
                                cursor_pos += 1
                            paste_mode = False
                            seq_buffer = ''
                            paste_last_byte_at = None
                            _redraw_line()
                            continue
                        if len(seq_buffer) >= 8:
                            for c in seq_buffer:
                                current_line.insert(cursor_pos, c)
                                cursor_pos += 1
                            seq_buffer = ''
                            paste_last_byte_at = _time.monotonic()
                        continue
                    if len(seq_buffer) == 1 and ch == '\x1b':
                        continue  # 等待更多字节

                    # ── 粘贴括号模式 ────────────────────
                    if seq_buffer == '\x1b[200~':
                        # 开始粘贴 — 暂停逐字符重绘
                        paste_mode = True
                        paste_last_byte_at = _time.monotonic()
                        seq_buffer = ""
                        continue
                    if seq_buffer == '\x1b[201~':
                        # 粘贴结束 — 一次性重绘
                        paste_mode = False
                        paste_last_byte_at = None
                        _redraw_line()
                        seq_buffer = ""
                        continue

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

                    # Shift+Enter（kitty 键盘协议）: \x1b[13;2u
                    if seq_buffer == '\x1b[13;2u':
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

                    # Shift+Tab: \x1b[Z → 切换思考范式
                    if seq_buffer == '\x1b[Z':
                        seq_buffer = ""
                        raise _ShiftTabSignal()

                    # 未知转义序列 — 静默丢弃或超时后当作普通字符
                    # 如果序列长度 >= 8 或超时，丢弃
                    if len(seq_buffer) >= 8:
                        seq_buffer = ""
                        continue
                    # 否则继续累积
                    continue

                # ── 粘贴模式：缓冲修改，不重绘 ──
                if paste_mode:
                    if ch in ('\r', '\n'):
                        # 粘贴中的换行：完成当前行，开始新行
                        lines.append("".join(current_line))
                        current_line = []
                        cursor_pos = 0
                    elif ch == '\x03':   # Ctrl+C during paste
                        sys.stdout.write("\r\n")
                        sys.stdout.flush()
                        raise KeyboardInterrupt
                    elif ch in ('\x7f', '\x08'):  # Backspace
                        if cursor_pos > 0:
                            current_line.pop(cursor_pos - 1)
                            cursor_pos -= 1
                    elif ch == '\x1b':
                        # v0.3.0+ 修复（C-1 配套）：粘贴期间遇到 ESC 字节
                        # 不再走转义序列累积器（已在上方 if 屏蔽），改当普通
                        # 字符插入 buffer——用户主动复制粘贴含 ANSI 转义序列
                        # 的代码（如 `echo -e "\033[31m红色\033[0m"`）应保留 ESC
                        current_line.insert(cursor_pos, ch)
                        cursor_pos += 1
                    elif ord(ch) >= 0x20:
                        current_line.insert(cursor_pos, ch)
                        cursor_pos += 1
                    # v0.3.0 修复：每次粘贴期间字节都要刷新 last_byte_at，
                    # 否则 select 0.3s 超时检查会误判 paste_mode 空闲
                    paste_last_byte_at = _time.monotonic()
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
            # 禁用粘贴括号模式，恢复终端设置
            sys.stdout.write('\x1b[?2004l')
            sys.stdout.flush()
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    def _classify_slash_input(self, raw: str) -> str:
        """规则 + LLM 校验：判断以 / 开头的输入是命令还是普通对话。

        规则先行，LLM 兜底。返回 'command' 或 'chat'。

        v0.5.4: 已知命令（含参数）直接走命令处理器，不再经 LLM 分类。
        此前 /skill creat ... 等已知命令+参数会被 LLM 误判为 "chat"，
        导致整个输入被路由到 _handle_chat 而非命令处理器。
        """
        parts = raw.split(maxsplit=1)
        cmd_name = parts[0].lower()

        # ── 规则快速通道：无歧义场景直接判定 ──
        # 文件路径：含多个 /（如 /home/user/file）
        if cmd_name.count("/") > 1:
            return "chat"
        # v0.5.4: 已知命令（无论有无参数）→ 直接走命令处理器
        # 子命令纠错/模糊匹配应在命令处理器内部完成，不应由 LLM 决定路由
        if cmd_name in COMMANDS:
            return "command"

        # ── LLM 分类：仅对未知 / 开头的输入 ──
        try:
            from omniagent.utils.llm_client import chat_completion

            model_ids = self.registry.get_role_priority("planner")
            effective = [m for m in model_ids if m not in getattr(self, "_failed_models", set())]
            if not effective:
                effective = model_ids
            if effective:
                prompt = (
                    "你是一个输入分类器。判断以下用户输入是斜杠命令还是普通对话。\n"
                    "斜杠命令：用户想执行一个操作（如 /help, /exit, /code 写代码）\n"
                    "普通对话：用户想聊天或提问，只是输入恰好以 / 开头\n\n"
                    f"用户输入: {raw}\n\n"
                    "只回复一个词: command 或 chat"
                )
                result = chat_completion(
                    effective[0],
                    [{"role": "user", "content": prompt}],
                    max_tokens=10,
                    temperature=0,
                )
                result_lower = result.strip().lower()
                if "chat" in result_lower:
                    return "chat"
                if "command" in result_lower:
                    return "command"
        except Exception:
            pass  # LLM 不可用，走规则兜底

        # ── 规则兜底：未知命令视为 chat ──
        return "chat"

    def _handle_command(self, raw: str) -> bool:
        """处理斜杠命令。返回 True 表示需要退出。"""
        from omniagent.repl.commands import ExitSignal

        parts = raw.split(maxsplit=1)
        cmd_name = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        # v0.5.2: LLM + 规则校验——判断输入是否是真正的命令
        if self._classify_slash_input(raw) == "chat":
            self._handle_chat(raw)
            return False

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
            console.print()
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
        self._sync_context_window(self.auto_router.route(user_input, count=3))
        # 自动 compact 检查
        if self.ctx_mgr.needs_compact():
            console.print("[dim]· 对话较长，建议 [bold cyan]/compact[/bold cyan] 压缩[/dim]")

        # ── 视觉分隔：在每轮对话前打印一条细线 ──
        if not self._first_turn:
            console.print()
            console.print("[dim]───[/dim]")
        self._first_turn = False

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
                # 展示优化后的 prompt，帮助用户学习（折叠为一行提示）
                console.print(
                    f"[dim]📝 已优化 → "
                    f"{optimized[:120]}{'…' if len(optimized) > 120 else ''}[/dim]"
                )
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
        # v0.4.0: auto-route based on task difficulty
        if self.auto_router.is_empty():
            # Fallback to static registry for backward compat
            model_ids = self.registry.get_role_priority("planner")
        else:
            model_ids = self.auto_router.route(
                optimized, self.ctx_mgr.get_messages(), count=3,
                preferred_models=self._preferred_model_ids or None,
            )
        if not model_ids:
            console.print("[red]· 未配置模型，请先 [bold cyan]/setup[/bold cyan] 配置[/red]")
            return

        # v0.5.2: 过滤本会话已失败的模型（统一入口，覆盖所有引擎模式）
        model_ids = [m for m in model_ids if m not in self._failed_models]
        if not model_ids:
            self._failed_models.clear()
            model_ids = self.registry.get_role_priority("planner")
            if not model_ids:
                console.print("[red]· 所有模型均已失败且无法恢复[/red]")
                return
            console.print("[dim]· 所有模型已重置失败状态，重新尝试[/dim]")

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
        """直接对话模式。自动检测工具需求并委派给 ReAct 引擎。

        路由决策优先级（通用设计，不枚举具体领域）：
        1. 明确的编程/文件/命令任务 → ReAct（_TOOL_PATTERNS 正则）
        2. 已识别的 query/write_code 意图 → ReAct
        3. MCP 工具可用 + 输入具有"外部信息查询"特征 → ReAct
           （通用语言结构判断，不枚举天气/高铁/酒店等具体领域）
        4. 其他 → direct 模式（纯对话/解释/闲聊）
        """
        # 检测是否需要工具执行（编程/文件/命令任务，或 query 意图实时数据查询）
        if self._detect_tool_need(user_input, intent=intent):
            if intent == "query":
                console.print("[cyan]🔧 检测到信息查询（需实时数据），自动切换到 ReAct 模式...[/cyan]")
            else:
                console.print("[cyan]🔧 检测到需要工具执行，自动切换到 ReAct 模式...[/cyan]")
            self._run_react_engine(user_input, model_ids)
            return

        # v0.5.3: MCP 工具可用时，通用判断——任何具有"信息查询"特征的输入都走 ReAct，
        # 让 LLM 自行决定是否调用 mcp_call。不枚举具体查询领域（天气/高铁/酒店等）。
        if self._has_mcp_tools() and _looks_like_external_query(user_input):
            console.print("[cyan]🔧 检测到可用 MCP 工具，自动切换到 ReAct 模式...[/cyan]")
            self._run_react_engine(user_input, model_ids)
            return

        messages = self.ctx_mgr.get_messages()

        # v0.5.2: 过滤本会话已失败的模型，避免每次对话都重试不可用模型
        effective_ids = [m for m in model_ids if m not in self._failed_models]
        if not effective_ids:
            # 所有模型都已失败过——重置并重新尝试（给一次重试机会）
            self._failed_models.clear()
            effective_ids = model_ids
        elif len(effective_ids) < len(model_ids):
            skipped = set(model_ids) - set(effective_ids)
            console.print(f"[dim]· 跳过 {len(skipped)} 个已失败模型（本会话）[/dim]")

        last_error = None
        for model_id in effective_ids:
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
                self._failed_models.add(model_id)
                console.print(f"[yellow]模型 {model_id} 失败: {e}，已标记不可用，尝试下一个...[/yellow]")

        console.print(f"[error]❌ 所有模型均调用失败: {last_error}[/error]")

    def _inject_mcp_tools_into_engine(self, engine: object) -> None:
        """v0.5.3: 将可用 MCP 工具列表注入引擎的 system prompt。

        所有引擎（ReAct/PlanExecute/Reflection 等）统一使用此方法，
        让 LLM 在 mcp_call 工具描述中看到具体可用的 MCP 工具名和描述。
        """
        try:
            mcp_tools_list = self._build_mcp_tools_list()
        except Exception as e:
            logger.debug(f"构建 MCP 工具列表失败: {e}")
            return
        if mcp_tools_list and hasattr(engine, '_mcp_tools_list'):
            try:
                engine._mcp_tools_list = mcp_tools_list
                if hasattr(engine, '_build_system_prompt'):
                    engine.system_prompt = engine._build_system_prompt()
            except Exception as e:
                logger.warning(f"注入 MCP 工具列表到引擎失败: {e}")

    def _run_react_engine(self, user_input: str, model_ids: list[str]) -> None:
        """ReAct 引擎模式。"""
        from omniagent.engine.react_engine import ReActEngine

        # v0.5.3: 模式行不直接打印，存储供 Ctrl+O 展开时使用
        self._last_mode_line = "· ReAct 思考 → 行动 → 观察"

        self._start_log_capture()
        callback = self._make_callback()
        engine = ReActEngine(model_priority=model_ids, model_pool=self.model_pool, auto_router=self.auto_router, max_iterations=10, callback=callback, model_configs=dict(self.registry.models))
        self._inject_mcp_tools_into_engine(engine)
        try:
            result = engine.run(user_input, self.agent_context, ctx_mgr=self.ctx_mgr)
            self._captured_log = self._stop_log_capture()
            # v0.5.3: 诊断日志 — 记录结果实际值，用于排查空白面板根因
            logger.info(
                f"_run_react_engine: result type={type(result).__name__}, "
                f"len={len(result) if isinstance(result, str) else 'N/A'}, "
                f"strip_len={len(result.strip()) if isinstance(result, str) and result else 0}, "
                f"head={result[:80] if isinstance(result, str) and result else repr(result)[:80]}"
            )
            self.ctx_mgr.add_assistant_message(result, model_used=model_ids[0])
            self._render_engine_result(callback, result, "ReAct 结果")
            self.status_bar.set_last_model(model_ids[0])
        except Exception as e:
            self._captured_log = self._stop_log_capture()
            # 异常时展开日志便于调试
            if self._last_mode_line:
                console.print(f"[dim]{self._last_mode_line}[/dim]")
            if self._captured_log:
                console.print(self._captured_log, end="")
            import traceback
            logger.error(f"ReAct 引擎异常:\n{traceback.format_exc()}")
            console.print(f"[error]❌ ReAct 引擎执行失败: {e}[/error]")
            try:
                self.ctx_mgr.add_assistant_message(
                    f"[错误] ReAct 引擎执行失败: {e}", model_used=model_ids[0],
                )
            except Exception:
                try:
                    self.ctx_mgr.trim_last_user()
                except Exception:
                    pass

    def _run_plan_execute_engine(self, user_input: str, model_ids: list[str]) -> None:
        """Plan-Execute 引擎模式。"""
        from omniagent.engine.plan_execute_engine import PlanExecuteEngine

        self._last_mode_line = "· Plan-Execute 规划 → 逐步执行"

        self._start_log_capture()
        callback = self._make_callback()
        engine = PlanExecuteEngine(model_priority=model_ids, model_pool=self.model_pool, auto_router=self.auto_router, max_steps=20, callback=callback, model_configs=dict(self.registry.models))
        self._inject_mcp_tools_into_engine(engine)
        try:
            result = engine.run(user_input, self.agent_context, ctx_mgr=self.ctx_mgr)
            self._captured_log = self._stop_log_capture()
            self.ctx_mgr.add_assistant_message(result, model_used=model_ids[0])
            self._render_engine_result(callback, result, "Plan-Execute 结果")
            self.status_bar.set_last_model(model_ids[0])
        except Exception as e:
            self._captured_log = self._stop_log_capture()
            if self._last_mode_line:
                console.print(f"[dim]{self._last_mode_line}[/dim]")
            if self._captured_log:
                console.print(self._captured_log, end="")
            console.print(f"[error]❌ Plan-Execute 引擎执行失败: {e}[/error]")

    def _run_reflection_engine(self, user_input: str, model_ids: list[str]) -> None:
        """Reflection 引擎模式。"""
        from omniagent.engine.reflection_engine import ReflectionEngine

        self._last_mode_line = "· Reflection 执行 → 审查 → 修正"

        self._start_log_capture()
        callback = self._make_callback()
        engine = ReflectionEngine(model_priority=model_ids, model_pool=self.model_pool, auto_router=self.auto_router, max_rounds=3, callback=callback, model_configs=dict(self.registry.models))
        self._inject_mcp_tools_into_engine(engine)
        try:
            result = engine.run(user_input, context=self.agent_context, ctx_mgr=self.ctx_mgr)
            self._captured_log = self._stop_log_capture()
            self.ctx_mgr.add_assistant_message(result, model_used=model_ids[0])
            self._render_engine_result(callback, result, "Reflection 结果")
            self.status_bar.set_last_model(model_ids[0])
        except Exception as e:
            self._captured_log = self._stop_log_capture()
            if self._last_mode_line:
                console.print(f"[dim]{self._last_mode_line}[/dim]")
            if self._captured_log:
                console.print(self._captured_log, end="")
            console.print(f"[error]❌ Reflection 引擎执行失败: {e}[/error]")

    def _run_plan_react_engine(self, user_input: str, model_ids: list[str]) -> None:
        """Plan + React 组合引擎模式。"""
        from omniagent.engine.combined_engines import PlanReactEngine

        self._last_mode_line = "· Plan+React 全局规划 → 每步 ReAct 执行"

        self._start_log_capture()
        callback = self._make_callback()
        engine = PlanReactEngine(model_priority=model_ids, model_pool=self.model_pool, auto_router=self.auto_router, max_steps=10, react_iterations=8, callback=callback, model_configs=dict(self.registry.models))
        self._inject_mcp_tools_into_engine(engine)
        try:
            result = engine.run(user_input, context=self.agent_context, ctx_mgr=self.ctx_mgr)
            self._captured_log = self._stop_log_capture()
            self.ctx_mgr.add_assistant_message(result, model_used=model_ids[0])
            self._render_engine_result(callback, result, "Plan+React 结果")
            self.status_bar.set_last_model(model_ids[0])
        except Exception as e:
            self._captured_log = self._stop_log_capture()
            if self._last_mode_line:
                console.print(f"[dim]{self._last_mode_line}[/dim]")
            if self._captured_log:
                console.print(self._captured_log, end="")
            console.print(f"[error]❌ Plan+React 引擎执行失败: {e}[/error]")

    def _run_plan_reflection_engine(self, user_input: str, model_ids: list[str]) -> None:
        """Plan + Reflection 组合引擎模式。"""
        from omniagent.engine.combined_engines import PlanReflectionEngine

        self._last_mode_line = "· Plan+Reflection 规划执行 → 反思修正"

        self._start_log_capture()
        callback = self._make_callback()
        engine = PlanReflectionEngine(model_priority=model_ids, model_pool=self.model_pool, auto_router=self.auto_router, max_steps=10, review_rounds=2, callback=callback, model_configs=dict(self.registry.models))
        self._inject_mcp_tools_into_engine(engine)
        try:
            result = engine.run(user_input, context=self.agent_context, ctx_mgr=self.ctx_mgr)
            self._captured_log = self._stop_log_capture()
            self.ctx_mgr.add_assistant_message(result, model_used=model_ids[0])
            self._render_engine_result(callback, result, "Plan+Reflection 结果")
            self.status_bar.set_last_model(model_ids[0])
        except Exception as e:
            self._captured_log = self._stop_log_capture()
            if self._last_mode_line:
                console.print(f"[dim]{self._last_mode_line}[/dim]")
            if self._captured_log:
                console.print(self._captured_log, end="")
            console.print(f"[error]❌ Plan+Reflection 引擎执行失败: {e}[/error]")

    def _run_react_reflection_engine(self, user_input: str, model_ids: list[str]) -> None:
        """ReAct + Reflection 组合引擎模式。"""
        from omniagent.engine.combined_engines import ReactReflectionEngine

        self._last_mode_line = "· React+Reflection 探索 → 反思审查"

        self._start_log_capture()
        callback = self._make_callback()
        engine = ReactReflectionEngine(model_priority=model_ids, model_pool=self.model_pool, auto_router=self.auto_router, react_iterations=8, review_rounds=2, callback=callback, model_configs=dict(self.registry.models))
        self._inject_mcp_tools_into_engine(engine)
        try:
            result = engine.run(user_input, context=self.agent_context, ctx_mgr=self.ctx_mgr)
            self._captured_log = self._stop_log_capture()
            self.ctx_mgr.add_assistant_message(result, model_used=model_ids[0])
            self._render_engine_result(callback, result, "React+Reflection 结果")
            self.status_bar.set_last_model(model_ids[0])
        except Exception as e:
            self._captured_log = self._stop_log_capture()
            if self._last_mode_line:
                console.print(f"[dim]{self._last_mode_line}[/dim]")
            if self._captured_log:
                console.print(self._captured_log, end="")
            console.print(f"[error]❌ React+Reflection 引擎执行失败: {e}[/error]")

    def _run_novel_engine(self, user_input: str, model_ids: list[str]) -> None:
        """小说创作引擎模式（支持多小说隔离）。"""
        from omniagent.engine.novel_engine import NovelEngine

        self._last_mode_line = "· Novel 小说创作模式"

        self._start_log_capture()
        callback = self._make_callback()
        engine = NovelEngine(
            model_priority=model_ids,
            max_iterations=15,
            callback=callback,
            novel_manager=self._novel_manager,
            model_configs=dict(self.registry.models),
            model_pool=self.model_pool,
            auto_router=self.auto_router,
        )
        try:
            result = engine.run(user_input, context=self.agent_context, ctx_mgr=self.ctx_mgr)
            self._captured_log = self._stop_log_capture()
            self.ctx_mgr.add_assistant_message(result, model_used=model_ids[0])
            self._render_engine_result(callback, result, "Novel 创作结果", border_style="magenta")
            self.status_bar.set_last_model(model_ids[0])
        except Exception as e:
            self._captured_log = self._stop_log_capture()
            if self._last_mode_line:
                console.print(f"[dim]{self._last_mode_line}[/dim]")
            if self._captured_log:
                console.print(self._captured_log, end="")
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

    def _has_mcp_tools(self) -> bool:
        """检查是否有已连接的 MCP 服务器且提供了工具。"""
        try:
            registry = getattr(self, '_mcp_registry', None)
            if registry is None:
                return False
            return bool(registry.clients)
        except Exception:
            return False

    def _build_mcp_tools_list(self) -> str:
        """v0.5.3: 构建可用 MCP 工具列表，注入到引擎的 system prompt 中。

        LLM 需要知道具体有哪些 MCP 工具可用，才能正确调用 mcp_call。
        不提供的话 LLM 只能猜测工具名（如 query_train），必然失败。
        """
        registry = getattr(self, '_mcp_registry', None)
        if not registry or not registry.tool_map:
            return ""

        # 确保工具已发现
        if not registry.tool_map:
            try:
                registry.discover_tools()
            except Exception:
                return ""

        tools_by_server: dict[str, list[tuple[str, str]]] = {}
        for full_name, (server_name, tool) in registry.tool_map.items():
            desc = tool.get("description", "")[:80] if isinstance(tool, dict) else str(tool)[:80]
            tools_by_server.setdefault(server_name, []).append((full_name, desc))

        parts = ["\n当前可用的 MCP 工具："]
        for server, tools in sorted(tools_by_server.items()):
            parts.append(f"  [{server}]")
            for name, desc in tools:
                parts.append(f"    - {name}: {desc}")
        return "\n".join(parts)

    def _auto_connect_mcp_servers(self) -> None:
        """v0.5.3: 启动时自动连接已持久化的 MCP 服务器。"""
        from omniagent.mcp.registry import MCPRegistry
        from omniagent.repl.provider_registry import load_mcp_servers

        servers = load_mcp_servers()
        if not servers:
            return

        if not hasattr(self, '_mcp_registry') or self._mcp_registry is None:
            self._mcp_registry = MCPRegistry()
            self.agent_context.set("_mcp_registry", self._mcp_registry)

        connected = 0
        for s in servers:
            name = s.get("name", "")
            if not name:
                continue
            try:
                if s.get("url"):
                    self._mcp_registry.add_server(name, url=str(s["url"]))
                else:
                    cmd = str(s.get("command", ""))
                    args = [str(a) for a in s.get("args", [])]
                    if cmd:
                        self._mcp_registry.add_server(name, command=cmd, args=args)
                    else:
                        continue
                connected += 1
            except Exception as e:
                logger.debug(f"自动连接 MCP '{name}' 失败: {e}")

        if connected:
            # 发现工具
            try:
                self._mcp_registry.discover_tools()
                total_tools = sum(
                    len(client.tools)
                    for client in self._mcp_registry.clients.values()
                )
                console.print(
                    f"[dim]· 自动连接 {connected} 个 MCP 服务器"
                    f"（{total_tools} 个工具）[/dim]"
                )
            except Exception:
                console.print(f"[dim]· 自动连接 {connected} 个 MCP 服务器[/dim]")

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


# ── v0.5.3: 通用外部查询检测 ──────────────────────────────
# 不枚举具体领域（天气/高铁/酒店），基于语言结构判断输入是否
# 具有"信息查询"特征——即需要外部/实时数据才能回答的问题。
# 当 MCP 工具可用时，这类输入应路由到 ReAct 让 LLM 决定调用哪些工具。

# 疑问结构（通用语言特征，不依赖领域关键词）
_RE_QUESTION_STRUCTURE = re.compile(
    r"[吗呢吧啊][？?]?$"           # 句末疑问语气词
    r"|[？?]$"                      # 问号结尾
    r"|有没有|会不会|能不能|可不可以"  # 正反问结构
    r"|怎么(?:走|去|办|样|回事)"    # 疑问代词 + 动作
    r"|在哪里|在哪|什么时候|几点|多少" # 疑问短语
    r"|what|when|where|how|which|who", # 英文疑问词
    re.IGNORECASE,
)
_RE_QUERY_VERB = re.compile(
    r"(?:帮|请|给).{0,3}(?:我)?(?:查|搜|找|查询|搜索|查找|看看|了解)"  # 委托查询
    r"|^(?:查|搜|找|查询|搜索|查找|看看)"                              # 句首查询动词
    r"|(?:search|find|look\s*up|check|query)\s",                       # 英文查询
    re.IGNORECASE,
)
_RE_TIME_SENSITIVE = re.compile(
    r"(?:今天|今日|现在|目前|最近|这周末|本周|下周|本月|这个月"
    r"|明天|后天|昨天|周日|周一|周二|周三|周四|周五|周六"
    r"|today|now|recently|this\s+week|next\s+week|tomorrow)",
    re.IGNORECASE,
)
# 排除：明确是关于代码/文件的查询（由 _TOOL_PATTERNS 处理）
_RE_CODE_CONTEXT = re.compile(
    r"(?:文件|代码|项目|脚本|程序|函数|类|目录|文件夹|bug|错误|报错"
    r"|测试|配置|日志|commit|分支|仓库|git\b"
    r"|\.(?:py|js|ts|java|go|rs|cpp|c|h|html|css|json|yaml|yml|toml|md|txt|sh)\b)",
    re.IGNORECASE,
)


def _looks_like_external_query(text: str) -> bool:
    """通用判断：输入是否具有"外部信息查询"特征。

    基于语言结构而非领域关键词：
    - 疑问结构（吗/呢/？/有没有/怎么走/在哪里/什么时候/几点/多少）
    - 查询动词（查/搜/找/search/find）
    - 时间敏感框架（今天/明天/最近...）

    排除：明确关于代码/文件的查询（由 _TOOL_PATTERNS 处理）。
    """
    if not text or len(text) < 3:
        return False
    # 代码相关 → 不归这里管
    if _RE_CODE_CONTEXT.search(text):
        return False
    # 疑问结构 → 需要外部信息
    if _RE_QUESTION_STRUCTURE.search(text):
        return True
    # 查询动词 → 在搜索/查找信息
    if _RE_QUERY_VERB.search(text):
        return True
    # 时间敏感短语 → 大概率需要实时/外部数据
    if _RE_TIME_SENSITIVE.search(text):
        return True
    return False


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
    # P0: 打通 --config -> ModelPool。原仅喂 Registry,而 AutoRouter 只认 Pool,
    # 导致 --config 加载的模型形同虚设(池仍空)。复用 ModelPool.from_config。
    if config_path:
        repl.model_pool.from_config(registry.export_config().get("models", {}))
    # v0.5.3: 用户显式指定的模型优先于 auto-router 的选择
    if models:
        repl._preferred_model_ids = list(models)
    repl.run()
