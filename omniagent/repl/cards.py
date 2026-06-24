"""
Rich 卡片组件库 — OmniAgent CLI 的统一终端 UI 组件。

提供可复用的 Rich 卡片组件，实现类似 Aider / gptme / Claude Code 的终端 UI 风格：
- 信息视觉分层（dim 思考 → 彩色工具卡片 → 高亮结果）
- 状态驱动的颜色编码（pending=yellow, running=cyan, success=green, error=red）
- 紧凑模式 / 完整模式自适应（读取工具紧凑，写入工具完整）

所有组件实现 ``__rich_console__`` 协议，可通过 ``console.print(Card(...))`` 直接渲染。
"""

from __future__ import annotations

from typing import ClassVar

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ═══════════════════════════════════════════════════════════════════
# 统一工具图标映射（合并自 REPL._TOOL_ICONS 和 ConsoleCallback）
# ═══════════════════════════════════════════════════════════════════

TOOL_ICONS: dict[str, str] = {
    # 文件操作
    "read_file": "📖",
    "write_file": "📄",
    "edit_file": "✏️",
    "batch_write": "📝",
    "batch_edit": "📝",
    "list_files": "📋",
    "search_files": "🔍",
    "create_directory": "📁",
    "move_file": "📦",
    "copy_file": "📋",
    "delete_file": "🗑️",
    # 执行类
    "command": "⚡",
    "git": "🔀",
    # 网络/数据
    "web_fetch": "🌐",
    "github_fetch": "🐙",
    "weather": "🌤️",
    "datetime": "🕐",
    # Agent 相关
    "mcp_call": "🔌",
    "spawn_agent": "🤖",
    "agent_result": "📬",
    # 分析
    "code_index": "📊",
    "ast_analyze": "🔬",
    "refactor": "🔧",
    "diff_preview": "📊",
}

# 写入/敏感工具（始终显示完整卡片）
_NOTIFY_TOOLS: set[str] = {
    "write_file", "edit_file", "batch_write", "batch_edit",
    "create_directory", "move_file", "copy_file", "delete_file",
    "command", "git", "mcp_call", "spawn_agent",
}

# 信息获取工具（紧凑显示）
_INFO_TOOLS: set[str] = {
    "read_file", "list_files", "search_files",
    "web_fetch", "github_fetch", "weather", "datetime",
}


def _is_notify_tool(tool_name: str) -> bool:
    return tool_name in _NOTIFY_TOOLS


def _status_color(status: str) -> str:
    """将逻辑状态映射到 Rich 颜色名。"""
    return {
        "pending": "yellow",
        "running": "bright_cyan",
        "success": "green",
        "error": "red",
        "denied": "red",
        "info": "dim",
    }.get(status, "dim")


def _status_icon(status: str) -> str:
    """将逻辑状态映射到 Unicode 状态图标。"""
    return {
        "pending": "○",
        "running": "◌",
        "success": "✅",
        "error": "❌",
        "denied": "⛔",
        "info": "",
    }.get(status, "")


def _truncate(text: str, max_len: int = 80) -> str:
    """截断文本，末尾追加 …"""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


# ═══════════════════════════════════════════════════════════════════
# 卡片组件
# ═══════════════════════════════════════════════════════════════════


