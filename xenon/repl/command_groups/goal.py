"""/goal: show the projected goal, pending gate and known artifacts."""

from __future__ import annotations

from typing import Any

from xenon.repl.command_registry import command_handler, register_command

register_command("/goal", "查看当前目标与待批动作（投影视图）", "/goal")


@command_handler("/goal")
def _cmd_goal(*, session_state: dict, **kwargs: Any) -> str:
    repl = session_state.get("_repl")
    if repl is None:
        return "❌ /goal 需要交互会话"
    log = getattr(repl, "_session_events", None)
    if log is None:
        return "（无会话事件日志）"

    from xenon.session.projection import project

    try:
        view = project(log.read())
    except Exception:  # noqa: BLE001 - 投影失败不能影响命令
        return "（目标投影不可用）"

    lines = []
    if view.active_goal is not None:
        lines.append(f"🎯 当前目标：{view.active_goal.objective}")
    else:
        lines.append("🎯 当前没有进行中的目标")
    pending = getattr(repl, "_pending_action", None)
    if pending is not None:
        lines.append(f"⏸ 待确认动作：{pending.reason}")
    if view.open_gates:
        lines.append(f"📋 打开的门：{len(view.open_gates)}")
    if view.artifacts:
        lines.append(f"📄 已知产物：{len(view.artifacts)}（最近 {view.artifacts[-1].path}）")
    return "\n".join(lines)
