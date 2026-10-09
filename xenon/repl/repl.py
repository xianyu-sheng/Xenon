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
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any

from rich.console import Console
from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.prompt import Prompt
from rich.theme import Theme

from xenon.engine.context import AgentContext
from xenon.nodes.approval_policy import (
    APPROVAL_ALLOWED_ONCE,
    APPROVAL_CANCELLED,
    APPROVAL_REJECTED,
    APPROVAL_UNAVAILABLE,
    SessionRuleStore,
    targets_outside_workspace,
)
from xenon.repl.commands import COMMANDS, dispatch_command
from xenon.repl.context_manager import ContextManager
from xenon.repl.direct_chat import DirectChatMixin
from xenon.repl.engine_host import EngineHostMixin
from xenon.repl.turn_flow import TurnFlowMixin
from xenon.repl.execution_policy import (
    ExecutionLevel,
)
from xenon.repl.turn_contract import (
    PendingAction,
    TurnContract,
    build_turn_contract,
)
from xenon.repl.input_buffer import PastedTextStore, _ShiftTabSignal
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.project_context import ProjectContext
from xenon.repl.repl_input import _read_input_unix, _read_input_windows
from xenon.repl.status_bar import StatusBar
from xenon.repl.system_config import get_config


logger = logging.getLogger(__name__)

# R1: 纯函数已提取到具名模块；这里保留兼容导出（历史调用点/测试）。
from xenon.repl.turn_helpers import (  # noqa: E402
    _looks_like_external_query,  # noqa: F401 - compatibility export
    resume_prompt as _resume_prompt_fn,
    retry_budget_for as _retry_budget_for,  # noqa: F401 - compatibility export
    verify_retries_limit as _verify_retries_limit,  # noqa: F401 - compatibility export
)

# ── prompt_toolkit（可选依赖，不可用时回退自建输入）────────────
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.application import run_in_terminal
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.styles import Style
    from prompt_toolkit.formatted_text import HTML
    from pathlib import Path as _Path

    _HISTORY_DIR = _Path.home() / ".xenon"
    _HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    _HAS_PROMPT_TOOLKIT = True
except ImportError:
    _HAS_PROMPT_TOOLKIT = False
    PromptSession = None  # type: ignore
    FileHistory = None  # type: ignore
    KeyBindings = None  # type: ignore
    Style = None  # type: ignore
    HTML = None  # type: ignore
    run_in_terminal = None  # type: ignore

# ── 自定义主题 ────────────────────────────────────────────
_theme = Theme(
    {
        "user": "bold #67e8f9",
        "assistant": "#bbf7d0",
        "system": "dim #facc15",
        "error": "bold #fda4af",
        "command": "bold #c4b5fd",
    }
)

console = Console(theme=_theme)
# R1-2: 引擎宿主 mixin 与 REPL 共用同一 console 实例（测试 monkeypatch 依赖同一对象）。
import xenon.repl.engine_host as _engine_host  # noqa: E402
import xenon.repl.direct_chat as _direct_chat  # noqa: E402
import xenon.repl.turn_flow as _turn_flow  # noqa: E402

_engine_host.console = console
_direct_chat.console = console
_turn_flow.console = console