class ToolCallCard:
    """工具调用卡片 — 带边框、图标、参数预览。

    视觉设计：
    - pending: yellow 边框，指示即将执行
    - running: bright_cyan 边框，指示正在执行
    - success: green 边框
    - error/denied: red 边框
    - 写入/命令工具: 完整模式（边框 + 参数列表）
    - 读取/搜索工具: 紧凑模式（单行 dim）

    Usage::

        card = ToolCallCard("write_file", {"file_path": "a.py", "content": "..."})
        console.print(card)
    """

    def __init__(
        self,
        tool_name: str,
        params: dict,
        *,
        status: str = "running",
        compact: bool | None = None,
    ) -> None:
        self.tool_name = tool_name
        self.params = params
        self.status = status

        # 自动判断紧凑模式：非 notify 工具默认紧凑
        if compact is None:
            compact = not _is_notify_tool(tool_name)
        self.compact = compact

    def __rich_console__(self, console, options):
        icon = TOOL_ICONS.get(self.tool_name, "🔧")
        color = _status_color(self.status)

        if self.compact:
            # 紧凑模式：单行，dim 风格，无边框
            params_brief = _format_params_brief(self.tool_name, self.params)
            line = f"  [dim]{icon} {self.tool_name}[/dim]"
            if params_brief:
                line += f" [dim]{params_brief}[/dim]"
            yield Text.from_markup(line)
            return

        # ── 完整模式：Panel 卡片 ──
        # Header: 图标 + 工具名
        header = f"[bold {color}]{icon} {self.tool_name}[/bold {color}]"

        # Body: 参数预览（最多 3 个关键参数，每行一个）
        body_lines: list[str] = []
        param_items = list(self.params.items())
        for k, v in param_items[:3]:
            v_str = str(v)
            v_display = _truncate(v_str, 100)
            body_lines.append(f"[dim]{k} = [/dim]{v_display}")
        if len(param_items) > 3:
            body_lines.append(f"[dim]… 及其他 {len(param_items) - 3} 个参数[/dim]")

        body = "\n".join(body_lines) if body_lines else "[dim](无参数)[/dim]"

        # Footer: 状态指示
        status_icon = _status_icon(self.status)
        footer = f"[{color}]{status_icon} {self.status}[/{color}]"

        full_content = f"{header}\n\n{body}\n\n{footer}"

        yield Panel(
            Text.from_markup(full_content),
            border_style=color,
            padding=(0, 1),
        )


class ToolResultCard:
    """工具执行结果卡片。

    Usage::

        card = ToolResultCard("write_file", success=True, summary="已写入: a.py (100 bytes)")
        console.print(card)
    """

    def __init__(
        self,
        tool_name: str,
        success: bool,
        summary: str,
        *,
        error: str | None = None,
        permission_denied: bool = False,
        circuit_breaker_tripped: bool = False,
    ) -> None:
        self.tool_name = tool_name
        self.success = success
        self.summary = summary
        self.error = error
        self.permission_denied = permission_denied
        self.circuit_breaker_tripped = circuit_breaker_tripped

    def __rich_console__(self, console, options):
        icon = TOOL_ICONS.get(self.tool_name, "🔧")
        tool_display = f"{icon} {self.tool_name}"

        if self.permission_denied:
            # 权限被拒
            content = f"[bold red]⛔ 已拒绝: {tool_display}[/bold red]\n\n"
            content += f"[dim red]{_truncate(self.summary, 200)}[/dim red]"
            yield Panel(
                Text.from_markup(content),
                border_style="red",
                padding=(0, 1),
            )
            return

        if self.circuit_breaker_tripped:
            # 断路器触发
            content = f"[bold red]🛑 断路器: {tool_display}[/bold red]\n\n"
            content += f"[dim red]{_truncate(self.summary, 200)}[/dim red]"
            yield Panel(
                Text.from_markup(content),
                border_style="red",
                padding=(0, 1),
            )
            return

        if self.success:
            # 成功
            content = f"[bold green]✅ {tool_display} 完成[/bold green]"
            if self.summary:
                content += f"\n[dim green]{_truncate(self.summary, 200)}[/dim green]"
            yield Panel(
                Text.from_markup(content),
                border_style="green",
                padding=(0, 1),
            )
            return

        # 失败
        err_text = self.error or self.summary or ""
        content = f"[bold red]❌ {tool_display} 失败[/bold red]\n\n"
        content += f"[dim red]{_truncate(err_text, 300)}[/dim red]"
        yield Panel(
            Text.from_markup(content),
            border_style="red",
            padding=(0, 1),
        )


