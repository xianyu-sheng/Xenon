"""Plan-mode slash command.

Plan mode is **logged soft guidance**: while active, requests get a planning
instruction and write/execute contracts are converted into pending approval
gates instead of running.  It never replaces the workspace/approval policy —
those stay independent.
"""

from __future__ import annotations

from typing import Any

from xenon.repl.command_registry import command_handler, register_command

register_command(
    "/plan",
    "切换计划模式（软引导，写/执行需确认）",
    "/plan [on|off|status]",
)


@command_handler("/plan")
def _cmd_plan(*, args: str, session_state: dict, **kwargs: Any) -> str:
    repl = session_state.get("_repl")
    if repl is None:
        return "❌ /plan 需要交互会话"

    arg = args.strip().lower()
    if arg in {"on", "开启", "打开"}:
        repl._plan_mode_active = True
    elif arg in {"off", "关闭", "退出"}:
        repl._plan_mode_active = False
    elif arg in {"", "status", "状态"}:
        state = "开启" if repl._plan_mode_active else "关闭"
        return f"📋 计划模式：{state}（/plan on|off 切换）"
    else:
        return "用法: /plan [on|off|status]"

    repl._record_event("plan_mode/set", active=bool(repl._plan_mode_active))
    state = "开启" if repl._plan_mode_active else "关闭"
    return f"✅ 计划模式已{state}"