class REPL(EngineHostMixin, DirectChatMixin, TurnFlowMixin):
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
        resume: str | None = None,
    ) -> None:
        self.registry = registry or ModelRegistry()
        # 启动即恢复的目标（xenon --resume <序号|名称>）；None 表示不恢复。
        self._startup_resume = resume
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
        self._memory_service: Any = None
        self._memory_detector: Any = None
        self._pending_memory_use_ids: list[str] = []

        # 状态栏
        from xenon.utils.deepseek_cache import CacheTracker

        self._cache_tracker = CacheTracker(persist=True)
        self.status_bar = StatusBar(
            console, self.ctx_mgr, self.registry, cache_tracker=self._cache_tracker
        )

        # ── 视觉桥接器（惰性加载） ──────────────────────────
        from xenon.tools import VisionBridge, ClipboardMonitor

        self._vision_bridge = VisionBridge()
        self._vision_enabled = True  # 默认开启，可 /vision 切换
        self._clipboard_monitor = ClipboardMonitor(on_image=self._on_clipboard_image)
        self._logo_shown: bool = False  # 启动动画只播一次

        # 终端标签页活动状态：任务执行时星核移动，空闲/等待用户时静止。
        # OSC 标题更新不触碰终端正文；非 TTY、CI 和 dumb 终端自动禁用。
        from xenon.repl.terminal_activity import TerminalActivityIndicator

        self._terminal_activity = TerminalActivityIndicator()
        # Permission callbacks may arrive from parallel tool workers.  Only
        # one confirmation frontend may own stdin at a time; otherwise several
        # Rich prompts compete for the same terminal input stream.
        self._permission_prompt_lock = threading.RLock()
        self._active_callback: Any = None

        # v0.4.0: Auto router + model pool (replaces role_priority)
        from xenon.repl.model_pool import ModelPool
        from xenon.repl.auto_router import AutoRouter

        self.model_pool = ModelPool()
        self.auto_router = AutoRouter(
            self.model_pool,
            context_manager=self.ctx_mgr,
            cache_tracker=self._cache_tracker,
        )
        self.status_bar._auto_router = self.auto_router  # for "auto" display

        # P1-High: 注册状态栏同步回调
        self.model_pool.set_success_callback(
            lambda model_id: self.status_bar.set_last_model(model_id)
        )

        # v0.6.0: 智能路由器 (Intelligent Router)
        from xenon.repl.intelligent_router import IntelligentRouter

        self.intelligent_router = IntelligentRouter(
            enabled=False,  # 默认关闭，通过 CLI 参数或命令启用
            confidence_threshold=0.6,
            notify_user=True,
        )

        # 会话状态，供命令处理器共享
        self._session_state: dict[str, Any] = {
            "agent_context": self.agent_context,
            "_repl": self,
            "model_pool": self.model_pool,
            "auto_router": self.auto_router,
            "terminal_activity": self._terminal_activity,
        }

        # v0.3.0+ 修复（C-3）：bash 风格——单次 Ctrl+C 重画 prompt 继续，
        # 连续两次 Ctrl+C 才退出 REPL。修复前空 prompt + Ctrl+C 直接退出，
        # 5/9 终端类型（xterm256color/alacritty/gnome-256color/screen-256color/vt100）
        # 在空行 Ctrl+C 时丢失输入机会。
        self._pending_exit: bool = False

        # v0.5.0: prompt_toolkit 会话（命令历史 + Tab 补全 + 固定状态栏）
        self._paste_store = PastedTextStore()
        self._pt_session: Any = None
        self._init_prompt_toolkit()

        # 会话内明确不可恢复的模型（认证/模型名等终端错误）。网络抖动等
        # 瞬时错误交给 ModelPool 阈值熔断，不在这里永久拉黑。
        self._failed_models: set[str] = set()
        self._preferred_model_ids: list[str] = []  # v0.5.3: 用户 -m 指定的模型
        # 跨回合“回复继续”承诺：上一轮模型提出、用户确认后本轮续接的状态机。
        self._pending_action: PendingAction | None = None
        # 事件日志（附加事实层，失败无害）；投影/检索都从它重建。
        self._session_events: Any = None
        try:
            from xenon.session.events import SessionEventLog

            self._session_events = SessionEventLog()
        except Exception:  # noqa: BLE001 — 事实层不可用不能阻断 REPL
            logger.debug("会话事件日志初始化失败（已忽略）", exc_info=True)
        self._plan_mode_active = self._load_plan_mode()
        # 本地 hooks（用户级 + 项目级 .xenon/hooks.yaml；失败无影响）。
        self._hook_runner: Any = None
        try:
            from xenon.hooks.runner import HookRunner, set_default_runner

            self._hook_runner = HookRunner(
                user_dir=Path.home() / ".xenon" / "hooks.yaml",
                project_dir=self._workspace_root() / ".xenon" / "hooks.yaml",
                session_id=self._session_events.session_id
                if self._session_events is not None
                else "",
            )
            set_default_runner(self._hook_runner)
        except Exception:  # noqa: BLE001
            logger.debug("hooks 初始化失败（已忽略）", exc_info=True)

        # v0.5.3: 折叠思考过程 — 默认隐藏，Ctrl+O 展开
        self._show_thinking: bool = False
        self._last_thinking_panel: Any = None
        self._captured_log: str = ""  # 引擎执行期间捕获的日志文本
        self._last_mode_line: str = ""  # 上次引擎的模式行

        # v0.5.0: 工具权限门控
        from xenon.repl.permissions import PermissionGate, PermissionMode

        # 默认 ACCEPT_EDITS：文件编辑/写入自动放行，Shell 和危险 git 仍需确认。
        # 这在保留关键安全边界的同时，消除了编码工作流中高频写入操作的确认摩擦。
        self._permission_gate = PermissionGate(mode=PermissionMode.ACCEPT_EDITS)
        self._permission_gate.set_confirm_callback(self._confirm_tool)
        self.agent_context.set_tool_checkpoint_callback(self._persist_tool_checkpoint)
        # 能力不足时的升级询问：工具被本轮执行级别拦截时问用户一次。
        self._tool_escalations: set[str] = set()
        # “a=本会话总是允许”是带 TTL 的会话规则（用户决策），不是永久放行。
        self._approval_rules = SessionRuleStore()
        # 本轮已被边界审批一次性放行的工具（防旧权限门重复询问）。
        self._boundary_approved_once: set[str] = set()
        # 结构层：会话树（回合节点=状态单一来源），可从事件日志重建。
        from xenon.session.tree import TurnTree
        from xenon.turn.gate import TurnGate

        self._turn_tree: TurnTree = TurnTree()
        self._turn_tree_rebuilt = False
        self._turn_gate = TurnGate()
        self._last_gate_verdict = None
        self.agent_context.set_escalation_callback(self._confirm_tool_escalation)
        # 工具边界审批：工作区内写免问；越界写与命令要问（可 y/n/a，a 带 TTL）。
        self.agent_context.set_approval_callback(self._confirm_tool_approval)
        # 计划审批通道：submit_plan 工具由这里呈现面板。
        self.agent_context.set_plan_callback(self._confirm_plan)
        self.agent_context.set_checkpoint_callback(self._confirm_checkpoint)

        # LLM 意图分类器按需初始化：未启用/无可用模型时为 None，正则层兜底。
        self._intent_classifier: Any = None
        self._intent_classifier_checked: bool = False

        # 优雅重启管理器
        from xenon.repl.graceful_restart import GracefulRestartManager

        self._restart_manager = GracefulRestartManager(self)
        signal_ok = self._restart_manager.install_signal_handlers()
        if not signal_ok:
            logger.info("信号驱动重启不可用，请使用 /restart 命令")

    def _init_prompt_toolkit(self) -> None:
        if not _HAS_PROMPT_TOOLKIT:
            return

        from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
        from prompt_toolkit.keys import Keys
        from xenon.repl.completer import OmniCompleter

        # Terminals do not agree on a Shift+Enter wire encoding.  prompt_toolkit
        # 3.x collapses xterm's modifyOtherKeys form into a plain Enter and has
        # no built-in mapping for kitty's CSI-u form.  Preserve the modifier by
        # decoding both as Escape+Enter, which shares our Alt+Enter newline
        # binding while leaving the ordinary CR/Enter path untouched.
        shift_enter = (Keys.Escape, Keys.ControlM)
        ANSI_SEQUENCES["\x1b[13;2u"] = shift_enter
        ANSI_SEQUENCES["\x1b[27;2;13~"] = shift_enter

        cmd_names = list(COMMANDS.keys())
        self._completer = OmniCompleter(cmd_names)

        kb = KeyBindings()

        @kb.add("s-tab")
        def _(event):
            # Like Ctrl+O, the mode notification writes to stdout. Suspend the
            # active prompt first so the fixed toolbar is not overwritten.
            if run_in_terminal is not None:
                run_in_terminal(self._handle_shift_tab)
            else:
                self._handle_shift_tab()

        @kb.add("escape", "enter", eager=True)
        def _(event):
            event.current_buffer.insert_text("\n")

        @kb.add(Keys.BracketedPaste)
        def _(event):
            """Fold only the editor view; retain the exact paste for submit."""
            buffer = event.current_buffer
            visible = self._paste_store.compact(
                event.data,
                occupied_text=buffer.text,
            )
            buffer.insert_text(visible)

        @kb.add(Keys.Backspace)
        def _(event):
            """Treat a folded paste as one editor atom when deleting left."""
            buffer = event.current_buffer
            if buffer.selection_state is not None:
                buffer.cut_selection()
                return
            token = self._paste_store.token_before_cursor(
                buffer.text, buffer.cursor_position
            )
            buffer.delete_before_cursor(len(token) if token else 1)

        @kb.add(Keys.Delete)
        def _(event):
            """Treat a folded paste as one editor atom when deleting right."""
            buffer = event.current_buffer
            if buffer.selection_state is not None:
                buffer.cut_selection()
                return
            token = self._paste_store.token_after_cursor(
                buffer.text, buffer.cursor_position
            )
            buffer.delete(count=len(token) if token else 1)

        @kb.add(Keys.Left)
        def _(event):
            buffer = event.current_buffer
            token = self._paste_store.token_before_cursor(
                buffer.text, buffer.cursor_position
            )
            buffer.cursor_left(count=len(token) if token else 1)

        @kb.add(Keys.Right)
        def _(event):
            buffer = event.current_buffer
            token = self._paste_store.token_after_cursor(
                buffer.text, buffer.cursor_position
            )
            buffer.cursor_right(count=len(token) if token else 1)

        @kb.add("c-o")
        def _(event):
            """v0.5.3: Ctrl+O 切换显示/隐藏上次引擎执行的完整过程。

            折叠 → 展开：重新打印模式行、捕获的日志、推理面板。
            展开 → 折叠：重新打印折叠摘要行。
            """
            # prompt_toolkit owns the terminal while input is active. Writing
            # through Rich directly from this callback corrupts its input
            # area/bottom toolbar. run_in_terminal temporarily erases the
            # application, prints above it, then redraws it coherently.
            if run_in_terminal is not None:
                run_in_terminal(self._toggle_thinking_details)
            else:  # defensive fallback for unusual prompt_toolkit versions
                self._toggle_thinking_details()

        style = Style.from_dict(
            {
                # 输入区借鉴 Claude Code / pi 的轻量层次：线条定界，避免整块底色。
                "prompt": "bold #67e8f9",
                "input.rule": "#334155",
                "bottom-toolbar": "noreverse",
                "bottom-toolbar.text": "#94a3b8",
                "toolbar.separator": "#475569",
                "toolbar.model": "#cbd5e1",
                "toolbar.mode": "#c4b5fd",
                "toolbar.good": "bold #86efac",
                "toolbar.warning": "bold #fcd34d",
                "toolbar.danger": "bold #fda4af",
                "toolbar.notice": "bold #fde68a",
                "toolbar.muted": "#94a3b8",
                "toolbar.hint": "#64748b italic",
            }
        )

        history_path = _HISTORY_DIR / "input_history.txt"

        paste_store = self._paste_store

        class _PasteAwareFileHistory(FileHistory):
            """Never persist a UI-only paste token into reusable history."""

            def append_string(self, string: str) -> None:
                super().append_string(paste_store.expand(string))

        if os.environ.get("XENON_NO_PT") == "1":
            self._pt_session = None
        else:
            try:
                self._pt_session = PromptSession(
                    history=_PasteAwareFileHistory(str(history_path)),
                    completer=self._completer,
                    key_bindings=kb,
                    style=style,
                )
                # 状态栏不再用 bottom_toolbar：固定底部工具栏会把输入区与
                # 状态栏之间的空白拉满整屏。它由 _install_input_lower_rule
                # 作为内联行紧跟在输入下边界之后。
                self._install_input_lower_rule()
            except Exception:
                logger.debug("prompt_toolkit 初始化失败，回退自建输入", exc_info=True)
                self._pt_session = None

    def _toggle_thinking_details(self) -> None:
        """Render the last execution trace while prompt_toolkit is suspended."""
        if (
            self._last_thinking_panel is None
            and not self._captured_log
            and not self._last_mode_line
        ):
            console.print("\n[dim]· 暂无可展开的执行详情[/dim]\n")
            return

        self._show_thinking = not self._show_thinking
        console.print()
        if self._show_thinking:
            if self._last_mode_line:
                console.print(f"[dim]{self._last_mode_line}[/dim]")
            if self._captured_log:
                console.print(Text(self._captured_log.rstrip(), style="dim"))
            if self._last_thinking_panel is not None:
                console.print(self._last_thinking_panel)
            console.print("[dim]  💭 思考过程已展开  [Ctrl+O 折叠][/dim]")
        else:
            panel = self._last_thinking_panel
            if panel is not None:
                parts = []
                if panel.steps:
                    parts.append(f"{len(panel.steps)} 次迭代")
                if panel.tool_call_count:
                    parts.append(f"{panel.tool_call_count} 次工具调用")
                summary = " · ".join(parts) if parts else "无工具调用"
            else:
                summary = "无推理步骤"
            console.print(f"[dim]  💭 思考过程 · {summary}  [Ctrl+O][/dim]")
        console.print()

    def _install_input_lower_rule(self) -> None:
        """把下边界与状态栏都做成紧贴输入区的内联行。"""
        if self._pt_session is None:
            return

        from prompt_toolkit.layout.containers import VerticalAlign, Window
        from prompt_toolkit.layout.controls import FormattedTextControl

        root = self._pt_session.app.layout.container
        if not hasattr(root, "children") or not root.children:
            return

        # PromptSession 的主输入 FloatContainer 会占据状态栏上方的剩余空间。
        # 把规则线放进它内部并设为 TOP 对齐，规则线会紧随输入内容；外层仍然
        # 占满屏幕，因此原生 bottom_toolbar 继续固定在终端最底端。
        main = root.children[0]
        float_container = getattr(main, "alternative_content", None)
        main_stack = getattr(float_container, "content", None)
        if main_stack is None or not hasattr(main_stack, "children"):
            return
        main_stack.align = VerticalAlign.TOP

        # 默认 Buffer Window 会吞掉状态栏上方的全部剩余高度。按实际输入内容
        # 固定它的当前高度，空白空间便会留在下边界之后，而不是输入和下边界之间。
        buffer_container = (
            main_stack.children[1] if len(main_stack.children) > 1 else None
        )
        buffer_window = getattr(buffer_container, "content", None)

        def input_height() -> int:
            import shutil
            from prompt_toolkit.utils import get_cwidth

            document = self._pt_session.default_buffer.document
            available = max(20, shutil.get_terminal_size((80, 24)).columns - 5)
            visual_lines = 0
            for line in document.lines or [""]:
                visual_lines += max(1, (get_cwidth(line) + available - 1) // available)
            return max(1, min(10, visual_lines))

        if buffer_window is not None:
            buffer_window.height = input_height

        lower_rule = Window(
            FormattedTextControl(self.status_bar.get_input_rule_fragments),
            height=1,
            dont_extend_height=True,
        )
        main_stack.children.append(lower_rule)

        # 状态栏紧跟下边界：composer = 输入 + 下边界 + 状态栏。长会话时
        # prompt_toolkit 自然把它带到屏幕底部；短会话也不会凭空拉出整屏空白。
        status_window = Window(
            FormattedTextControl(self.status_bar.get_toolbar_fragments),
            height=1,
            dont_extend_height=True,
        )
        main_stack.children.append(status_window)

    @staticmethod
    def _diff_preview(tool_name: str, params: dict) -> str:
        """审批面板里的 diff 预览（实现见 xenon.repl.render）。"""

        from xenon.repl.render import diff_preview

        return diff_preview(tool_name, params)

    def _confirm_checkpoint(self, reason: str) -> str:
        """预算检查点续跑审批：y=继续下一窗口 / n=停止并交付进度草稿。fail-closed。"""

        if not sys.stdin.isatty():
            return "declined"
        with self._permission_prompt_lock:
            callback = getattr(self, "_active_callback", None)
            if hasattr(callback, "suspend_for_prompt"):
                callback.suspend_for_prompt()
            with self._terminal_waiting("等待续跑审批"):
                console.print()
                console.print(
                    Panel(
                        str(reason)[:800],
                        title="⏳ 预算检查点",
                        border_style="cyan",
                        padding=(0, 1),
                    )
                )
                try:
                    choice = Prompt.ask(
                        "是否继续执行下一窗口？",
                        choices=["y", "n"],
                        default="n",
                        show_choices=True,
                        case_sensitive=False,
                    )
                except (KeyboardInterrupt, EOFError):
                    choice = "n"
        if hasattr(callback, "resume_after_prompt"):
            callback.resume_after_prompt("budget_checkpoint", {})
        if choice == "y":
            self._record_event("checkpoint/continued", reason=str(reason)[:200])
            return "approved"
        self._record_event("checkpoint/stopped", reason=str(reason)[:200])
        return "declined"

    def _confirm_plan(self, plan_text: str) -> dict:
        """计划审批面板：批准退出计划模式，驳回让模型修改后重提。"""

        if not sys.stdin.isatty():
            return {"approved": False, "feedback": "非交互环境无法批准计划"}
        with self._permission_prompt_lock:
            callback = getattr(self, "_active_callback", None)
            if hasattr(callback, "suspend_for_prompt"):
                callback.suspend_for_prompt()
            with self._terminal_waiting("等待计划审批"):
                console.print()
                console.print(
                    Panel(
                        str(plan_text)[:1500],
                        title="📋 计划待审批",
                        border_style="cyan",
                        padding=(0, 1),
                    )
                )
                try:
                    choice = Prompt.ask(
                        "选择",
                        choices=["y", "n"],
                        default="n",
                        show_choices=True,
                        case_sensitive=False,
                    )
                except (KeyboardInterrupt, EOFError):
                    return {"approved": False, "feedback": "用户取消"}
        if choice == "y":
            self._plan_mode_active = False
            self._record_event("plan/approved", plan=str(plan_text)[:200])
            console.print("[dim]· 计划已批准，退出计划模式开始执行[/dim]")
            if hasattr(callback, "resume_after_prompt"):
                callback.resume_after_prompt("submit_plan", {})
            return {"approved": True, "feedback": ""}
        console.print("[dim]· 计划被驳回[/dim]")
        if hasattr(callback, "resume_after_prompt"):
            callback.resume_after_prompt("submit_plan", {})
        return {"approved": False, "feedback": "用户驳回，请修改计划后重新提交"}

    def _confirm_tool(
        self, tool_name: str, params: dict, risk: str
    ) -> tuple[bool, str]:
        """旧权限门的确认回调：委托给统一的边界审批面板。

        PermissionGate.check 仍负责模式语义（PLAN 只读 / BYPASS 全允许 /
        风险路由），但所有需要询问的路径都走同一个面板（工作区策略 +
        TTL 规则 + allowed-once），不再有自己的 CRITICAL 弹窗。
        """

        if get_config().interaction.assume_yes:
            return True, ""
        if not sys.stdin.isatty():
            return (
                False,
                "非交互环境无法确认危险操作；请显式使用 "
                "/permissions bypass 或设置 XENON_ASSUME_YES=1",
            )

        # 边界审批已经处理过（TTL 规则或本轮 allowed-once）时不再重复询问。
        if self._approval_rules.is_allowed(tool_name):
            return True, ""
        if tool_name in self._boundary_approved_once:
            return True, ""

        outcome = self._confirm_tool_approval(
            tool_name, params or {}, reason=f"风险等级 {risk}"
        )
        if outcome == APPROVAL_ALLOWED_ONCE:
            return True, ""
        if outcome == APPROVAL_CANCELLED:
            return False, "用户取消任务"
        return False, "用户拒绝"

    def _terminal_waiting(self, detail: str):
        """Return a waiting context, with a no-op fallback for partial REPLs."""
        from contextlib import nullcontext

        activity = getattr(self, "_terminal_activity", None)
        if activity is None:
            return nullcontext()
        return activity.waiting(detail)

    def _persist_tool_checkpoint(self, _checkpoint: dict[str, Any]) -> None:
        """Durably save an in-flight tool transition without maintenance work."""
        self._auto_save_session(cleanup=False)

    def _auto_save_session(self, *, cleanup: bool = True) -> None:
        """Atomically checkpoint the active session during use and on exit."""
        try:
            from xenon.repl.session import auto_save, cleanup_expired_sessions

            history = self.ctx_mgr.export_history()
            context_store = self.agent_context.to_dict()
            model_config = self.model_pool.to_config()
            auto_save(
                history=history,
                context_store=context_store,
                model_config=model_config,
                extra={
                    "paradigm": self.registry.current_mode,
                    "working_memory": self.ctx_mgr.get_working_memory(),
                },
            )
            if cleanup:
                cleanup_expired_sessions()
        except Exception as exc:
            logger.debug("自动保存失败（不影响当前会话）: %s", exc)

    def _check_auto_resume(self) -> None:
        """v0.4.0 Step 14: 启动时检查可恢复的会话。"""
        try:
            from xenon.repl.session import list_sessions, get_session_age

            sessions = list_sessions()
            if not sessions:
                return

            latest = sessions[0]
            age = get_session_age(latest) or latest.get("saved_at", "")[:16]
            name = latest["name"]
            if name.startswith("_auto"):
                name = "上次自动保存"
            console.print(
                f"\n[dim]┌─ {name} ({age}) · {latest['messages']} 条消息[/dim]"
            )
            if len(sessions) > 1:
                console.print(
                    f"[dim]│  输入 [bold]/resume[/bold] 从 {len(sessions)} 个历史会话中选择[/dim]"
                )
            else:
                console.print(
                    "[dim]│  输入 [bold]/resume[/bold] 恢复，或直接开始新对话[/dim]"
                )
        except Exception as exc:
            # 纯提示，失败不影响启动；但要留痕，否则"为什么没有恢复提示"无从排查。
            logger.debug("渲染历史会话提示失败: %s", exc)

    def _run_startup_resume(self) -> None:
        """执行 `xenon --resume <序号|名称>` 请求的启动即恢复。

        直接派发 /resume，让 CLI 入口和 REPL 内的 /resume 走同一条恢复路径；
        输出照常打印，用户能看到恢复了哪个会话、多少条消息。
        """
        target = getattr(self, "_startup_resume", None)
        if not target:
            return
        # 只消费一次：避免 run() 被重入时重复恢复、覆盖用户已有的对话。
        self._startup_resume = None
        try:
            self._handle_command(f"/resume {target}")
        except Exception as exc:
            # 恢复失败不该让 REPL 起不来，但必须让用户看见——否则会误以为
            # 会话已恢复，继续在空白上下文里提问。
            logger.warning("启动时恢复会话 %r 失败: %s", target, exc)
            console.print(f"\n[warning]⚠ 恢复会话 '{target}' 失败：{exc}[/warning]")

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
        except ValueError as exc:
            # 静默失败会让用户以为 Shift+Tab 这个键坏了。切换失败必须给反馈，
            # 并指向可用的替代路径。
            logger.warning("Shift+Tab 切换范式到 %r 失败: %s", next_mode_name, exc)
            console.print(
                f"\n[warning]⚠ 无法切换到范式 {next_mode_name}：{exc}[/warning]\n"
                "[dim]│  用 [bold]/mode[/bold] 查看并手动选择可用范式[/dim]"
            )

    def _render_engine_result(
        self, callback, result: str, title: str, border_style: str = "green"
    ) -> None:
        """渲染引擎结果：默认折叠执行过程，仅显示最终答案。

        v0.5.3: 执行日志（工具调用/HTTP 请求/引擎信息）默认全部隐藏。
        仅显示一条折叠摘要行（含迭代次数和工具调用次数）。
        用户通过 Ctrl+O 或 /thinking on 可展开查看完整执行过程。
        """
        # 有些引擎的异常路径不会触发 on_finish；这里统一清掉瞬时活动行。
        if hasattr(callback, "finish_activity"):
            callback.finish_activity()
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
            result = (
                "任务已执行，但未生成明确的回复内容。请尝试重新提问或使用更具体的指令。"
            )

        # v0.6.1: 安全网 —— 如果引擎返回的是未解析的 JSON 文本，
        # 尝试从 JSON 中提取 final_answer，避免用户看到裸 JSON。
        result = self._unwrap_json_result(result)

        if self._show_thinking:
            # ── 展开模式：重现完整执行过程（辅助信息全部 dim，只有最终答案高亮）──
            if self._last_mode_line:
                console.print(f"[dim]{self._last_mode_line}[/dim]")
            if self._captured_log:
                console.print(Text(self._captured_log.rstrip(), style="dim"))
            if panel is not None:
                console.print(panel)
            console.print("[dim]  💭 思考过程已展开  [Ctrl+O 折叠][/dim]")
        else:
            # ── 折叠模式：只保留一行摘要，完整轨迹由 Ctrl+O 展开 ──
            if panel is not None and panel.steps:
                header_parts = []
                if step_count:
                    header_parts.append(f"{step_count} 步")
                if tool_count:
                    header_parts.append(f"{tool_count} 个工具")
                error_count = sum(1 for step in panel.steps if step.is_error) + len(
                    panel.errors
                )
                if error_count:
                    header_parts.append(f"{error_count} 个错误")
                header = " · ".join(header_parts) if header_parts else ""
                console.print(f"[dim]  💭 {header}  [Ctrl+O 展开详情][/dim]")
            else:
                console.print("[dim]  💭 无工具调用[/dim]")

        # 发布门 v2：校验未通过的答案不发布为成功结果（草稿在失败面板内展示）。
        # 判定优先复用 TurnGate 缓存（回合内已判定过），避免重复评估。
        if panel is None:
            verification_ok, verification_reasons = True, []
        else:
            verdict = getattr(self, "_last_gate_verdict", None)
            if verdict is not None:
                verification_ok = verdict.outcome == "pass"
                verification_reasons = verdict.reasons
            else:
                verification_ok, verification_reasons = self._verify_turn(
                    panel, result
                )
        if verification_ok:
            # 最终答案始终显示；正文保持正常亮度，不再使用大边框。
            self._render_assistant_text(result, title=title)
        else:
            self._render_verification_failure(result, verification_reasons)

    @staticmethod
    def _render_verification_failure(draft: str, reasons: list[str]) -> None:
        """未通过发布门的答案（实现见 xenon.repl.render）。"""

        from xenon.repl.render import render_verification_failure

        render_verification_failure(draft, reasons)

    @staticmethod
    def _render_assistant_text(
        content: str, *, title: str = "Assistant", model_id: str | None = None
    ) -> None:
        """无边框渲染模型正文（实现见 xenon.repl.render）。"""

        from xenon.repl.render import render_assistant_text

        render_assistant_text(content, title=title, model_id=model_id)

    @staticmethod
    def _render_secondary_text(title: str, content: str) -> None:
        """无边框渲染辅助信息（实现见 xenon.repl.render）。"""

        from xenon.repl.render import render_secondary_text

        render_secondary_text(title, content)

    # v0.5.4: 从成功的工具调用中提取文件路径，更新工作记忆，
    # 使后续对话能知道"刚刚创建/修改了哪些文件"。
    _FILE_CREATE_TOOLS = {"write_file", "create_directory", "batch_write"}
    _FILE_MODIFY_TOOLS = {"edit_file", "command"}

    @staticmethod
    def _unwrap_json_result(result: str) -> str:
        """安全网：如果 result 是裸 JSON 文本，提取 final_answer（实现见 xenon.repl.render）。"""

        from xenon.repl.render import unwrap_json_result

        return unwrap_json_result(result)

    def _track_session_files(self, panel) -> None:
        """从 ThinkingPanel 中提取文件路径，更新 ContextManager 工作记忆。"""
        import os as _os

        created: list[str] = []
        modified: list[str] = []

        for step in panel.steps:
            if not step.action:
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

            # 失败的工具也要进事实日志（回合归档需要失败日志）；
            # 工作记忆仍只吸收成功结果。
            self._record_event(
                "tool/result",
                tool=str(action),
                success=not step.is_error,
                paths=[str(p) for p in paths[:20]],
                error=(
                    str(getattr(step, "observation", "") or "")[:120]
                    if step.is_error
                    else ""
                ),
            )

            # PostToolUse hook：exit 2 的 stderr 回传给用户/模型。
            if self._hook_runner is not None:
                try:
                    outcome = self._hook_runner.run(
                        "PostToolUse",
                        str(action),
                        dict(ai),
                        extra={
                            "success": not step.is_error,
                            "observation": str(
                                getattr(step, "observation", "") or ""
                            )[:200],
                        },
                    )
                    if outcome is not None and outcome.message:
                        console.print(
                            f"[dim]· hook 反馈: {outcome.message[:200]}[/dim]"
                        )
                except Exception:  # noqa: BLE001
                    pass

            if step.is_error or not paths:
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

    def _process_restart_request(self, preserve_session: bool) -> Any:
        """处理重启请求，包含三级异常兜底。

        Args:
            preserve_session: 是否保存会话

        Returns:
            RestartOutcome 对象
        """
        from pathlib import Path

        try:
            # 一级：正常执行
            outcome = self._restart_manager.perform_restart(preserve_session)
            if outcome.ok:
                console.print(f"\n[bold green]{outcome.message}[/bold green]\n")
            else:
                console.print(f"\n[bold yellow]{outcome.message}[/bold yellow]\n")
            return outcome

        except Exception as e:
            # 二级兜底：perform_restart 抛异常，尝试保存会话
            logger.error("重启过程异常", exc_info=True)
            console.print(f"\n[bold red]⚠ 重启失败: {e}[/bold red]\n")

            saved_path = None
            try:
                # 尝试自动保存会话
                saved_path = self._auto_save_session()
            except Exception:
                logger.error("二级兜底保存会话失败", exc_info=True)

                # 三级兜底：auto_save 也失败，查找最新会话文件
                try:
                    session_dir = Path.home() / ".xenon" / "sessions"
                    if session_dir.exists():
                        candidates = sorted(
                            session_dir.glob("*.json"),
                            key=lambda p: p.stat().st_mtime,
                            reverse=True,
                        )
                        if candidates and candidates[0].exists():
                            saved_path = candidates[0]
                except Exception:
                    logger.error("三级兜底查找会话文件失败", exc_info=True)

            # 构造提示消息
            if saved_path and Path(saved_path).exists():
                console.print(
                    f"[yellow]会话已保存到 {saved_path}\n"
                    f"REPL 将继续运行，可用 /resume 恢复会话[/yellow]\n"
                )
            else:
                console.print(
                    "[yellow]会话保存失败，请用 /sessions 查看可恢复的会话\n"
                    "REPL 将继续运行[/yellow]\n"
                )

            # 返回失败结果，让 REPL 继续运行
            from xenon.repl.graceful_restart import RestartOutcome

            return RestartOutcome(ok=False, message=str(e))

    @staticmethod
    def _default_system_prompt() -> str:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        weekdays_cn = [
            "星期一",
            "星期二",
            "星期三",
            "星期四",
            "星期五",
            "星期六",
            "星期日",
        ]
        current_date = (
            f"{now.year}年{now.month}月{now.day}日 {weekdays_cn[now.weekday()]}"
        )
        return (
            "你是 Xenon 的 AI 编程助手。"
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

    def _set_console_title(self) -> None:
        """Publish Xenon's static Star Core title before entering the REPL."""
        self._terminal_activity.start()

    def run(self) -> None:
        """启动 REPL 主循环。"""
        self._set_console_title()
        try:
            # Discover configured providers before rendering the welcome card
            # so MODEL reflects the runtime that will actually answer.
            startup = self._check_first_run()
            self._print_welcome()
            self._render_startup_summary(startup)

            # Non-model extensions are intentionally loaded after the welcome
            # card; they are local and do not affect its model status.
            self._load_custom_commands()
            self._preload_mcp_server_configs()

            # v0.4.0 Step 14: 检查可恢复的会话
            self._check_auto_resume()

            # 初始化系统消息
            if self.system_prompt:
                self.ctx_mgr.add_system_message(self.system_prompt)

            # xenon --resume <序号|名称>：进主循环前派发一次 /resume，
            # 恢复逻辑完全复用 /resume 处理器，不在此处重复实现。
            self._run_startup_resume()

            while True:
                # 检查重启请求（信号或 /restart 命令触发）
                should_restart, preserve = (
                    self._restart_manager.coordinator.should_restart()
                )
                if should_restart:
                    self._restart_manager.coordinator.clear()
                    outcome = self._process_restart_request(preserve)
                    if not outcome.ok:
                        # 重启失败，继续运行
                        continue

                # 显示状态栏（PT 模式由 bottom_toolbar 渲染，非 PT 模式才需单独打印）
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
                        self._print_exit_report()
                        console.print("\n[dim]再见！[/dim]")
                        break
                    self._pending_exit = True
                    console.print("\n[dim]· 已中断，按 Ctrl+C 再次退出[/dim]")
                    continue

                # 成功读取输入 → 重置 pending_exit
                self._pending_exit = False

                if not user_input:
                    continue

                # A submitted command/chat owns one activity scope. It settles
                # synchronously before the next input prompt is rendered.
                with self._terminal_activity.active():
                    # 斜杠命令
                    if user_input.startswith("/"):
                        if self._handle_command(user_input):
                            self._auto_save_session()
                            self._print_exit_report()
                            console.print("[dim]再见！[/dim]")
                            break
                        self._auto_save_session()
                        continue

                    # 多轮对话（带 prompt 优化）
                    self._handle_chat(user_input)
                    self._auto_save_session()
        finally:
            # 卸载信号处理器
            if hasattr(self, "_restart_manager"):
                self._restart_manager.uninstall_signal_handlers()
            # Stop the worker and restore the prior/best-effort shell title even
            # when setup, a command, or an engine raises unexpectedly.
            self._terminal_activity.close()

    def _print_exit_report(self) -> None:
        """P1-7：会话结束时打印省钱报告。"""
        if not hasattr(self, "_cache_tracker") or not self._cache_tracker:
            return
        cr = self._cache_tracker
        models = cr.all_models
        if not models:
            return

        total_calls = 0
        total_tokens = 0
        total_cost = 0.0
        total_saved = 0.0

        lines: list[str] = []
        for mid in models:
            snap = cr.model_snapshot(mid)
            if not snap:
                continue
            calls = snap.get("calls", 0)
            prompt_t = snap.get("prompt_tokens", 0)
            comp_t = snap.get("completion_tokens", 0)
            rate = snap.get("cache_hit_rate", 0.0)
            rate_text = f"{rate:.0%}" if snap.get("cache_reported_calls", 0) else "n/a"
            cost = snap.get("cost_yuan", 0.0)
            saved = snap.get("saved_yuan", 0.0)

            total_calls += calls
            total_tokens += prompt_t + comp_t
            total_cost += cost
            total_saved += saved

            short_name = mid.split("/")[-1] if "/" in mid else mid
            if calls > 0:
                lines.append(
                    f"[dim]{short_name}[/dim]  {calls} 次 · {prompt_t + comp_t:,}t "
                    f"· 💾{rate_text} · 💰¥{cost:.4f}"
                )

        if total_calls == 0:
            return

        overall_rate = f"{cr.cache_hit_rate:.0%}" if cr.cache_reported_calls else "n/a"
        overall_pct = cr.savings_pct

        console.print()
        console.print(
            Panel(
                "\n".join(lines)
                + f"\n\n[bold]合计[/bold]  {total_tokens:,} tokens · 💾{overall_rate} · 💰¥{total_cost:.4f} · 💡省 ¥{total_saved:.4f} ({overall_pct}%)",
                title="[bold]📊 本次会话省钱报告[/bold]",
                border_style="dim",
                padding=(0, 1),
            )
        )
        cr.close()

    def _on_clipboard_image(self, image_data: bytes) -> None:
        """剪贴板图片回调 — 惰性初始化 VisionBridge 并转录图片。"""
        if not self._vision_enabled:
            console.print("[dim]👁 视觉模式已关闭，输入 /vision 开启[/dim]")
            return

        # 惰性初始化：首次调用才连接模型池
        try:
            self._vision_bridge.lazy_init(self.model_pool)
        except Exception:
            pass  # 可能已经初始化过

        console.print("[dim]👁 正在用多模态模型转录图片...[/dim]")
        try:
            result = self._vision_bridge.describe_image(image_data)
        except RuntimeError as e:
            console.print(f"[yellow]⚠ {e}[/yellow]")
            console.print(
                "[dim]请配置一个多模态模型（如 gpt-4o-mini、claude-haiku、gemini-flash）。[/dim]"
            )
            return
        except Exception as e:
            console.print(f"[red]视觉转录失败: {e}[/red]")
            return

        # 注入到对话
        description = (
            f"[用户粘贴了一张图片，视觉模型 ({result.model_used}) "
            f"已将其转录为文字：]\n\n{result.text}"
        )
        self.ctx_mgr.add_user_message(description)
        console.print(
            f"[dim green]👁 图片已转录 ({result.model_used}, {len(result.text)} 字符, "
            f"{result.latency_ms:.0f}ms)[/dim green]"
        )
        # 自动触发一轮对话
        self._handle_chat(description)

    def _start_vision_monitor(self) -> None:
        """惰性启动剪贴板监听（首次调用时激活）。"""
        if not self._clipboard_monitor.is_running:
            self._clipboard_monitor.start()
            console.print("[dim]👁 视觉模式已开启 (Ctrl+Alt+V 粘贴图片)[/dim]")

    def _check_first_run(self) -> dict[str, Any]:
        """首次启动时检测配置状态，自动引导。

        v0.3.0+ 修复（C-2）：从纯 yaml 检查改为 get_configured_providers 检查，
        兼容 env 变量（Claude Code 内 ANTHROPIC_AUTH_TOKEN 也能触发自动加载）。

        v0.9.0 懒加载：**启动路径不再探测 provider**。实测启动 73% 的时间花在
        get_configured_providers() 的网络往返上（5 个 provider 约 1.9s，失效 key
        每次都完整走一遍 401/403，尾部可打满 MODEL_LIST_TIMEOUT=8s），而池子真正
        需要的模型早已由 models.yaml 经 from_config() 在 run() 之前填好（实测
        0.0001s、零网络）。因此这里只做本地判断，把探测延后到用户真正需要完整
        模型目录时（/model、/setup、vision 等入口自己会调）。

        注意这与被否决的 refresh_models=False 方案不同：那个方案把**内置硬编码
        兜底列表**（含 gpt-4o 等早已下线的型号）当作实时结果展示给用户；这里不
        展示任何未经探测的列表，只如实报告"已加载 N 个配置模型"，并提示用 /model
        查看完整目录。
        """
        from xenon.repl.provider_registry import load_credentials

        creds = load_credentials()

        # 启动只看本地事实：凭证文件 + models.yaml 填出来的池/注册表。
        # 三者皆空才是真的没配过，需要进 /setup 向导。
        needs_setup = (
            not creds
            and not self.registry.list_models()
            and not self.model_pool.list_all()
        )

        # 未探测，故无法得知哪些 provider 的模型列表可用；failures 留空而不是
        # 猜测。探测发生时（/model 等）由那条路径自己报告认证失败。
        failures: list[tuple[str, str]] = []
        return {
            "needs_setup": needs_setup,
            "probed": False,
            "configured_providers": 0,
            "available_providers": 0,
            "loaded_models": len(self.model_pool.list_all()),
            "failures": failures,
        }

    def ensure_providers_probed(self, *, force: bool = False) -> dict[str, Any]:
        """按需探测 provider 并把发现的模型补进池/注册表。

        这是懒加载契约的另一半：启动路径（_check_first_run）只用 models.yaml 的
        静态配置，真正需要完整模型目录时才由此方法付网络代价。逻辑与 v0.8.x 的
        启动探测完全一致（同样的 skip_benchmark、同样的"已在池里就不覆盖"），
        只是触发时机从"每次启动"变成"用户真正需要时"。

        get_configured_providers() 内部会缓存探测结果，因此同一会话内重复调用
        不会重复付网络代价；凭证变更时 save_credentials() 会自动作废该缓存。

        Args:
            force: 忽略缓存强制重新探测（/model refresh 等场景）

        Returns:
            与 _check_first_run 同构的摘要 dict，供调用方渲染。
        """
        from xenon.repl.provider_registry import (
            get_configured_providers,
            invalidate_provider_probe,
        )

        if force:
            invalidate_provider_probe()

        # httpx INFO is valuable in Ctrl+O execution details but is startup
        # noise during a provider capability probe. Suppress it only for this
        # narrow scope; --verbose preserves the original diagnostic stream.
        probe_loggers = ("httpx", "httpcore", "openai")
        saved_levels: dict[str, int] = {}
        if not self.verbose:
            for name in probe_loggers:
                target = logging.getLogger(name)
                saved_levels[name] = target.level
                target.setLevel(logging.WARNING)
        try:
            configured = get_configured_providers()
        finally:
            for name, level in saved_levels.items():
                logging.getLogger(name).setLevel(level)

        failures: list[tuple[str, str]] = []
        for provider in configured:
            if provider.model_error:
                failures.append(
                    (
                        provider.name,
                        self._summarize_provider_probe_error(provider.model_error),
                    )
                )

        # v0.4.0: always populate model pool from ALL configured providers
        _max_per_provider = get_config().limits.max_models_per_provider
        for p in configured:
            if not p.models or "(auto-fetch" in str(p.models[0]):
                continue
            if not p.key or not p.key.strip():
                logger.warning(
                    f"跳过空 key 的 provider（name={p.name!r}），model_id 会变成 /model_name 导致路由失败"
                )
                continue
            for model_name in p.models[
                :_max_per_provider
            ]:  # top N per provider (P0: 可配置)
                model_id = f"{p.key}/{model_name}"
                alias = model_name.replace(".", "-")
                # 探测出的模型如果已由 models.yaml 手工配置（在池里），跳过自动
                # 注册——保留那份配置（可能带自定义 weight/tier）。
                if self.model_pool.get(alias):
                    continue
                self.model_pool.register(
                    model_id,
                    alias=alias,
                    weight=3.0,
                    api_key=p.api_key,
                    base_url=p.base_url,
                    # benchmark 的 HF 端点已永久 404，tier 由 _infer_capability
                    # 推断，发这个请求纯属白等。
                    skip_benchmark=True,
                )
                # Also ensure registry has it (backward compat)
                if alias not in {m.alias for m in self.registry.list_models()}:
                    self.registry.add_model(model_id, alias)
                    if "planner" not in self.registry.role_priority:
                        self.registry.role_priority["planner"] = []
                    if alias not in self.registry.role_priority["planner"]:
                        self.registry.role_priority["planner"].append(alias)

        available_providers = sum(
            1
            for provider in configured
            if provider.models and not str(provider.models[0]).startswith("(auto-fetch")
        )
        return {
            "needs_setup": False,
            "probed": True,
            "configured_providers": len(configured),
            "available_providers": available_providers,
            "loaded_models": len(self.model_pool.list_all()),
            "failures": failures,
        }

    @staticmethod
    def _summarize_provider_probe_error(error: str) -> str:
        """Reduce provider errors to a safe, actionable startup label."""
        status_match = re.search(r"HTTP\s+(\d{3})", error, re.IGNORECASE)
        if status_match:
            status = int(status_match.group(1))
            if status in {401, 403}:
                return f"认证失败（HTTP {status}）"
            if status == 429:
                return "请求受限（HTTP 429）"
            if 500 <= status < 600:
                return f"服务暂不可用（HTTP {status}）"
            return f"模型列表请求失败（HTTP {status}）"
        lowered = error.lower()
        if any(token in lowered for token in ("timeout", "timed out")):
            return "连接超时"
        if any(token in lowered for token in ("connect", "network", "dns")):
            return "网络连接失败"
        return "模型列表获取失败"

    @staticmethod
    def _render_startup_summary(summary: dict[str, Any] | None) -> None:
        """Render concise startup facts after the model-aware welcome card."""
        if not summary:
            return
        if summary.get("needs_setup"):
            console.print(
                "[dim]· 尚未配置 API Key，输入 "
                "[bold cyan]/setup[/bold cyan] 进入配置向导[/dim]\n"
            )
            return

        loaded = int(summary.get("loaded_models", 0))
        providers = int(summary.get("available_providers", 0))
        if loaded:
            if summary.get("probed"):
                provider_text = f" · {providers} 个提供商" if providers else ""
                console.print(
                    f"[dim]· 已准备 {loaded} 个模型{provider_text}"
                    " · auto 按任务难度选择[/dim]"
                )
            else:
                # 懒加载：未探测 provider，因此不能声称"N 个提供商"（没探测就不
                # 知道哪些活着）。只如实报本地已加载的配置模型，并指出完整目录
                # 需要一次网络探测。
                console.print(
                    f"[dim]· 已加载 {loaded} 个配置模型 · auto 按任务难度选择"
                    " · [bold cyan]/model[/bold cyan] 查看完整目录[/dim]"
                )
        for name, reason in summary.get("failures", []):
            console.print(
                f"[dim yellow]· {name} 模型列表不可用：{reason}；已跳过[/dim yellow]"
            )
        console.print()

    def _print_welcome(self) -> None:
        """打印简洁的欢迎界面。

        设计原则：信息密度高、视觉噪音低。只展示关键状态——
        版本、范式、模型、一个实用提示。用 Unicode 细线框替代 ASCII 艺术。
        """
        import random

        # ── 启动动画 Logo（仅在交互模式播放）──
        if not self._logo_shown and sys.stdout.isatty():
            self._logo_shown = True
            try:
                from xenon.utils.logo import print_logo as _print_logo

                _print_logo(animated=True, duration=2.0)
            except Exception:
                pass  # Logo 加载失败不影响启动

        mode = self.registry.get_current_mode()
        models = self.registry.list_models()

        # ── 模型状态 ──
        if models:
            model_display = f"[bold green]{models[0].alias}[/bold green]"
            if len(models) > 1:
                model_display += f" [dim]+{len(models) - 1}[/dim]"
        else:
            model_display = (
                "[dim]未配置 — 输入 [bold cyan]/setup[/bold cyan] 开始[/dim]"
            )

        # ── 随机提示 ──
        tips = [
            "[bold cyan]/help[/bold cyan] 查看命令  [dim]·[/dim]  [bold cyan]/mode[/bold cyan] 切换范式",
            "Shift+Enter / Alt+Enter 多行输入  [dim]·[/dim]  Enter 发送  [dim]·[/dim]  Ctrl+C 退出",
            "[bold cyan]/setup[/bold cyan] 配置向导  [dim]·[/dim]  [bold cyan]/tools[/bold cyan] 查看工具  [dim]·[/dim]  [bold cyan]/mcp[/bold cyan] 扩展",
        ]
        tip = random.choice(tips)

        # Editable installs can keep stale distribution metadata after the
        # source tree version changes.  The running package constant is the
        # authoritative version for the welcome screen and ``--version``.
        from xenon import __version__ as _ver

        details = Table.grid(padding=(0, 2))
        details.add_column(style="dim #94a3b8", justify="right")
        details.add_column()
        details.add_row(
            "MODE",
            f"[bold #c4b5fd]{mode.name}[/bold #c4b5fd]  [dim]{mode.description}[/dim]",
        )
        details.add_row("MODEL", model_display)

        body = Table.grid(expand=True, padding=(0, 1))
        body.add_column(ratio=1)
        body.add_row(
            "[bold #f8fafc]Your AI coding workspace[/bold #f8fafc]\n[dim #94a3b8]Plan, build, and iterate without leaving the terminal.[/dim #94a3b8]"
        )
        body.add_row(details)
        body.add_row(f"[dim #64748b]TIP[/dim #64748b]  {tip}")

        console.print()
        console.print(
            Panel(
                body,
                title=f"[bold #67e8f9] XENON [/bold #67e8f9] [dim]v{_ver}[/dim]",
                subtitle="[dim]type /help to explore[/dim]",
                border_style="#155e75",
                box=box.ROUNDED,
                padding=(1, 2),
                width=min(76, max(48, console.width - 4)),
            )
        )
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
        """上下平行线定界输入；运行状态独立固定在终端屏幕底端。"""
        self._paste_store.reset()
        if hasattr(self, "_completer"):
            self._completer.update_commands(list(COMMANDS.keys()))
            if hasattr(self, "model_pool") and self.model_pool:
                self._completer.update_models(
                    [e.alias for e in self.model_pool.list_all()]
                )

        # 上边界属于多行 prompt；下边界由主输入布局追加并紧贴输入内容；
        # API/模型状态则由原生 bottom_toolbar 固定在整个终端屏幕底端。
        import shutil

        width = max(20, shutil.get_terminal_size((80, 24)).columns - 1)
        message: list[tuple[str, str]] = [
            ("class:input.rule", "─" * width),
            ("", "\n"),
            ("class:prompt", "  ❯ "),
        ]

        try:
            text = self._pt_session.prompt(message)
        except KeyboardInterrupt:
            raise KeyboardInterrupt
        except EOFError:
            raise KeyboardInterrupt

        # Strip only editor whitespace around the visible value, then restore
        # paste blocks.  Leading/trailing whitespace *inside* a pasted block is
        # therefore preserved exactly.
        return self._paste_store.expand(text.strip())

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
            from xenon.utils.llm_client import chat_completion

            model_ids = self.registry.get_role_priority("planner")
            effective = [
                m for m in model_ids if m not in getattr(self, "_failed_models", set())
            ]
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
        from xenon.repl.commands import ExitSignal

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
                # 懒加载契约：命令处理器需要 repl 才能触发 ensure_providers_probed()
                # （启动路径不再探测，/model 这类需要完整目录的命令自己按需付代价）
                repl=self,
            )
        except ExitSignal:
            return True

        if output:
            console.print(
                Panel(
                    output,
                    title=f"[bold]{cmd_name}[/bold]",
                    border_style="dim",
                    padding=(0, 1),
                )
            )
        return False

    def _resolve_bare_continuation(self, user_input: str) -> tuple[str | None, str]:
        """裸“继续”的确定性判定：树尾节点可续接 → (None, 续接输入)；否则 (提示, 原输入)。"""

        tail = self._turn_tree.tail()
        if tail is not self._turn_tree.root and tail.is_resumable:
            resumed = self._resume_prompt(tail) + user_input
            self._record_event(
                "turn/resumed", turn_id=tail.turn_id, status=tail.status
            )
            return None, resumed
        return (
            "· 没有可继续的未完成任务。请直接说明你想让我继续做什么。",
            user_input,
        )

    @staticmethod
    def _resume_prompt(node: Any) -> str:
        """续接提示（实现见 xenon.repl.turn_helpers）。"""

        return _resume_prompt_fn(node)

    def _ensure_turn_tree_rebuilt(self) -> None:
        """首次访问时从事件日志重建树（best effort，失败保留新树）。"""

        if getattr(self, "_turn_tree_rebuilt", False):
            return
        self._turn_tree_rebuilt = True
        try:
            log = getattr(self, "_session_events", None)
            if log is not None:
                from xenon.session.tree import TurnTree

                self._turn_tree = TurnTree.rebuild_from_events(log.read())
        except Exception:  # noqa: BLE001 — 重建失败不阻断会话
            logger.debug("TurnTree 重建失败（已忽略）", exc_info=True)

    def _finish_turn_node(
        self,
        status: str,
        *,
        engine: str = "",
        reasons: list[str] | None = None,
        retries_used: int = 0,
        panel: Any = None,
    ) -> None:
        """回合收尾：写树节点状态 + turn/verdict 事实（状态单一来源）。"""

        try:
            node = self._turn_tree.tail()
            fields: dict[str, Any] = {"engine": engine, "retries_used": retries_used}
            if panel is not None:
                fields["steps"] = len(getattr(panel, "steps", []) or [])
                fields["tools"] = int(getattr(panel, "tool_call_count", 0) or 0)
                fields["errors"] = sum(
                    1 for s in (getattr(panel, "steps", []) or []) if s.is_error
                ) + len(getattr(panel, "errors", []) or [])
                # R4: 产物单一来源——树节点 artifacts（回合内成功写入路径）。
                from xenon.repl.turn_helpers import paths_from_panel

                fields["artifacts"] = paths_from_panel(panel)[-10:]
            if reasons:
                fields["verdict_reasons"] = list(reasons)
            # 同因中断快速熔断（402 类基础设施错误）：连续两轮同因 → fused + 指引。
            if status == "interrupted" and reasons:
                parent = node.parent
                if parent is not None and parent is not self._turn_tree.root:
                    from xenon.session.tree import RESUMABLE_STATUSES

                    if (
                        parent.status in RESUMABLE_STATUSES
                        and parent.verdict_reasons[:1] == list(reasons)[:1]
                    ):
                        status = "fused"
                        fields["verdict_reasons"] = list(reasons) + [
                            "连续两轮同因中断（可能是 API 余额/配置问题），请检查后再继续"
                        ]
                        reasons = fields["verdict_reasons"]
            self._turn_tree.finish(node, status, **fields)
            self._record_event(
                "turn/verdict",
                turn_id=node.turn_id,
                status=status,
                reasons=list(reasons or []),
                engine=engine,
                steps=fields.get("steps", 0),
                tools=fields.get("tools", 0),
                errors=fields.get("errors", 0),
            )
        except Exception:  # noqa: BLE001 — 状态写入失败不阻断回合
            logger.debug("回合节点状态写入失败（已忽略）", exc_info=True)

    def _ensure_turn_node_finished(self) -> None:
        """纯对话路径（direct 无引擎）收尾：running 节点 → passed。"""

        try:
            node = self._turn_tree.tail()
            if node is not self._turn_tree.root and node.status == "running":
                self._turn_tree.finish(node, "passed", engine="direct")
        except Exception:  # noqa: BLE001
            pass

    def _auto_compact(self) -> bool:
        """用事件日志做确定性回合归档，替代 LLM 摘要压缩。"""

        log = getattr(self, "_session_events", None)
        if log is None:
            return False
        try:
            from xenon.session.archive import (
                build_turn_archives,
                render_archive_block,
            )

            events = log.read()
            archives = build_turn_archives(events)
            if not archives:
                return False
            block = render_archive_block(archives)
            if not block:
                return False
            self.ctx_mgr.compact(summary=block)
            self._record_event("compaction", turns_archived=len(archives))
            return True
        except Exception:  # noqa: BLE001 — 压缩失败不能阻断回合
            logger.debug("自动压缩失败（已忽略）", exc_info=True)
            return False

    def _verify_turn(self, panel, result: str) -> tuple[bool, list[str]]:
        """发布门：TurnGate 单一判定；失败记事实；判定缓存供渲染复用。"""

        try:
            from xenon.engine.task_verifier import extract_acceptance_criteria
            from xenon.turn.gate import tool_events_from_panel

            tool_events = tool_events_from_panel(panel)
            criteria = extract_acceptance_criteria(
                getattr(self, "_last_user_text", "") or ""
            )
            verdict = self._turn_gate.evaluate(
                result or "", tool_events=tool_events, criteria=criteria
            )
            self._last_gate_verdict = verdict
            if verdict.outcome != "pass":
                self._record_event("verification/failed", reasons=verdict.reasons)
            return verdict.outcome == "pass", verdict.reasons
        except Exception:  # noqa: BLE001 — 校验失败不能阻断输出
            logger.debug("任务校验执行失败（已忽略）", exc_info=True)
            return True, []

    def _rewind_to_turn(self, n: int) -> tuple[bool, list[str]]:
        """截断上下文到第 n 个用户回合；返回其后的文件产物清单。"""

        history = list(getattr(self.ctx_mgr, "history", []))
        user_turns = [t for t in history if getattr(t, "role", "") == "user"]
        if n < 1 or n > len(user_turns):
            return False, []
        target = user_turns[n - 1]
        kept = history[: history.index(target) + 1]
        self.ctx_mgr.clear()
        for turn in kept:
            self.ctx_mgr.add_message(
                str(getattr(turn, "role", "user")),
                str(getattr(turn, "content", "")),
                model_used=getattr(turn, "model_used", None),
                node_id=getattr(turn, "node_id", None),
                metadata=getattr(turn, "metadata", {}) or {},
                task_tier=int(getattr(turn, "task_tier", 3) or 3),
                turn_type=getattr(turn, "turn_type", "general") or "general",
                semantic_group_id=getattr(turn, "semantic_group_id", None),
            )
        self._pending_action = None
        self._boundary_approved_once.clear()
        # 结构层：指针回退到第 n 个回合节点（原枝保留，供 /fork 复用）。
        node = self._turn_tree.ancestor_at(n)
        if node is not None:
            self._turn_tree.move_to(node)
        self._record_event("rewind", turn=n)
        return True, self._artifacts_since_turn(n)

    def _artifacts_since_turn(self, n: int) -> list[str]:
        """事件日志里第 n 个用户回合之后的成功产物（只读提示）。"""

        log = getattr(self, "_session_events", None)
        if log is None:
            return []
        out: list[str] = []
        user_count = 0
        for event in log.read():
            if event.get("type") == "turn/user":
                user_count += 1
                continue
            if (
                event.get("type") == "tool/result"
                and event.get("success")
                and user_count > n
            ):
                for path in event.get("paths") or []:
                    if path and str(path) not in out:
                        out.append(str(path))
        return out

    def _record_event(self, event_type: str, **data: object) -> str | None:
        """Best-effort append to the additive session fact log."""

        log = getattr(self, "_session_events", None)
        if log is None:
            return None
        try:
            return log.append(event_type, **data)
        except Exception:  # noqa: BLE001 — 事实层失败不能影响回合
            logger.debug("会话事件记录失败（已忽略）", exc_info=True)
            return None

    def _recall_block(self, text: str) -> str:
        """Bounded read-only recall over past fact events (never authority)."""

        try:
            from xenon.session.query import recall

            directory = getattr(self._session_events, "directory", None)
            return recall(text, directory=directory)
        except Exception:  # noqa: BLE001 — 检索失败不能阻断回合
            logger.debug("历史检索失败（已忽略）", exc_info=True)
            return ""

    def _load_plan_mode(self) -> bool:
        """Last logged plan-mode state for this session (fail-safe: off)."""

        log = getattr(self, "_session_events", None)
        if log is None:
            return False
        try:
            active = False
            for event in log.read():
                if event.get("type") == "plan_mode/set":
                    active = bool(event.get("active"))
            return active
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _plan_mode_contract(contract: TurnContract) -> TurnContract:
        """计划模式转换：保留提案，但本轮不授予写/执行。"""

        from dataclasses import replace

        level = (
            ExecutionLevel.READ_ONLY
            if contract.operations & {"read", "network"}
            else ExecutionLevel.ANSWER_ONLY
        )
        return replace(
            contract,
            level=level,
            proposed_level=max(contract.level, contract.proposed_level),
            deferred_write=True,
            reason=f"{contract.reason}；计划模式：写/执行需用户确认",
        )

    @staticmethod
    def _goal_related(text: str, objective: str) -> bool:
        from xenon.session.query import extract_anchors

        tokens, grams = extract_anchors(text)
        _, objective_grams = extract_anchors(objective)
        if any(len(token) >= 3 and token in objective.lower() for token in tokens):
            return True
        return len(grams & objective_grams) >= 2

    def _get_intent_classifier(self) -> Any:
        """惰性获取 LLM 意图分类器；未启用/不可用时返回 None（正则兜底）。"""

        if self._intent_classifier_checked:
            return self._intent_classifier
        self._intent_classifier_checked = True
        try:
            from xenon.repl.llm_intent_classifier import get_llm_classifier

            classifier = get_llm_classifier()
            if classifier.enabled:
                self._intent_classifier = classifier
                logger.info("LLM 意图分类器已启用: %s", classifier.model)
            else:
                logger.debug("LLM 意图分类器未启用，使用正则层")
        except Exception as exc:  # noqa: BLE001 — 分类器不可用不能阻断 REPL
            logger.warning("意图分类器初始化失败，回退正则层: %s", exc)
        return self._intent_classifier

    def _workspace_root(self) -> Path:
        """当前工作区根：优先项目根，否则进程 cwd。"""

        root = getattr(self.project_ctx, "root", None)
        return Path(root) if root else Path.cwd()

    def _confirm_tool_approval(
        self,
        tool_name: str,
        params: dict,
        reason: str = "",
    ) -> str:
        """工具边界审批：工作区内写直接放行，越界写与命令询问。

        返回值是封闭结果词表：allowed-once / rejected / cancelled /
        unavailable；只有 allowed-once 是授权。非交互且无 assume_yes 时返回
        unavailable（fail-closed）。“a”登记带 TTL 的会话规则。
        """

        if self._approval_rules.is_allowed(tool_name):
            return APPROVAL_ALLOWED_ONCE
        if get_config().interaction.assume_yes:
            return APPROVAL_ALLOWED_ONCE
        if not sys.stdin.isatty():
            logger.info("非交互环境：%s 未获边界审批", tool_name)
            return APPROVAL_UNAVAILABLE

        # 写工具在工作区内免问（用户决策 1）。命令类没有目标路径，会走到询问。
        if not targets_outside_workspace(tool_name, params, self._workspace_root()):
            return APPROVAL_ALLOWED_ONCE

        brief = str(params.get("command") or params.get("action") or "")[:120]
        diff_text = self._diff_preview(tool_name, params)
        with self._permission_prompt_lock:
            callback = getattr(self, "_active_callback", None)
            if hasattr(callback, "suspend_for_prompt"):
                callback.suspend_for_prompt()
            with self._terminal_waiting("等待工具授权"):
                console.print()
                body = (
                    f"模型请求执行 [bold]{tool_name}[/bold]，超出工作区或属于命令执行。\n"
                    + (f"命令: {brief}\n" if brief else "")
                    + f"原因：{reason or '工具边界策略'}"
                )
                if diff_text:
                    body += f"\n\n{diff_text}"
                console.print(
                    Panel(
                        body,
                        title="需要授权",
                        border_style="yellow",
                        padding=(0, 1),
                    )
                )
                try:
                    choice = Prompt.ask(
                        "选择",
                        choices=["y", "n", "a", "q"],
                        default="n",
                        show_choices=True,
                        case_sensitive=False,
                    )
                except (KeyboardInterrupt, EOFError):
                    return APPROVAL_CANCELLED
        if choice == "a":
            ttl = self._approval_rules.allow(tool_name)
            self._boundary_approved_once.add(tool_name)
            console.print(
                f"[dim]· 本会话 {ttl / 60:.0f} 分钟内自动允许 {tool_name}"
            )
            if hasattr(callback, "resume_after_prompt"):
                callback.resume_after_prompt(tool_name, params)
            return APPROVAL_ALLOWED_ONCE
        if choice == "y":
            self._boundary_approved_once.add(tool_name)
            console.print("[dim]· 已授权本次操作[/dim]")
            if hasattr(callback, "resume_after_prompt"):
                callback.resume_after_prompt(tool_name, params)
            return APPROVAL_ALLOWED_ONCE
        if choice == "q":
            console.print("[dim]· 已取消任务[/dim]")
            if hasattr(callback, "resume_after_prompt"):
                callback.resume_after_prompt(tool_name, params)
            return APPROVAL_CANCELLED
        console.print("[dim]· 未授权[/dim]")
        if hasattr(callback, "resume_after_prompt"):
            callback.resume_after_prompt(tool_name, params)
        return APPROVAL_REJECTED

    def _confirm_tool_escalation(
        self,
        tool_name: str,
        required_level: int,
        reason: str,
    ) -> bool:
        """工具超出本轮级别时的询问式升级（能力不足 → 问用户，不硬拒）。"""

        if self._approval_rules.is_allowed(tool_name):
            return True
        if tool_name in self._tool_escalations:
            return True
        if get_config().interaction.assume_yes:
            return True
        if not sys.stdin.isatty():
            logger.info("非交互环境：工具 %s 未获授权，保持当前级别", tool_name)
            return False

        level_label = {1: "只读", 2: "写入", 3: "执行"}.get(
            int(required_level), str(required_level)
        )
        with self._permission_prompt_lock:
            callback = getattr(self, "_active_callback", None)
            if hasattr(callback, "suspend_for_prompt"):
                callback.suspend_for_prompt()
            with self._terminal_waiting("等待级别授权"):
                console.print()
                console.print(
                    Panel(
                        f"模型需要调用 [bold]{tool_name}[/bold]（{level_label}级），"
                        "本轮当前级别不足。\n"
                        f"原因：{reason}",
                        title="需要授权",
                        border_style="yellow",
                        padding=(0, 1),
                    )
                )
                try:
                    choice = Prompt.ask(
                        "选择",
                        choices=["y", "n", "a"],
                        default="n",
                        show_choices=True,
                        case_sensitive=False,
                    )
                except (KeyboardInterrupt, EOFError):
                    return False
        if choice == "a":
            ttl = self._approval_rules.allow(tool_name)
            self._tool_escalations.add(tool_name)  # 旧接口兼容
            console.print(
                f"[dim]· 本会话 {ttl / 60:.0f} 分钟内自动授权 {tool_name}[/dim]"
            )
            if hasattr(callback, "resume_after_prompt"):
                callback.resume_after_prompt(tool_name, None)
            return True
        if choice == "y":
            console.print("[dim]· 已授权本轮该级别[/dim]")
            if hasattr(callback, "resume_after_prompt"):
                callback.resume_after_prompt(tool_name, None)
            return True
        console.print("[dim]· 未授权[/dim]")
        if hasattr(callback, "resume_after_prompt"):
            callback.resume_after_prompt(tool_name, None)
        return False

    def _confirm_write_escalation(self, contract: TurnContract) -> TurnContract:
        """低置信的写入/执行提议：问用户一次，而不是静默授权或拒绝。"""

        if get_config().interaction.assume_yes:
            return contract.approve()
        if not sys.stdin.isatty():
            logger.info("非交互环境：写入提议未获确认，保持只读")
            return contract

        ops = "、".join(sorted(contract.operations)) or "未知操作"
        with self._permission_prompt_lock:
            console.print()
            console.print(
                Panel(
                    f"模型判断本轮可能需要：[bold]{ops}[/bold]"
                    f"（置信度 {contract.confidence:.0%}）\n"
                    f"依据：{contract.classifier_reasoning or '无'}",
                    title="需要确认",
                    border_style="yellow",
                    padding=(0, 1),
                )
            )
            try:
                choice = Prompt.ask(
                    "是否授权本轮操作",
                    choices=["y", "n"],
                    default="n",
                    show_choices=True,
                    case_sensitive=False,
                )
            except (KeyboardInterrupt, EOFError):
                return contract
        if choice == "y":
            console.print("[dim]· 已授权本轮操作[/dim]")
            return contract.approve()
        console.print("[dim]· 未授权；本轮按只读/对话处理[/dim]")
        return contract

    # ── 工具需求检测 ──────────────────────────────────────────

    @classmethod
    def _detect_tool_need(cls, text: str, intent: str | None = None) -> bool:
        """检测用户输入是否明确需要工具执行。

        ``query`` 和明确的读取/写入/执行请求需要工具；``write_code`` 只描述
        产物类型，默认在对话中返回，不能被当作写盘或执行授权。显式的“不写入”
        “不要运行”“只输出到对话”始终具有最高优先级。
        """
        # Intent does not authorize side effects.  In particular, write_code
        # defaults to returning code in chat unless the user explicitly asks
        # Xenon to persist or execute it.

        return build_turn_contract(text, fallback_intent=intent).requires_tools

    def _has_mcp_tools(self) -> bool:
        """检查是否有 MCP 服务器可用（含已连接和惰性）。"""
        try:
            registry = getattr(self, "_mcp_registry", None)
            if registry is None:
                return False
            return bool(registry.clients) or registry.has_pending_servers()
        except Exception:
            return False

    def _build_mcp_tools_list(self) -> str:
        """v0.5.4: 构建可用 MCP 工具/服务器列表，注入到引擎 system prompt。

        - 已连接的服务器：展示完整工具列表
        - 惰性（未连接）服务器：仅展示服务器名，提示按需调用

        LLM 需要知道有哪些 MCP 工具可用，才能正确调用 mcp_call。
        """
        registry = getattr(self, "_mcp_registry", None)
        if not registry:
            return ""

        parts: list[str] = [""]

        # 已连接的工具
        if registry.tool_map:
            tools_by_server: dict[str, list[tuple[str, str]]] = {}
            for full_name, (server_name, tool) in registry.tool_map.items():
                desc = (
                    tool.get("description", "")[:80]
                    if isinstance(tool, dict)
                    else str(tool)[:80]
                )
                tools_by_server.setdefault(server_name, []).append((full_name, desc))

            parts.append("当前可用的 MCP 工具：")
            for server, tools in sorted(tools_by_server.items()):
                parts.append(f"  [{server}]")
                for name, desc in tools:
                    parts.append(f"    - {name}: {desc}")

        # 惰性服务器（尚未连接）
        pending = registry.get_pending_server_names()
        if pending:
            parts.append("\n可用的 MCP 服务器（首次调用时自动连接）：")
            for name in sorted(pending):
                parts.append(
                    f'  - {name}:* — 使用 mcp_call tool_name="{name}:<工具名>" 调用'
                )

        return "\n".join(parts) if len(parts) > 1 else ""

    def _preload_mcp_server_configs(self) -> None:
        """v0.5.4: 启动时仅登记 MCP 服务器配置，不连接（惰性）。

        首次工具调用时才会真正启动子进程并发现工具，避免启动时阻塞。
        """
        from xenon.mcp.registry import MCPRegistry
        from xenon.repl.provider_registry import load_mcp_servers

        servers = load_mcp_servers()
        if not servers:
            return

        if not hasattr(self, "_mcp_registry") or self._mcp_registry is None:
            self._mcp_registry = MCPRegistry()
            self.agent_context.set("_mcp_registry", self._mcp_registry)

        pending_count = 0
        for s in servers:
            name = s.get("name", "")
            if not name:
                continue
            try:
                if s.get("url"):
                    headers = s.get("headers")
                    self._mcp_registry.add_server_pending(
                        name,
                        url=str(s["url"]),
                        headers=(
                            {str(k): str(v) for k, v in headers.items()}
                            if isinstance(headers, dict)
                            else None
                        ),
                    )
                else:
                    cmd = str(s.get("command", ""))
                    args = [str(a) for a in s.get("args", [])]
                    env = s.get("env")
                    if cmd:
                        self._mcp_registry.add_server_pending(
                            name,
                            command=cmd,
                            args=args,
                            env=(
                                {str(k): str(v) for k, v in env.items()}
                                if isinstance(env, dict)
                                else None
                            ),
                        )
                    else:
                        continue
                pending_count += 1
            except Exception as e:
                logger.debug(f"登记 MCP '{name}' 失败: {e}")

        if pending_count:
            console.print(
                f"[dim]· {pending_count} 个 MCP 服务器已登记（按需连接）[/dim]"
            )

    def _ensure_mcp_ready(self) -> None:
        """确保所有惰性 MCP 服务器已连接并发现工具。

        在 LLM 决定使用 mcp_call 时调用，或用户执行 /mcp tools 时调用。
        连接完成后更新 _mcp_tools_list 以供后续引擎注入。
        """
        registry = getattr(self, "_mcp_registry", None)
        if not registry or not registry.has_pending_servers():
            return

        console.print("[dim]· 正在连接 MCP 服务器...[/dim]", end="")
        try:
            registry.discover_tools()
            total = sum(len(c.tools) for c in registry.clients.values())
            console.print(
                f"[dim] 就绪（{len(registry.clients)} 个服务器，{total} 个工具）[/dim]"
            )
        except Exception as e:
            console.print(f"[dim] 部分失败: {e}[/dim]")

    _FILE_CLAIM_KEYWORDS: list[str] = [
        "已创建",
        "已经创建",
        "已生成",
        "已经生成",
        "已写入",
        "已经写入",
        "已保存",
        "已经保存",
        "已新建",
        "已经新建",
        "已建立",
        "已经建立",
        "创建了",
        "生成了",
        "写入了",
        "保存了",
        "新建了",
        "created",
        "written",
        "saved",
        "generated",
        "文件已",
        "目录已",
        "文件夹已",
    ]

    # LLM 拒绝性回复的关键词 — 表示它不知道怎么做，应该切换到 ReAct
    _DENIAL_KEYWORDS: list[str] = [
        "无法直接",
        "无法获取",
        "无法查询",
        "无法访问",
        "无法提供",
        "不能直接",
        "不能获取",
        "不能查询",
        "不能访问",
        "没有连接",
        "没有接入",
        "没有访问",
        "不具备",
        "不支持直接",
        "无法实时",
        "无法获取实时",
        "I cannot",
        "I can't",
        "I'm unable",
        "I don't have access",
        "I'm not able",
    ]

    @classmethod
    def _detect_tool_call_json(cls, text: str) -> bool:
        """检测 LLM 响应中是否包含未执行的工具调用 JSON/XML/DSML。

        direct 模式下 LLM 有时会输出 {"tool": "...", "arguments": {...}}
        或 {"action": "...", "action_input": {...}} 格式的工具调用，
        或 DeepSeek 的 DSML / 旧版 <uses_legacy_tools> XML 格式。
        因为 direct 模式不传工具定义，这些调用从未被执行。
        """
        if not text or len(text) < 20:
            return False
        import re as _re

        # JSON 格式
        patterns = [
            r'"tool"\s*:\s*"(?:list_files|read_file|write_file|command|web_fetch|docs_fetch|git|search_files|edit_file|clone_repo|github_fetch)"',
            r'"action"\s*:\s*"(?:list_files|read_file|write_file|command|web_fetch|docs_fetch|git|search_files|edit_file|clone_repo|github_fetch)"',
            r'"arguments"\s*:\s*\{',
            r'"action_input"\s*:\s*\{',
        ]
        for pattern in patterns:
            if _re.search(pattern, text, _re.IGNORECASE):
                return True
        # v0.7.0: XML 格式（DeepSeek 旧版模型）
        if _re.search(
            r"<uses_legacy_tools>|<tool_calls>|<tool_call\s+name=", text, _re.IGNORECASE
        ):
            return True
        # DeepSeek V4 may serialize tool calls into the content field using
        # full-width bars instead of returning OpenAI ``message.tool_calls``.
        normalized = text.replace("｜", "|")
        has_dsml_block = _re.search(
            r"<\|\|DSML\|\|tool_calls\b", normalized, _re.IGNORECASE
        )
        has_dsml_invoke = _re.search(
            r"<\|\|DSML\|\|invoke\s+name=", normalized, _re.IGNORECASE
        )
        if has_dsml_block and has_dsml_invoke:
            return True
        return False

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
            self.ctx_mgr.set_context_message(
                "project",
                ctx_text or None,
                stable=True,
            )
            if ctx_text:
                logger.debug(f"注入项目上下文: {self.project_ctx.project_type}")
        except Exception as e:
            logger.debug(f"项目上下文检测失败: {e}")

    def _inject_memories(self, user_input: str) -> None:
        """Inject bounded v2 memories and keep the v1 store as read compatibility."""
        blocks: list[str] = []
        self._pending_memory_use_ids = []
        try:
            service = self._get_memory_service()
            context_window = getattr(self.ctx_mgr, "max_tokens", None)
            retrieval_budget = service.policy.max_context_tokens
            if context_window:
                retrieval_budget = min(
                    retrieval_budget, max(1, int(context_window * 0.08))
                )
            relevant = service.retrieve(
                user_input,
                limit=5,
                token_budget=retrieval_budget,
            )
            memory_text = service.format_for_context(
                relevant,
                context_window=context_window,
            )
            if memory_text:
                blocks.append(memory_text)
                self._pending_memory_use_ids = [
                    record.id for record in relevant if f"id={record.id}" in memory_text
                ]
                logger.debug(f"注入 {len(self._pending_memory_use_ids)} 条 v2 相关记忆")
        except Exception as exc:
            logger.debug(f"v2 记忆检索失败: {exc}")

        try:
            from xenon.repl.memory import MemoryStore

            store = MemoryStore()
            relevant = store.get_relevant(user_input, limit=3)
            if relevant:
                memory_text = store.format_for_context(relevant)
                blocks.append(memory_text)
                logger.debug(f"注入 {len(relevant)} 条旧版相关记忆")
        except Exception as exc:
            logger.debug(f"旧版记忆检索失败: {exc}")

        # 失败或无命中时不能沿用上一轮不相关的检索结果。
        self.ctx_mgr.set_context_message(
            "long_term_memory",
            "\n\n".join(blocks) if blocks else None,
        )

    def _commit_memory_usage(self) -> None:
        """Persist successful context use after the answer has been recorded."""
        memory_ids = list(getattr(self, "_pending_memory_use_ids", []))
        self._pending_memory_use_ids = []
        if not memory_ids:
            return
        history = getattr(self.ctx_mgr, "history", [])
        last_user = max(
            (index for index, turn in enumerate(history) if turn.role == "user"),
            default=-1,
        )
        answer = next(
            (
                str(turn.content).strip()
                for turn in reversed(history[last_user + 1 :])
                if turn.role == "assistant"
            ),
            "",
        )
        if not answer or answer.startswith(("[错误]", "[已中断]")):
            return
        try:
            self._get_memory_service().mark_used(memory_ids)
        except Exception as exc:
            logger.debug(f"记忆使用计数写入失败: {exc}")

    def _get_memory_service(self):
        """Create the v2 service lazily after project detection."""
        if self._memory_service is not None:
            return self._memory_service
        from xenon.memory import MemoryBackendRegistry, MemoryService

        if not self.project_ctx._initialized:
            self.project_ctx.detect()
        self._memory_service = MemoryService(
            MemoryBackendRegistry(self.project_ctx.root)
        )
        self._session_state["memory_service"] = self._memory_service
        return self._memory_service

    def _get_memory_detector(self):
        if self._memory_detector is None:
            from xenon.memory import MemoryCandidateDetector

            self._memory_detector = MemoryCandidateDetector()
        return self._memory_detector

    def _has_active_project(self) -> bool:
        """Return the project boundary without forcing filesystem discovery."""
        project_ctx = getattr(self, "project_ctx", None)
        if project_ctx is not None:
            return getattr(project_ctx, "root", None) is not None
        # Lightweight/library tests may construct REPL via ``__new__`` and
        # inject an already-scoped memory service without ProjectContext.
        service = getattr(self, "_memory_service", None)
        registry = getattr(service, "registry", None)
        return bool(getattr(registry, "has_project", False))

    def _handle_explicit_memory_request(self, user_input: str) -> bool:
        """Persist an unambiguous user request immediately and print a receipt."""
        detector = self._get_memory_detector()
        proposal = detector.parse_reference(user_input)
        if proposal is not None:
            proposal.content = self._resolve_memory_reference()
            proposal.kind = detector.detect_kind(proposal.content)
        else:
            proposal = detector.parse_explicit(user_input)
        if proposal is None:
            return False
        if not proposal.content:
            console.print(
                "[error]❌ 未写入记忆：当前会话中没有可引用的上一条内容[/error]"
            )
            return True
        if not self._has_active_project() and proposal.scope.value.startswith(
            "project-"
        ):
            explicitly_project_scoped = bool(
                re.search(
                    r"(?:项目本地|项目共享|仓库共享|团队记忆|project[- ](?:local|shared))",
                    user_input,
                    re.IGNORECASE,
                )
            )
            if explicitly_project_scoped:
                console.print(
                    "[error]❌ 未写入记忆：当前未检测到项目；"
                    "请先进入具体项目目录，或明确使用用户全局记忆[/error]"
                )
                return True
            from xenon.memory import MemoryScope

            proposal.scope = MemoryScope.USER
            console.print("[dim]· 当前为无项目模式，默认写入用户全局记忆[/dim]")
        try:
            receipt = self._get_memory_service().remember(
                proposal.content,
                scope=proposal.scope,
                kind=proposal.kind,
                source="explicit-user-command",
                confidence=1.0,
                importance=0.7,
            )
        except ValueError as exc:
            console.print(f"[error]❌ 未写入记忆：{exc}[/error]")
            return True
        except Exception as exc:
            logger.exception("显式记忆写入失败")
            console.print(f"[error]❌ 记忆写入失败：{exc}[/error]")
            return True
        self._render_memory_receipt(receipt)
        return True

    def _resolve_memory_reference(self) -> str:
        """Resolve “this/previous item” to the latest visible conversational turn."""
        history = getattr(self.ctx_mgr, "history", [])
        for turn in reversed(history):
            if getattr(turn, "role", "") not in {"user", "assistant"}:
                continue
            content = str(getattr(turn, "content", "")).strip()
            if content and not content.startswith("[错误]"):
                return content
        return ""

    @staticmethod
    def _render_memory_proposal(proposal, destination: str, conflicts=()) -> None:
        table = Table.grid(padding=(0, 1))
        table.add_column(style="dim")
        table.add_column()
        table.add_row("内容", Text(proposal.content))
        table.add_row("原因", Text(proposal.reason))
        table.add_row("默认范围", proposal.scope.value)
        table.add_row("将写入", Text(destination))
        if conflicts:
            summary = "；".join(
                f"[{item.record.id}] {item.reason}" for item in conflicts[:3]
            )
            table.add_row("潜在冲突", Text(summary, style="yellow"))
        table.add_row(
            "选项",
            "s 保存 · e 编辑 · u 全局 · l 项目本地 · h 项目共享 · t 会话 · n 忽略",
        )
        console.print(
            Panel(
                table, title="🧠 Xenon 发现一条可能值得记住的信息", border_style="cyan"
            )
        )

    @staticmethod
    def _render_memory_receipt(receipt) -> None:
        action = "已写入" if receipt.created else "已去重并更新"
        lines = [
            f"{action} · ID: {receipt.record.id}",
            f"范围: {receipt.record.scope.value} · 类型: {receipt.record.kind.value}",
            f"位置: {receipt.destination}",
            f"内容: {receipt.record.content}",
            f"撤销: /memory archive {receipt.record.id}",
        ]
        if receipt.archived_ids:
            lines.append(f"容量治理已归档: {', '.join(receipt.archived_ids)}")
        if receipt.record.supersedes:
            lines.append(f"已替代: {receipt.record.supersedes}")
            lines[-1] += f" · 撤销替代: /memory rollback {receipt.record.id}"
        elif receipt.conflict_ids:
            lines.append(
                "潜在冲突（未自动覆盖）: "
                + ", ".join(receipt.conflict_ids)
                + "；可用 /memory replace <旧ID> <新内容> 明确替代"
            )
        if receipt.warning:
            lines.append(f"提示: {receipt.warning}")
        console.print(
            Panel(Text("\n".join(lines)), title="🧠 记忆回执", border_style="green")
        )

    def _load_custom_commands(self) -> None:
        """加载自定义快捷指令和技能，动态注册为命令。"""
        from xenon.repl.command_registry import register_command, _HANDLERS

        # 加载快捷指令
        try:
            from xenon.repl.shortcut_manager import ShortcutManager

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
            from xenon.repl.commands import _execute_installed_skill
            from xenon.repl.skill_manager import SkillManager

            skm = SkillManager()
            for sk in skm.list_all():
                cmd_name = f"/{sk.name}"
                if cmd_name not in _HANDLERS:

                    def make_skill_handler(sk_name):
                        def handler(
                            *,
                            args: str,
                            registry: ModelRegistry,
                            session_state=None,
                            **kwargs: Any,
                        ) -> str:
                            return _execute_installed_skill(
                                skm,
                                sk_name,
                                args,
                                registry=registry,
                                session_state=session_state,
                            )

                        return handler

                    _HANDLERS[cmd_name] = make_skill_handler(sk.name)
                    register_command(cmd_name, f"[技能] {sk.description}", cmd_name)
            if skm.list_all():
                console.print(f"[dim]· 已加载 {len(skm.list_all())} 个技能[/dim]")
            if skm.load_errors:
                console.print(
                    f"[yellow]⚠️  {len(skm.load_errors)} 个技能加载失败；"
                    "运行 /skill list 查看提示[/yellow]"
                )
        except Exception as e:
            logger.debug(f"加载技能失败: {e}")


# ── v0.5.3: 通用外部查询检测 ──────────────────────────────
# 不枚举具体领域（天气/高铁/酒店），基于语言结构判断输入是否
# 具有"信息查询"特征——即需要外部/实时数据才能回答的问题。
# 当 MCP 工具可用时，这类输入应路由到 ReAct 让 LLM 决定调用哪些工具。

# 疑问结构（通用语言特征，不依赖领域关键词）
# ── Method assignments from extracted input module ──
REPL._read_input_windows = _read_input_windows
REPL._read_input_unix = _read_input_unix

def start_repl(
    *,
    models: list[str] | None = None,
    mode: str | None = None,
    system_prompt: str | None = None,
    config_path: str | None = None,
    optimize: bool = True,
    verbose: bool = False,
    resume: str | None = None,
    auto_route: bool = False,
) -> None:
    """
    启动 REPL 的便捷入口。

    Args:
        models: 初始模型列表。
        mode: 初始思考范式。
        system_prompt: 自定义系统提示词。
        config_path: 配置文件路径。
        optimize: 是否启用 prompt 自动优化。
        verbose: 是否保留启动探测等详细诊断日志。
        resume: 非空时在进入主循环前恢复该会话（序号或名称），
            实现上直接派发一次 /resume，复用 REPL 内既有的恢复逻辑。
        auto_route: 是否启用智能路由（v0.6.0）。
    """
    registry = ModelRegistry()

    # 未显式 --config 时也要加载默认 models.yaml：它是手工配置的真相源，
    # 承载 max_tokens / weight / reasoning_effort 等 credentials.yaml 派生
    # 路径给不出的字段。漏掉它会让用户精心配置的模型被同名派生项取代。
    if config_path:
        registry.load_from_file(config_path)
    else:
        default_models = Path.home() / ".xenon" / "models.yaml"
        if default_models.exists():
            registry.load_from_file(default_models)

    # v0.8.5: 统一配置源 - 从 credentials.yaml 的 providers 段加载模型
    # models.yaml 中的配置优先（已在 load_from_file 中加载）
    registry.load_from_credentials()

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

    repl = REPL(
        registry=registry,
        system_prompt=system_prompt,
        optimize_prompts=optimize,
        verbose=verbose,
        resume=resume,
    )
    # P0: 打通 --config -> ModelPool。原仅喂 Registry,而 AutoRouter 只认 Pool,
    # 导致 --config 加载的模型形同虚设(池仍空)。复用 ModelPool.from_config。
    #
    # v0.8.6: 池是 AutoRouter 的唯一数据源，无论配置来自 --config 还是默认
    # models.yaml（上面 3310-3314 行）都必须喂；否则 weight/tier 只写进
    # Registry，AutoRouter 拿不到就退化成 get_role_priority 兜底顺序。
    # 之前这行在 if config_path: 里，导致无 --config 时池空、精心配置的
    # models.yaml 完全不生效（_check_first_run 的探测路径有个 1277 行
    # continue 跳过了 Registry 已有的模型，反而越是配好就越不进池）。
    repl.model_pool.from_config(registry.export_config().get("models", {}))
    # v0.5.3: 用户显式指定的模型优先于 auto-router 的选择
    if models:
        repl._preferred_model_ids = list(models)

    # v0.6.0: 智能路由启用逻辑
    # 优先级: CLI 参数 > 环境变量 > 配置文件 > 默认关闭
    import os
    should_enable_auto_route = auto_route  # CLI 参数优先

    if not should_enable_auto_route:
        # 检查环境变量
        env_auto_route = os.environ.get("XENON_AUTO_ROUTE", "").lower()
        if env_auto_route in ("1", "true", "yes", "on"):
            should_enable_auto_route = True
        elif not env_auto_route:
            # 检查配置文件
            config_file = Path.home() / ".xenon" / "config.yaml"
            if config_file.exists():
                try:
                    import yaml
                    with open(config_file, "r", encoding="utf-8") as f:
                        config = yaml.safe_load(f) or {}
                    should_enable_auto_route = config.get("auto_route", {}).get("enabled", False)
                except Exception:
                    pass  # 配置文件读取失败，使用默认值

    if should_enable_auto_route:
        repl.intelligent_router.enable()
        console.print("[dim]智能路由已启用[/dim]")

    repl.run()