class ThinkingCard:
    """LLM 思考内容卡片 — 降低视觉权重。

    dim 边框 + 斜体文字，不与工具卡片争夺注意力。
    """

    def __init__(
        self,
        thought: str,
        *,
        step_number: int | None = None,
        compact: bool = False,
    ) -> None:
        self.thought = thought
        self.step_number = step_number
        self.compact = compact

    def __rich_console__(self, console, options):
        if self.compact:
            # 单行紧凑
            prefix = f"[dim]🤔[/dim]" if self.step_number is None else f"[dim]#{self.step_number} 🤔[/dim]"
            thought_short = _truncate(self.thought.replace("\n", " "), 120)
            yield Text.from_markup(f"  {prefix} [dim italic]{thought_short}[/dim italic]")
            return

        # 完整卡片
        prefix = "💭 思考" if self.step_number is None else f"💭 思考 · 第 {self.step_number} 轮"
        yield Panel(
            Text.from_markup(f"[dim italic]{self.thought[:500]}[/dim italic]"),
            title=f"[dim]{prefix}[/dim]",
            border_style="dim",
            padding=(0, 1),
        )


class StepCard:
    """Plan-Execute 步骤卡片。

    显示编号步骤指示器 + 任务描述 + 状态。
    """

    def __init__(
        self,
        step_id: int,
        total: int,
        task: str,
        *,
        status: str = "pending",
    ) -> None:
        self.step_id = step_id
        self.total = total
        self.task = task
        self.status = status  # pending | running | done | failed

    def __rich_console__(self, console, options):
        color = _status_color(self.status)
        icon = _status_icon(self.status)
        if self.status == "running":
            icon = "▸"

        progress = f"[{color}]{icon}[/{color}]"
        number = f"[dim]步骤[/dim] [bold]{self.step_id}/{self.total}[/bold]"
        task_display = _truncate(self.task, 120)

        content = f"{progress} {number}\n[dim]{task_display}[/dim]"

        yield Panel(
            Text.from_markup(content),
            border_style=color,
            padding=(0, 1),
        )


class ErrorCard:
    """错误/警告卡片 — 红色醒目。

    包含错误标题 + 详情 + 可选的展开区域。
    """

    def __init__(
        self,
        message: str,
        *,
        title: str = "错误",
        details: str | None = None,
        is_warning: bool = False,
    ) -> None:
        self.message = message
        self.title = title
        self.details = details
        self.is_warning = is_warning

    def __rich_console__(self, console, options):
        if self.is_warning:
            color = "yellow"
            icon = "⚠️"
        else:
            color = "red"
            icon = "❌"

        content = f"[bold {color}]{icon} {self.title}[/bold {color}]\n\n"
        content += f"[{color}]{_truncate(self.message, 300)}[/{color}]"

        if self.details:
            content += f"\n\n[dim {color}]{_truncate(self.details, 500)}[/dim {color}]"

        yield Panel(
            Text.from_markup(content),
            border_style=color,
            padding=(0, 1),
        )


class ApprovalCard:
    """权限审批对话框卡片。

    极氪风格 (bright_cyan 边框) + 颜色编码的选项按钮。
    """

    def __init__(
        self,
        tool_name: str,
        params_preview: str,
        *,
        always_approved_count: int = 0,
    ) -> None:
        self.tool_name = tool_name
        self.params_preview = params_preview
        self.always_approved_count = always_approved_count

    def __rich_console__(self, console, options):
        icon = TOOL_ICONS.get(self.tool_name, "🔧")

        # 操作描述
        if self.tool_name in ("write_file", "edit_file", "batch_write", "batch_edit"):
            action_desc = "OmniAgent 需要写入文件"
        elif self.tool_name == "command":
            action_desc = "OmniAgent 需要执行命令"
        elif self.tool_name == "git":
            action_desc = "OmniAgent 需要执行 Git 操作"
        elif self.tool_name in ("create_directory", "move_file", "copy_file", "delete_file"):
            action_desc = "OmniAgent 需要操作文件系统"
        elif self.tool_name == "mcp_call":
            action_desc = "OmniAgent 需要调用外部 MCP 工具"
        elif self.tool_name == "spawn_agent":
            action_desc = "OmniAgent 需要启动子 Agent"
        else:
            action_desc = f"OmniAgent 需要调用工具 {icon} {self.tool_name}"

        # 构建内容
        content = f"[bold bright_cyan]{icon} {action_desc}[/bold bright_cyan]\n\n"
        content += f"[dim]{_truncate(self.params_preview, 250)}[/dim]\n\n"

        # 颜色编码选项
        content += "[dim]▸ 选择: [/dim]"
        content += "[bold green](y) 批准一次[/bold green]  "
        content += "[bold cyan](a) 始终批准[/bold cyan]  "
        content += "[bold red](n) 拒绝[/bold red]"

        # 全局授权状态
        if self.always_approved_count > 0:
            content += f"\n\n[dim cyan]📌 当前会话已授权 {self.always_approved_count} 项操作[/dim cyan]"

        yield Panel(
            Text.from_markup(content),
            title="[bold bright_cyan]◆ 权限审批[/bold bright_cyan]",
            border_style="bright_cyan",
            padding=(1, 2),
        )


