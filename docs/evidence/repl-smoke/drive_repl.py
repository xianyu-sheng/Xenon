"""Drive a real REPL with a scripted input source.

Everything except the keystrokes is production: the real ``ModelRegistry``
loaded from ``~/.xenon``, the real ``REPL`` object, the real kernel, real
tools, a real ledger.  The built-in editor reads the terminal directly, so a
piped stdin never reaches it; calling ``_handle_command`` / ``_handle_chat``
is the same path its main loop calls.

Usage:  python docs/evidence/repl-smoke/drive_repl.py
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def main() -> int:
    ws = Path(tempfile.mkdtemp(prefix="xenon-repl-smoke-"))
    (ws / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (ws / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    (ws / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    os.chdir(ws)

    from xenon.repl.model_registry import ModelRegistry
    from xenon.repl.repl import REPL

    registry = ModelRegistry()
    default = Path.home() / ".xenon" / "models.yaml"
    if default.exists():
        registry.load_from_file(default)
    registry.load_from_credentials()

    repl = REPL(registry=registry, optimize_prompts=False, streaming=False)
    # start_repl does this right after construction; without it the pool stays
    # empty and /sub-agent reports "模型池为空" even though models exist.
    repl.model_pool.from_config(registry.export_config().get("models", {}))
    # Pin the model this environment can actually reach (xenon -m equivalent):
    # the pool also carries entries whose endpoint rejects the request, and the
    # router is free to pick one of those.
    preferred = os.environ.get("XENON_SMOKE_MODEL", "")
    if preferred:
        repl._preferred_model_ids = [preferred]
    print("== workspace:", ws, flush=True)
    print(
        "== models:",
        [e.model_id for e in (repl.model_pool.get_healthy() or repl.model_pool.list_all())],
        flush=True,
    )

    def snapshot(label: str) -> None:
        print(f"\n===== {label} =====", flush=True)
        print("== calc.py:", (ws / "calc.py").read_text(encoding="utf-8").strip(), flush=True)
        ledger = getattr(repl, "_last_kernel_ledger", None)
        print("== ledger:", ledger, flush=True)
        print("== task:", getattr(repl, "_last_kernel_task_id", ""), flush=True)
        if ledger and Path(ledger).exists():
            kinds = []
            for line in Path(ledger).read_text(encoding="utf-8").splitlines():
                try:
                    kinds.append(json.loads(line).get("kind", "?"))
                except Exception:
                    pass
            print("== event kinds:", kinds, flush=True)

    try:
        only = os.environ.get("XENON_SMOKE_ONLY", "")
        if only == "history":
            # Prove the pointer is really in the prompt the model receives:
            # the REPL also injects its own session memory, which can answer
            # the question without the model ever pulling the ledger.
            import xenon.harness.llm as llm_mod

            _original = llm_mod.current_turn
            _pointers: list[str] = []

            def _spy(view, *, instruction, step=None):
                text = _original(view, instruction=instruction, step=step)
                for line in text.splitlines():
                    if "Earlier tasks this session" in line:
                        _pointers.append(line)
                return text

            llm_mod.current_turn = _spy
            repl._handle_command("/permissions bypass")
            print(">> task 1", flush=True)
            repl._handle_chat("读一下 notes.txt，记住里面的第一个词，先不做别的。")
            snapshot("task 1")
            print(">> task 2 (only session history knows the answer)", flush=True)
            repl._handle_chat(
                "上一轮我让你读的是哪个文件？答案只在会话历史里，请不要读取工作区文件，"
                "先用会话历史确认再回答。"
            )
            snapshot("task 2")
            print("== pointer seen in prompt:", bool(_pointers), flush=True)
            if _pointers:
                print("== pointer:", _pointers[0], flush=True)
            llm_mod.current_turn = _original
        if only == "seal":
            repl._handle_command("/permissions bypass")
            print(">> kernel task (creates the parent ledger)", flush=True)
            repl._handle_chat("读一下 notes.txt 的第一行并告诉我。")
            print(">> /sub-agent", flush=True)
            repl._handle_command("/sub-agent 读取 notes.txt 的第一行并告诉我")
            parent = getattr(repl, "_last_kernel_ledger", None)
            print("== parent ledger:", parent, flush=True)
            if parent:
                for line in Path(parent).read_text(encoding="utf-8").splitlines():
                    event = json.loads(line)
                    if event.get("kind") in (
                        "child_started",
                        "child_finished",
                        "child_interrupted",
                    ):
                        print(
                            "==",
                            event["kind"],
                            json.dumps(event.get("payload", {}).get("branch"), ensure_ascii=False),
                            flush=True,
                        )
        if only not in ("sub", "history", "seal"):
            print("\n===== /permissions plan =====", flush=True)
            repl._handle_command("/permissions plan")
            print(">> task under plan mode", flush=True)
            repl._handle_chat("把 calc.py 里的 add 改成加法实现，写好后告诉我。")
            snapshot("after plan-mode task (write must be vetoed)")

            print("\n===== /permissions bypass =====", flush=True)
            repl._handle_command("/permissions bypass")
            print(">> task under bypass", flush=True)
            repl._handle_chat(
                "修复 calc.py 里的 bug：add 现在做的是减法。改好后运行 pytest test_calc.py 确认通过。"
            )
            snapshot("after bypass task (fix + test)")

        if only not in ("history", "seal"):
            print("\n===== /sub-agent =====", flush=True)
            repl._handle_command("/sub-agent 读取 notes.txt 的第一行并告诉我")
            snapshot("after sub-agent delegation")
    finally:
        try:
            repl._terminal_activity.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
