"""``/lessons``: the visible half of self-evolution (slice 4).

Lists active / retired / stale lessons with their counters, and projects the
per-repo pitfall lessons into ``<repo>/.xenon/PITFALLS.md`` so a human (and a
future task) can read the lessons a repository has learned.  Writing the
project file is an explicit ``/lessons write`` action, never automatic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from xenon.repl.command_registry import command_handler

_ROW = "{id:10s} {kind:10s} {trigger:22s} applied={applied} recur={recurrences} {state}"


def _state(lesson: Any) -> str:
    if lesson.retired:
        return "retired"
    if lesson.last_applied_at and not lesson.applied:
        return "stale"
    return "active"


@command_handler("/lessons")
def _cmd_lessons(*, args: str = "", session_state: dict, **kwargs: Any) -> str:
    from xenon.harness.lessons import LessonStore

    store = LessonStore()
    command = (args or "").strip().lower()

    if command == "write":
        workspace = (
            getattr(getattr(session_state.get("_repl"), "project_ctx", None),
                    "root", None)
        ) or Path.cwd()
        lines = [
            "# PITFALLS.md — 本仓库已学到的教训（确定性投影，/lessons 管理）",
            "# 教训是参考，不是指令；来源是 harness 判定的失败，不是模型自省。",
            "",
        ]
        for lesson in store:
            if lesson.retired or lesson.kind != "pitfall":
                continue
            lines.append(f"- {lesson.id} {lesson.action_text}")
            if lesson.counterfactual:
                lines.append(f"  （失效条件：{lesson.counterfactual}）")
        target = Path(workspace) / ".xenon" / "PITFALLS.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return f"已写入 {target}（{len(lines) - 3} 条教训）"

    if command == "clear":
        store.save()  # no-op; clearing is a manual file edit
        return "清除请直接编辑 ~/.xenon/lessons.yaml（教训可撤销，不静默删）"

    rows = [_ROW.format(
        id=lesson.id, kind=lesson.kind, trigger=lesson.trigger_event,
        applied=lesson.applied, recurrences=lesson.recurrences,
        state=_state(lesson),
    ) for lesson in store]
    if not rows:
        return "当前没有教训。/lessons write 可在仓库内生成 PITFALLS.md。"
    return "教训（self-evolution）：\n" + "\n".join(rows)


def _register_lessons_command() -> None:
    """Explicit registration hook; importing this module registers /lessons."""