class ModeHeader:
    """引擎模式启动头部卡片。

    替换当前 ``console.print("[cyan]...[/cyan]")`` 的单行模式声明。
    """

    def __init__(
        self,
        mode: str,
        *,
        description: str = "",
        iterations: int | None = None,
        extra_info: str = "",
    ) -> None:
        self.mode = mode
        self.description = description
        self.iterations = iterations
        self.extra_info = extra_info

    def __rich_console__(self, console, options):
        parts: list[str] = []

        if self.mode == "ReAct":
            parts.append("[bold bright_cyan]🔄 ReAct 模式[/bold bright_cyan]")
            parts.append("[dim]思考 → 行动 → 观察 → 循环[/dim]")
        elif self.mode == "Plan-Execute":
            parts.append("[bold bright_cyan]📋 Plan-Execute 模式[/bold bright_cyan]")
            parts.append("[dim]规划 → 逐步执行[/dim]")
        elif self.mode == "Reflection":
            parts.append("[bold bright_cyan]🔍 Reflection 模式[/bold bright_cyan]")
            parts.append("[dim]执行 → 审查 → 修正[/dim]")
        elif self.mode == "Direct":
            parts.append("[bold bright_cyan]💬 Direct 模式[/bold bright_cyan]")
            parts.append("[dim]直接对话[/dim]")
        else:
            parts.append(f"[bold bright_cyan]📐 {self.mode} 模式[/bold bright_cyan]")
            if self.description:
                parts.append(f"[dim]{self.description}[/dim]")

        if self.iterations is not None:
            parts.append(f"[dim]· 迭代预算: {self.iterations} 轮[/dim]")

        if self.extra_info:
            parts.append(f"[dim]· {self.extra_info}[/dim]")

        content = "  ".join(parts)

        # 底部操作提示
        footer = "[dim]按 Esc 或 Ctrl+C 中断任务执行[/dim]"

        yield Panel(
            Group(Text.from_markup(content), Text.from_markup(footer)),
            border_style="bright_cyan",
            padding=(0, 1),
        )


# ═══════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════


def _format_params_brief(tool_name: str, params: dict) -> str:
    """生成一行参数预览（用于紧凑模式）。"""
    if tool_name == "command":
        return _truncate(str(params.get("command", "")), 100)
    if tool_name == "git":
        git_cmd = params.get("git_command") or params.get("command", "")
        return _truncate(str(git_cmd), 80)
    if tool_name in ("write_file", "edit_file", "read_file"):
        return _truncate(str(params.get("file_path", "")), 80)
    if tool_name in ("list_files", "create_directory"):
        path = params.get("file_path") or params.get("path", "")
        return _truncate(str(path), 60)
    if tool_name == "search_files":
        pattern = params.get("search_pattern") or params.get("pattern", "")
        return _truncate(str(pattern), 60)
    if tool_name in ("web_fetch", "github_fetch"):
        return _truncate(str(params.get("url", "")), 80)
    # 通用：显示第一个关键参数
    for key in ("file_path", "path", "url", "query", "search_pattern"):
        if key in params:
            return _truncate(str(params[key]), 60)
    return ""


def render_shortcut_bar() -> Panel:
    """渲染底部快捷键提示栏。

    在每次引擎响应后显示，提醒用户可用快捷键。
    """
    content = (
        "[dim]"
        "Ctrl+C 退出 · Esc 中断 · /help 命令 · "
        "/mode 切换范式 · /model 切换模型 · "
        "/compact 压缩上下文"
        "[/dim]"
    )
    return Panel(
        Text.from_markup(content),
        style="dim",
        height=1,
        padding=(0, 1),
        border_style="dim",
    )
