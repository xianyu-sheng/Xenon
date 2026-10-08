"""/review: 列出本会话文件改动，并支持从 .bak 备份恢复。"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from xenon.repl.command_registry import command_handler, register_command

register_command(
    "/review",
    "查看本会话文件改动（可 revert 恢复 .bak 备份）",
    "/review [revert <路径>]",
)


@command_handler("/review")
def _cmd_review(*, args: str, session_state: dict, **kwargs: Any) -> str:
    repl = session_state.get("_repl")
    if repl is None:
        return "❌ /review 需要交互会话"

    arg = args.strip()
    if arg.startswith("revert"):
        parts = arg.split(None, 1)
        if len(parts) != 2 or not parts[1].strip():
            return "用法: /review revert <路径>"
        path = Path(parts[1].strip())
        backup = Path(str(path) + ".bak")
        if not backup.exists():
            return (
                f"❌ {path} 没有 .bak 备份（只有 edit_file 会产生备份；"
                "新写/覆盖的文件请用 git 或手动恢复）"
            )
        shutil.copy2(backup, path)
        return f"✅ 已从 {backup} 恢复 {path}"

    memory = repl.ctx_mgr.get_working_memory()
    created = list(memory.get("session_created_files", []))[-20:]
    modified = list(memory.get("session_modified_files", []))[-20:]

    lines = ["本会话文件改动："]
    for path in created:
        lines.append(f"  [新增] {path}")
    for path in modified:
        backup = "（有 .bak，可 /review revert）" if Path(str(path) + ".bak").exists() else ""
        lines.append(f"  [修改] {path} {backup}")
    if len(lines) == 1:
        return "本会话没有记录到文件改动"
    return "\n".join(lines)
