"""真实端到端验收：同一个 TS 前端会话内跑两轮，验证自进化闭环。

这是 docs/design/self-evolution.md §6 的最后一项验收，必须用真实 ConPTY +
真实模型，单测无法替代：

  第 1 轮：让模型执行一条 shell 命令（auto-edit 下 shell 必须审批），
           在审批面板上选择 [4] 拒绝。
           → 内核写 lesson_proposed（user_approval 路径）
           → runner 收尾存储 → lesson_stored + 磁盘记忆记录
  第 2 轮：让模型读 README（只读任务）。
           → match_for_task() 携带上一轮的教训
           → lesson_applied 出现在本轮账本

验收全部基于"证据"：读取两轮真实账本和磁盘记忆文件断言，不看代码。

用法：
    PYTHONIOENCODING=utf-8 python docs/evidence/self-evolution/tty_lesson_double_run.py [model]
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, r"D:\Xenon")

import winpty  # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else "deepseek/deepseek-v4-flash"
REPO = Path(r"C:\Users\Administrator\AppData\Local\Temp\xenon-lesson-run")

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\]8;[^ \x07]*?(?:\x07|\x1b\\)|\r")

TURN1 = (
    '请调用 command 工具执行这条命令：python -c "print(123)"。'
    "只执行这一条命令，不要修改任何文件，执行后调用 finish。"
)
TURN2 = "读取 README.md 的内容并原样告诉我，然后调用 finish，不要执行其他操作。"

HERE = Path(__file__).parent


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def wait_for(pty: winpty.PTY, needle: str, seconds: float) -> tuple[bool, str]:
    """Read the ConPTY until ``needle`` appears (plain-text match) or timeout."""

    return wait_for_count(pty, needle, 1, seconds)


def wait_for_count(
    pty: winpty.PTY,
    needle: str,
    count: int,
    seconds: float,
    *,
    initial: str = "",
) -> tuple[bool, str]:
    """Read until ``needle`` occurs at least ``count`` times, or timeout.

    Counting is over ``initial`` (frames drained before the wait started) plus
    everything newly read.  It is required across turns: after turn 1 Ink
    keeps repainting and the turn-1 footer ("证据 …") sits in stale frames;
    waiting for one occurrence matched that stale text instantly and the
    probe Ctrl-C'd turn 2 at intake.  Waiting for baseline + 1 is race-free.
    """

    end = time.time() + seconds
    seen = initial
    while time.time() < end:
        try:
            chunk = pty.read(blocking=False)
        except (EOFError, Exception):  # noqa: BLE001
            chunk = ""
        if chunk:
            seen += chunk
            if strip_ansi(seen).count(needle) >= count:
                return True, seen
        time.sleep(0.25)
    return False, seen


def wait_for_new_ledger(
    pty: winpty.PTY, known: set[str], seconds: float, *, initial: str = ""
) -> tuple[str | None, str]:
    """Wait until a ledger path NOT in ``known`` appears on screen.

    This is the only race-free turn-2 signal: counting "证据" occurrences
    cannot tell a stale repaint of the turn-1 footer (identical text) from
    turn 2's footer, but turn 2's footer carries a brand-new task id that has
    never been rendered before.  Returns the new path and all bytes seen.
    """

    end = time.time() + seconds
    seen = initial
    while time.time() < end:
        try:
            chunk = pty.read(blocking=False)
        except (EOFError, Exception):  # noqa: BLE001
            chunk = ""
        if chunk:
            seen += chunk
            for path in ledger_paths(seen):
                if path not in known:
                    return path, seen
        time.sleep(0.25)
    return None, seen


def read_all(pty: winpty.PTY, *, settle: float = 0.6) -> str:
    """Drain every frame currently buffered in the ConPTY."""

    end = time.time() + settle
    chunks = ""
    while time.time() < end:
        try:
            chunk = pty.read(blocking=False)
        except (EOFError, Exception):  # noqa: BLE001
            chunk = ""
        if chunk:
            chunks += chunk
            end = time.time() + settle  # keep draining until output goes quiet
        time.sleep(0.05)
    return chunks


def type_line(pty: winpty.PTY, text: str) -> None:
    """Type like a person: one character at a time, then Enter.

    A single paste can be swallowed or reordered under ConPTY, and typing
    before the kernel finished connecting loses Enter (a real failure of the
    first version of this probe).
    """

    for char in text:
        pty.write(char)
        time.sleep(0.008)
    time.sleep(0.3)
    pty.write("\r")
    time.sleep(0.5)


def main() -> int:
    if REPO.exists():
        shutil.rmtree(REPO)
    REPO.mkdir(parents=True)
    (REPO / "README.md").write_text("# lesson double run\nhello evidence\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=str(REPO), check=True)

    transcript: list[str] = []

    pty = winpty.PTY(120, 44)
    pty.spawn("powershell.exe", cwd=str(REPO))
    pty.set_size(120, 44)

    # Launch the real TS front-end from the freshly built dist.
    launch = "node D:\\Xenon\\frontend\\dist\\cli.js --level auto-edit --model " + MODEL
    pty.write(launch + "\r")

    # Real readiness is "model id · direct" — the first ❯ appears earlier,
    # while the kernel is still connecting, and input typed then is dropped.
    ok, seen = wait_for(pty, "direct", 60)
    transcript.append(seen)
    if not ok:
        print("FAIL: TS front-end did not finish connecting to the kernel")
        (HERE / "transcript.raw.txt").write_text("".join(transcript), encoding="utf-8")
        return 1
    print("front-end ready")
    time.sleep(1.0)

    # ── Turn 1: shell command, then deny every approval it raises ────
    # Denying once is not enough: the model retries the command as a `test`
    # (verify) call, which raises a *second* panel with a new id.  A single
    # "4" left that panel waiting and the probe concluded the UI was stuck.
    type_line(pty, TURN1)
    seen = ""
    denials = 0
    deadline = time.time() + 300
    while time.time() < deadline:
        seen += read_all(pty, settle=0.8)
        text = strip_ansi(seen)
        if text.count("需要授权") > denials and denials < 8:
            denials += 1
            # The match can land while the panel frame is still being painted;
            # sending the keystroke then interleaves it with the write and the
            # "4" is lost.  Let output go quiet first, then answer.
            time.sleep(1.2)
            print(f"denying approval #{denials}")
            pty.write("4")
            continue
        if "证据" in text:
            break
    transcript.append(seen)
    if "证据" not in strip_ansi(seen):
        print(f"FAIL: turn 1 evidence footer never appeared ({denials} denials)")
        (HERE / "transcript.raw.txt").write_text("".join(transcript), encoding="utf-8")
        return 1
    print(f"turn 1 finished after {denials} denial(s)")
    time.sleep(1.0)

    # ── Turn 2: read-only carry of the stored lesson ────────────────
    # Drain stale frames, then baseline the LEDGER PATHS.  Footer text is
    # unusable as a signal here: stale repaints of the turn-1 footer are
    # byte-identical and fool both plain waits and occurrence counts; only
    # turn 2's brand-new task id distinguishes its footer.
    stale = read_all(pty, settle=0.8)
    transcript.append(stale)
    known_paths = set(ledger_paths("".join(transcript)))

    type_line(pty, TURN2)
    new_path, seen = wait_for_new_ledger(
        pty, known_paths, 180, initial=stale
    )
    # seen includes the stale drain already recorded; append only the new bytes.
    transcript.append(seen[len(stale):])
    if new_path is None:
        print("FAIL: turn 2 ledger footer never appeared")
        (HERE / "transcript.raw.txt").write_text("".join(transcript), encoding="utf-8")
        return 1
    print(f"turn 2 finished — ledger: {new_path}")
    time.sleep(1.0)

    pty.write("\x03")  # Ctrl-C: host shutdown
    time.sleep(1.5)
    try:
        transcript.append(pty.read(blocking=False) or "")
    except (EOFError, Exception):  # noqa: BLE001
        pass

    raw = "".join(transcript)
    (HERE / "transcript.raw.txt").write_text(raw, encoding="utf-8")
    (HERE / "transcript.txt").write_text(strip_ansi(raw), encoding="utf-8")

    verify(raw)
    return _fail_count


def ledger_paths(raw: str) -> list[str]:
    """Ledger paths the footer rendered (OSC-8 stripped by read of the screen)."""

    plain = strip_ansi(raw)
    return re.findall(r"[A-Za-z]:\\[^\s]*?repl[^\s]*?\.jsonl", plain)


def verify(raw: str) -> None:
    from xenon.ledger.events import EventKind
    from xenon.ledger.store import Ledger
    from xenon.memory.models import MemoryKind

    # Chronological order (mtime), never lexical: the checks assume e1 is the
    # turn that proposed/stored and e2 the turn that applied.
    paths = [
        str(p)
        for p in sorted(
            {Path(value) for value in ledger_paths(raw)},
            key=lambda p: p.stat().st_mtime,
        )
    ]
    print(f"\nledger paths on screen: {paths}")
    if len(paths) < 2:
        check("found both turn ledger paths on screen", False)
        # Fall back: newest two files in the repl ledger dir.
        ledger_dir = Path.home() / ".xenon" / "ledger" / "repl"
        paths = [
            str(p)
            for p in sorted(
                ledger_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime
            )[-2:]
        ]
        print(f"fallback ledgers: {paths}")

    def events_of(path: str):
        task_id = Path(path).stem
        return Ledger(Path(path), task_id).read()

    e1 = events_of(paths[0])
    e2 = events_of(paths[1])

    kinds1 = [e.kind for e in e1]
    kinds2 = [e.kind for e in e2]

    print("\n── evidence ──")
    check("turn 1 ledger: lesson_proposed", EventKind.LESSON_PROPOSED in kinds1)
    check("turn 1 ledger: lesson_stored", EventKind.LESSON_STORED in kinds1)
    check("turn 2 ledger: lesson_applied", EventKind.LESSON_APPLIED in kinds2)

    proposed_ids = {
        f"lsn-{e.payload['fingerprint'][:16]}"
        for e in e1
        if e.kind is EventKind.LESSON_PROPOSED
    }
    stored_ids = {
        e.payload["lesson_id"]
        for e in e1
        if e.kind is EventKind.LESSON_STORED
    }
    applied_ids = {
        e.payload["lesson_id"]
        for e in e2
        if e.kind is EventKind.LESSON_APPLIED
    }
    chain_ok = bool(proposed_ids) and proposed_ids == stored_ids == applied_ids
    check(
        "every proposed lesson was stored and later applied (same ids)",
        chain_ok,
    )
    if not chain_ok:
        print(f"        proposed={sorted(proposed_ids)}")
        print(f"        stored={sorted(stored_ids)}")
        print(f"        applied={sorted(applied_ids)}")

    # The memory record must physically exist in the project-local backend.
    backend_dir = REPO / ".xenon"
    memory_files = list(backend_dir.rglob("*.json")) if backend_dir.exists() else []
    found_record = None
    for file in memory_files:
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        records = (
            data
            if isinstance(data, list)
            else data.get("items", data.get("records", []))
        )
        found_record = next(
            (r for r in records if r.get("kind") == MemoryKind.LESSON.value),
            None,
        )
        if found_record:
            break
    check("project-local memory holds the LESSON record on disk", found_record is not None)
    if found_record:
        print(f"        record: {found_record.get('id')} tags={found_record.get('tags')}")

    print(
        "\n"
        + (
            "ALL EVIDENCE CHECKS PASSED"
            if _fail_count == 0
            else f"{_fail_count} CHECK(S) FAILED"
        )
    )


def check(name: str, passed: bool) -> None:
    print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    if not passed:
        global _fail_count
        _fail_count += 1


_fail_count = 0


if __name__ == "__main__":
    raise SystemExit(main())
