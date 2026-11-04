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
            console.print(
                Syntax(content, ext or "text", theme="monokai", line_numbers=False)
            )
            return ""
        except FileNotFoundError as exc:
            return str(exc)

    model_ids = registry.get_role_priority("planner")
    if not model_ids:
        return "❌ 未配置模型，无法使用 LLM 辅助编辑。请先 /set_model。"

    return CodeEditor.edit_with_llm(file_path, instruction, model_ids, confirm=True)


# ── /permissions — v0.5.0 权限模式管理 ─────────────────────

# /permissions lives in ``command_groups/permissions.py`` (#183): two modules
# registering the same name meant the later import silently won, and #373's
# "grants you can see and revoke" never reached a user.  Mode switching is part
# of that one command now; nothing here re-registers it.

# ══════════════════════════════════════════════════════════════
# /vision — 多模态视觉模式
# ══════════════════════════════════════════════════════════════

register_command(
    "/vision",
    "切换视觉粘贴模式 (Ctrl+Alt+V 粘贴图片，多模态模型转录)",
    "/vision [on|off]",
)


@command_handler("/vision")
def _cmd_vision(
    *, args: str = "", session_state: dict[str, Any] | None = None, **kwargs: Any
) -> str:
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

# /workspace ────────────────────────────────────────────────

register_command(
    "/workspace",
    "查看/扩展文件沙箱可写的目录（进程内有效）",
    "/workspace [add <路径> | remove <路径> | clear]",
)


@command_handler("/workspace")
def _cmd_workspace(*, args: str, session_state: dict[str, Any], **kwargs: Any) -> str:
    """Show and widen the write fence without restarting (#412).

    The fence is a feature: reads are free, writes are confined.  What was
    missing is a way to answer "the repo is one directory over" besides
    relaunching with an environment variable the user cannot discover.
    """

    from xenon.nodes import sandbox_roots
    from xenon.nodes.tool_node import _DEFAULT_EXTRA_ROOTS

    repl = session_state.get("_repl")
    root = Path(getattr(repl, "_workspace_root", lambda: Path.cwd())()) if repl else Path.cwd()
    tokens = args.strip().split(maxsplit=1)
    action = tokens[0].lower() if tokens else ""
    target = tokens[1].strip() if len(tokens) > 1 else ""

    if action == "add":
        if not target:
            return "用法：/workspace add <目录>"
        try:
            added = sandbox_roots.add_root(target)
        except (ValueError, OSError) as exc:
            return f"❌ 无法添加：{exc}"
        return (
            f"✅ 已加入可写目录（仅本进程有效）：{added}\n"
            f"工作区仍是 {root}；读写都会在下列根目录内放行。"
        )

    if action == "remove":
        if not target:
            return "用法：/workspace remove <目录>"
        removed = sandbox_roots.remove_root(target)
        return "✅ 已移除。" if removed else "⚠ 该目录不在本会话添加的列表里。"

    if action == "clear":
        sandbox_roots.clear_session_roots()
        return "✅ 已清空本会话添加的目录。"

    if action and action not in {"list", "status"}:
        return "用法：/workspace [add <目录> | remove <目录> | clear]"

    env = sandbox_roots.env_roots()
    session = sandbox_roots.session_roots()
    lines = [f"工作区（写入围栏的根）：{root}", "", "额外可写目录："]
    lines.append("  默认临时目录：")
    for item in _DEFAULT_EXTRA_ROOTS:
        lines.append(f"    · {item}")
    lines.append("  环境变量 XENON_EXTRA_ROOTS：" if env else "  环境变量 XENON_EXTRA_ROOTS：未设置")
    for item in env:
        lines.append(f"    · {item}")
    lines.append("  本会话添加（/workspace add）：" if session else "  本会话添加：无")
    for item in session:
        lines.append(f"    · {item}")
    lines.append("")
    lines.append("读取不受限（#378）；只有写和命令执行受上面的围栏约束。")
    return chr(10).join(lines)
