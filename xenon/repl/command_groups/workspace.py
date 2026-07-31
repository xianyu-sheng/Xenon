"""Workspace-oriented slash commands.

This group contains commands that inspect or configure the active project,
editor integration, permission mode, and optional visual clipboard mode.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from xenon.repl.command_groups.common import console
from xenon.repl.command_registry import command_handler, register_command

if TYPE_CHECKING:
    from xenon.repl.model_registry import ModelRegistry


# /project ──────────────────────────────────────────────────

register_command("/project", "查看/刷新项目上下文", "/project [refresh]")


@command_handler("/project")
def _cmd_project(*, args: str, session_state: dict[str, Any], **kwargs: Any) -> str:
    repl = session_state.get("_repl")
    if not repl:
        return "❌ 无法访问 REPL 实例。"

    pc = repl.project_ctx

    if args.strip().lower() == "refresh":
        pc.refresh()
        repl._project_injected = False
        repl._memory_service = None
        repl._session_state.pop("memory_service", None)
        return f"✅ 项目上下文已刷新。\n\n{pc.get_summary()}"

    if not pc._initialized:
        pc.detect()

    summary = pc.get_summary()

    tree_preview = ""
    if pc.file_tree:
        tree_lines = pc.file_tree.splitlines()[:30]
        tree_preview = "\n\n[文件树预览]\n" + "\n".join(tree_lines)
        if len(pc.file_tree.splitlines()) > 30:
            tree_preview += f"\n... (共 {len(pc.file_tree.splitlines())} 项)"

    return f"{summary}{tree_preview}"


# /edit ─────────────────────────────────────────────────────

register_command("/edit", "编辑代码文件（支持 LLM 辅助）", "/edit <file_path> [指令]")


@command_handler("/edit")
def _cmd_edit(*, args: str, registry: ModelRegistry, **kwargs: Any) -> str:
    from xenon.repl.code_editor import CodeEditor

    parts = args.strip().split(maxsplit=1)
    if not parts:
        return "用法: /edit <file_path> [修改指令]\n\n  /edit app.py  — 交互式查看文件\n  /edit app.py 把所有函数名改为驼峰 — LLM 辅助修改"

    file_path = parts[0]
    instruction = parts[1] if len(parts) > 1 else ""

    if not instruction:
        try:
            content, line_count = CodeEditor.read_file(file_path)
            from rich.syntax import Syntax

            ext = Path(file_path).suffix.lstrip(".")
            console.print(f"\n[bold]{file_path}[/bold] ({line_count} 行)\n")
            console.print(Syntax(content, ext or "text", theme="monokai", line_numbers=False))
            return ""
        except FileNotFoundError as exc:
            return str(exc)

    model_ids = registry.get_role_priority("planner")
    if not model_ids:
        return "❌ 未配置模型，无法使用 LLM 辅助编辑。请先 /set_model。"

    return CodeEditor.edit_with_llm(file_path, instruction, model_ids, confirm=True)


# ── /permissions — v0.5.0 权限模式管理 ─────────────────────

register_command("/permissions", "查看/切换工具执行权限模式", "/permissions [default|accept_edits|bypass|plan]")


@command_handler("/permissions")
def _cmd_permissions(
    *, args: str, session_state: dict[str, Any] | None = None, **kwargs: Any
) -> str:
    repl = session_state.get("_repl") if session_state else None
    if not repl or not hasattr(repl, "_permission_gate"):
        return "权限系统未初始化"

    gate = repl._permission_gate
    current_mode = gate.mode.value

    if not args:
        lines = [
            f"当前权限模式: [bold cyan]{current_mode}[/bold cyan]",
            "",
            "可用模式:",
            "  [bold]default[/bold]      — 写入/Shell 操作前确认",
            "  [bold]accept_edits[/bold] — 自动批准编辑，Shell 仍需确认",
            "  [bold]bypass[/bold]       — 跳过所有确认（CI/自动化场景）",
            "  [bold]plan[/bold]         — 只读模式，拒绝所有写入",
            "",
            "用法: /permissions <模式名>",
            f"最近确认状态: {gate.state.value}",
            "已记忆允许的工具: "
            + (", ".join(gate.session_allowed_tools) if gate.session_allowed_tools else "(无)"),
        ]
        return "\n".join(lines)

    mode_map = {
        "default": "DEFAULT",
        "accept_edits": "ACCEPT_EDITS",
        "bypass": "BYPASS",
        "plan": "PLAN",
    }
    mode_key = mode_map.get(args.strip().lower())
    if not mode_key:
        return f"未知模式: {args}。可用: default, accept_edits, bypass, plan"

    from xenon.repl.permissions import PermissionMode

    new_mode = getattr(PermissionMode, mode_key)
    gate.set_mode(new_mode)
    gate.reset_session()
    return f"✅ 权限模式已切换为: [bold cyan]{new_mode.value}[/bold cyan]"


# ══════════════════════════════════════════════════════════════
# /vision — 多模态视觉模式
# ══════════════════════════════════════════════════════════════

register_command(
    "/vision",
    "切换视觉粘贴模式 (Ctrl+Alt+V 粘贴图片，多模态模型转录)",
    "/vision [on|off]",
)


@command_handler("/vision")
def _cmd_vision(*, args: str = "", session_state: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """切换视觉粘贴模式。"""
    repl = session_state.get("_repl") if session_state else None
    if not repl:
        return "[dim]REPL 未初始化[/dim]"

    bridge = getattr(repl, "_vision_bridge", None)
    if not bridge:
        return "[dim]视觉桥接器未初始化[/dim]"

    arg = args.strip().lower()
    if arg == "on":
        repl._vision_enabled = True
        repl._start_vision_monitor()
        return (
            "[bold green]👁 视觉模式已开启[/bold green]\n"
            "按 [bold]Ctrl+Alt+V[/bold] 粘贴剪贴板图片，"
            "系统将自动用多模态模型转录为文字。"
        )
    if arg == "off":
        repl._vision_enabled = False
        if repl._clipboard_monitor.is_running:
            repl._clipboard_monitor.stop()
        return "[dim]👁 视觉模式已关闭[/dim]"

    status = "开启" if repl._vision_enabled else "关闭"
    return (
        f"👁 视觉模式: [bold]{status}[/bold]\n"
        "用法: /vision on 开启 | /vision off 关闭\n"
        "开启后按 Ctrl+Alt+V 粘贴剪贴板图片"
    )
