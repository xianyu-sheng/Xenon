"""/rewind / /fork: 会话回退与分叉（事件化，事实可回放）。"""

from __future__ import annotations

import os
import time
from typing import Any

from xenon.repl.command_registry import command_handler, register_command

register_command(
    "/rewind",
    "回退会话到第 N 个用户回合（列出其后的文件产物，不自动回滚）",
    "/rewind <n>",
)
register_command(
    "/fork",
    "从第 N 个用户回合分叉到新会话（可选 n，默认当前）",
    "/fork [n]",
)


@command_handler("/rewind")
def _cmd_rewind(*, args: str, session_state: dict, **kwargs: Any) -> str:
    repl = session_state.get("_repl")
    if repl is None:
        return "❌ /rewind 需要交互会话"
    try:
        n = int(args.strip())
    except ValueError:
        return "用法: /rewind <n>（n 为用户回合序号）"

    ok, artifacts = repl._rewind_to_turn(n)
    if not ok:
        return f"❌ 第 {n} 个用户回合不存在"
    lines = [f"✅ 已回退到第 {n} 个用户回合（上下文已截断，待批门已清除）。"]
    if artifacts:
        lines.append("该点之后的文件产物（未自动回滚，请自行用 git//review 处理）：")
        lines.extend(f"  - {p}" for p in artifacts[:20])
    return "\n".join(lines)


@command_handler("/fork")
def _cmd_fork(*, args: str, session_state: dict, **kwargs: Any) -> str:
    repl = session_state.get("_repl")
    if repl is None:
        return "❌ /fork 需要交互会话"
    log = getattr(repl, "_session_events", None)
    if log is None:
        return "❌ 无事件日志，无法分叉"

    arg = args.strip()
    n = int(arg) if arg.isdigit() and int(arg) >= 1 else None

    from xenon.session.events import SessionEventLog

    new_id = f"fork-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    new_log = SessionEventLog(new_id, directory=log.directory)
    user_count = 0
    for event in log.read():
        if event.get("type") == "turn/user":
            user_count += 1
            if n is not None and user_count > n:
                break
        new_log.append(
            event["type"],
            **{
                key: value
                for key, value in event.items()
                if key not in ("type", "id", "at")
            },
        )
    new_log.append("fork", parent=log.session_id, turns=user_count)
    repl._session_events = new_log

    artifacts: list[str] = []
    if n is not None:
        ok, artifacts = repl._rewind_to_turn(n)
        if not ok:
            return "❌ 目标回合不存在"
    lines = [f"✅ 已分叉到新会话 {new_id}（事件前缀已复制，含 fork 标记）。"]
    if artifacts:
        lines.append("该点之后的文件产物（未自动回滚）：")
        lines.extend(f"  - {p}" for p in artifacts[:20])
    return "\n".join(lines)
