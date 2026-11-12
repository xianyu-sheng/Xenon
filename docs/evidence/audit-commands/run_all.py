"""逐个执行每个斜杠命令，看它到底做了什么。

判据不是"有没有输出"，而是：
  * 有没有报错（未实现 / 依赖缺失）
  * 输出是不是空或纯占位文案
  * 是否真的改变了某处状态

只读命令安全；会改状态的命令（/save /clear /remove_model …）单独标注，
执行时跳过或只做 --dry 形式，避免污染真实配置。
"""

from __future__ import annotations

import io
import sys
import tempfile
from contextlib import redirect_stdout

sys.path.insert(0, r"D:\Xenon")

from xenon.repl.command_registry import ExitSignal  # noqa: E402
from xenon.repl.commands import COMMANDS, dispatch_command  # noqa: E402
from xenon.repl.context_manager import ContextManager  # noqa: E402
from xenon.repl.model_registry import ModelRegistry  # noqa: E402
import xenon.repl.session as session_module  # noqa: E402

# 会写盘 / 改配置的命令，执行时只跑"查看"形式或不跑。
DESTRUCTIVE = {
    "/save", "/load", "/resume", "/clear", "/remove_model", "/import_models",
    "/reload_models", "/set_model", "/set_role", "/set_profile", "/mode",
    "/restart", "/setup", "/trust-hooks", "/undo", "/rewind", "/fork",
    "/exit", "/quit", "/bye", "/compact", "/mcp", "/library", "/refresh",
}


def main() -> int:
    session_module.SESSIONS_DIR = tempfile.mkdtemp()

    registry = ModelRegistry()
    registry.add_model("test/fake", "fake")
    ctx = ContextManager()

    problems: list[tuple[str, str]] = []
    rows: list[tuple[str, str, bool]] = []

    for name in sorted(COMMANDS):
        if name in DESTRUCTIVE:
            rows.append((name, "(跳过：会改状态)", True))
            continue
        buffer = io.StringIO()
        try:
            with redirect_stdout(buffer):
                out = dispatch_command(
                    name,
                    "",
                    registry=registry,
                    ctx_mgr=ctx,
                    session_state={
                        "_repl": None,
                        "agent_context": None,
                    },
                )
        except ExitSignal:
            rows.append((name, "(ExitSignal)", True))
            continue
        except Exception as exc:  # noqa: BLE001 — 这里正是要看会不会炸
            rows.append((name, f"异常: {type(exc).__name__}: {exc}"[:90], False))
            problems.append((name, "raised"))
            continue

        text = "" if out is None else str(out)
        printed = buffer.getvalue()
        combined = (text + printed).strip()
        if not combined:
            rows.append((name, "(无任何输出)", False))
            problems.append((name, "silent"))
        else:
            first = combined.splitlines()[0][:70]
            rows.append((name, first, True))

    print(f"{'命令':<20} 结果")
    print("-" * 90)
    for name, detail, ok in rows:
        mark = " " if ok else "✗"
        print(f"{mark} {name:<20} {detail}")

    print()
    print(f"总计 {len(rows)} 个命令；异常 {sum(1 for p in problems if p[1] == 'raised')}；"
          f"无输出 {sum(1 for p in problems if p[1] == 'silent')}")
    for name, kind in problems:
        print(f"  ✗ {name}  {kind}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
