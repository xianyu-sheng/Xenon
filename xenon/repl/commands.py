"""
Slash Commands — 斜杠命令处理器。

每个命令是一个独立的函数，接收 REPL 上下文并返回要显示的文本。
"""

from __future__ import annotations

from typing import TYPE_CHECKING


from xenon.repl.command_registry import (
    COMMANDS,  # noqa: F401 - compatibility export
    _HANDLERS,  # noqa: F401 - compatibility export
    ExitSignal,  # noqa: F401 - compatibility export
    command_handler,
    dispatch_command,  # noqa: F401 - compatibility export
    register_command,  # noqa: F401 - compatibility export
)
from xenon.repl.command_groups.runtime import (
    cmd_optimize as _cmd_optimize,  # noqa: F401 - compatibility export
    cmd_stream as _cmd_stream,  # noqa: F401 - compatibility export
    cmd_thinking as _cmd_thinking,  # noqa: F401 - compatibility export
    cmd_verbose as _cmd_verbose,  # noqa: F401 - compatibility export
)
from xenon.repl.command_groups.cache import (
    _CACHE_CAUSE_LABELS,  # noqa: F401 - compatibility export
    _CACHE_STATE_LABELS,  # noqa: F401 - compatibility export
    _cache_doctor,  # noqa: F401 - compatibility export
    _cache_explain,  # noqa: F401 - compatibility export
    _cache_history,  # noqa: F401 - compatibility export
    _cache_lanes,  # noqa: F401 - compatibility export
    _cache_optimize,  # noqa: F401 - compatibility export
    _cache_status,  # noqa: F401 - compatibility export
    _cache_tracker,  # noqa: F401 - compatibility export
    _cmd_cache,  # noqa: F401 - compatibility export
    _cmd_cost,  # noqa: F401 - compatibility export
    _cmd_fix_cache,  # noqa: F401 - compatibility export
)
from xenon.repl.command_groups.common import confirm_action
from xenon.repl.command_groups.skill import (
    _FUZZY_ALIASES,  # noqa: F401 - compatibility export
    _SKILL_FUZZY,  # noqa: F401 - compatibility export
    _cmd_skill,  # noqa: F401 - compatibility export
    _execute_installed_skill,  # noqa: F401 - compatibility export
    _extract_skill_name,  # noqa: F401 - compatibility export
    _fallback_skill_steps,  # noqa: F401 - compatibility export
    _format_skill_preview,  # noqa: F401 - compatibility export
    _fuzzy_match_subcommand,  # noqa: F401 - compatibility export
    _generate_skill_steps,  # noqa: F401 - compatibility export
    _parse_skill_steps,  # noqa: F401 - compatibility export
    _register_skill_handler,  # noqa: F401 - compatibility export
    _skill_auto_generate,  # noqa: F401 - compatibility export
    _skill_create_interactive,  # noqa: F401 - compatibility export
    _skill_import_from_url,  # noqa: F401 - compatibility export
    _skill_manual_create,  # noqa: F401 - compatibility export
)
from xenon.repl.command_groups.agent import (
    _cmd_ask,  # noqa: F401 - compatibility export
    _cmd_code,  # noqa: F401 - compatibility export
    _cmd_run,  # noqa: F401 - compatibility export
    _cmd_sub_agent,  # noqa: F401 - compatibility export
)
from xenon.repl.command_groups.model import (
    _cmd_import_models,  # noqa: F401 - compatibility export
    _cmd_mode,  # noqa: F401 - compatibility export
    _cmd_model,  # noqa: F401 - compatibility export
    _cmd_models,  # noqa: F401 - compatibility export
    _cmd_pool,  # noqa: F401 - compatibility export
    _cmd_provider,  # noqa: F401 - compatibility export
    _cmd_reload_models,  # noqa: F401 - compatibility export
    _cmd_remove_model,  # noqa: F401 - compatibility export
    _cmd_set_model,  # noqa: F401 - compatibility export
    _cmd_set_profile,  # noqa: F401 - compatibility export
    _cmd_set_role,  # noqa: F401 - compatibility export
    _model_hint_local,  # noqa: F401 - compatibility export
)

from xenon.repl.command_groups.memory_cmd import (
    _cmd_memory,  # noqa: F401 - compatibility export
    _cmd_memory_v2,  # noqa: F401 - compatibility export
)
from xenon.repl.command_groups.shortcut import (
    _cmd_shortcut,  # noqa: F401 - compatibility export
    _generate_shortcut_steps,  # noqa: F401 - compatibility export
    _parse_shortcut_steps,  # noqa: F401 - compatibility export
    _shortcut_auto_generate,  # noqa: F401 - compatibility export
    _shortcut_create_interactive,  # noqa: F401 - compatibility export
    _shortcut_manual_create,  # noqa: F401 - compatibility export
)
from xenon.repl.command_groups.resources import (
    _MCP_USAGE,  # noqa: F401 - compatibility export
    _cmd_library,  # noqa: F401 - compatibility export
    _cmd_mcp,  # noqa: F401 - compatibility export
    _cmd_setup,  # noqa: F401 - compatibility export
    _cmd_skill_discover,  # noqa: F401 - compatibility export
    _cmd_skill_install,  # noqa: F401 - compatibility export
    _cmd_status,  # noqa: F401 - compatibility export
    _cmd_tools,  # noqa: F401 - compatibility export
)
from xenon.repl.command_groups.session import (
    _cmd_clear,  # noqa: F401 - compatibility export
    _cmd_compact,  # noqa: F401 - compatibility export
    _cmd_config,  # noqa: F401 - compatibility export
    _cmd_context,  # noqa: F401 - compatibility export
    _cmd_exit,  # noqa: F401 - compatibility export
    _cmd_help,  # noqa: F401 - compatibility export
    _cmd_history,  # noqa: F401 - compatibility export
    _cmd_load,  # noqa: F401 - compatibility export
    _cmd_resume,  # noqa: F401 - compatibility export
    _cmd_save,  # noqa: F401 - compatibility export
    _cmd_sessions,  # noqa: F401 - compatibility export
    _cmd_undo,  # noqa: F401 - compatibility export
)
from xenon.repl.command_groups.workspace import (
    _cmd_edit,  # noqa: F401 - compatibility export
    _cmd_permissions,  # noqa: F401 - compatibility export
    _cmd_project,  # noqa: F401 - compatibility export
    _cmd_vision,  # noqa: F401 - compatibility export
)

if TYPE_CHECKING:
    pass

# ── 命令处理器 ────────────────────────────────────────────

# Private compatibility name retained for existing command modules/tests.
_handler = command_handler

# Backward-compatible alias for P3-Q8 confirmations.
_confirm = confirm_action


# Model and provider commands live in repl.command_groups.model.
# Agent execution commands live in repl.command_groups.agent.


# Memory commands live in repl.command_groups.memory_cmd.
# Shortcut commands live in repl.command_groups.shortcut.


# ── 智能路由命令 (v0.6.0) ────────────────────────────────

@command_handler("auto-route", "切换智能路由开关")
def cmd_auto_route(repl, args: str) -> str | None:
    """
    切换智能路由开关

    用法:
        /auto-route on    - 启用智能路由
        /auto-route off   - 禁用智能路由
        /auto-route       - 查看当前状态
    """
    from rich.console import Console
    from rich.table import Table

    console = Console()
    arg = args.strip().lower()

    if arg == "on":
        repl.intelligent_router.enable()
        console.print("\n[green]✓ 智能路由已启用[/green]")
        console.print(
            "[dim]系统将根据任务特征自动选择最合适的推理范式。\n"
            "你仍可使用 /mode 命令手动切换。[/dim]"
        )
        return None
    elif arg == "off":
        repl.intelligent_router.disable()
        console.print("\n[yellow]智能路由已禁用[/yellow]")
        console.print("[dim]范式切换恢复为完全手动控制。[/dim]")
        return None
    else:
        # 显示当前状态和统计
        status = "启用" if repl.intelligent_router.enabled else "禁用"
        stats = repl.intelligent_router.get_stats()

        table = Table(title=f"智能路由状态: {status}", border_style="dim")
        table.add_column("配置项", style="cyan")
        table.add_column("值", style="white")

        table.add_row("状态", "🟢 启用" if repl.intelligent_router.enabled else "⚫ 禁用")
        table.add_row("置信度阈值", f"{repl.intelligent_router.confidence_threshold:.0%}")
        table.add_row("用户通知", "是" if repl.intelligent_router.notify_user else "否")
        table.add_row("防降级保护", "是" if repl.intelligent_router.switch_only_on_improvement else "否")
        table.add_row("", "")
        table.add_row("总路由次数", str(stats["total_routes"]))
        table.add_row("成功决策次数", str(stats["successful_routes"]))
        table.add_row("范式切换次数", str(stats["switches"]))

        console.print()
        console.print(table)
        console.print(
            "\n[dim]提示: /auto-route on 启用, /auto-route off 禁用[/dim]"
        )
        return None
