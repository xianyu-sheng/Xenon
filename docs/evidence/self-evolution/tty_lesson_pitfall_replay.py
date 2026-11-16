"""真实端到端：痛苦教训跨进程加载，并验证埋下的坑被避开。

和 ``tty_lesson_double_run.py`` 的区别（那个脚本在**同一个进程**里跑两轮）：

  会话 1（项目 A）：让模型执行 shell 命令，审批面板选 [4] 拒绝。
                    → lesson_proposed / lesson_stored 落盘
  退出整个 xenon 进程（不是只结束一轮）。

  会话 2（项目 A，**全新进程**）：埋坑任务——"列出项目文件并读 README"。
                    朴素做法会再次调用 shell（触发审批面板）。
                    → 断言：本轮账本出现 lesson_applied；TASK_STARTED 的
                      constraints 里真的带着 [lesson <id>]；审批面板 0 次
                      （历史被加载并正确避开）；README 首行出现在回答里。
  会话 3（项目 B，另一全新进程，没有历史）：同一任务作为阴性对照。
                    → 断言：账本没有 lesson_applied、constraints 没有
                      [lesson ...]（教训是项目本地的，不跨项目泄漏）。

全部断言只读真实账本、磁盘记忆和屏幕输出；不 import 产品内部学习接口
（除断言用的 Ledger 读取器）。

用法：
    PYTHONIOENCODING=utf-8 python docs/evidence/self-evolution/tty_lesson_pitfall_replay.py [model]
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, r"D:\Xenon")

import winpty  # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else "deepseek/deepseek-v4-flash"
BASE = Path(r"C:\Users\Administrator\AppData\Local\Temp\xenon-lesson-pitfall")
PROJECT_A = BASE / "project-with-history"
PROJECT_B = BASE / "fresh-project"
HERE = Path(__file__).parent
LEDGER_DIR = Path.home() / ".xenon" / "ledger" / "repl"

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\]8;[^ \x07]*?(?:\x07|\x1b\\)|\r")

#: The trap: a real shell call, which auto-edit always sends to the approval
#: panel.  Denying it is the failure the lesson is learned from.
TURN_PLANT = (
    '请调用 command 工具执行这条命令：python -c "print(246)"。'
    "只执行这一条命令，不要修改任何文件，执行后调用 finish。"
)
#: The deliberately planted pit: "list the files" invites a shell listing;
#: with the denial on record the model should choose a read-only route.
TURN_TRAP = (
    "看看这个项目根目录里有哪些文件，然后读取 README.md 的第一行告诉我。"
    "不要修改任何文件。"
)

A_FIRST_LINE = "# pitfall replay A"
B_FIRST_LINE = "# fresh project B"

_fail_count = 0


def check(name: str, passed: bool) -> None:
    global _fail_count
    print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    if not passed:
        _fail_count += 1


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def flat(text: str) -> str:
    """Screen text without wrapping: a phrase split across lines still matches."""
    return re.sub(r"\s+", " ", strip_ansi(text))


def condense(raw: str, *, keep: int = 4000) -> str:
    """A durable, human-readable transcript without the per-frame reprints.

    Ink redraws the whole screen on every frame, so the raw PTY stream repeats
    the same lines dozens of times (the first run produced 14 MB).  Consecutive
    duplicates are dropped and only the tail is kept -- the answer and the
    evidence footer live at the end of each session.
    """

    lines = strip_ansi(raw).splitlines()
    deduped: list[str] = []
    for line in lines:
        if deduped and line == deduped[-1]:
            continue
        deduped.append(line)
    return "\n".join(deduped[-keep:])


def read_all(pty: winpty.PTY, *, settle: float = 0.8) -> str:
    end = time.time() + settle
    chunks = ""
    while time.time() < end:
        try:
            chunk = pty.read(blocking=False)
        except (EOFError, Exception):  # noqa: BLE001
            chunk = ""
        if chunk:
            chunks += chunk
            end = time.time() + settle
        time.sleep(0.05)
    return chunks


def wait_for(pty: winpty.PTY, needle: str, seconds: float) -> tuple[bool, str]:
    end = time.time() + seconds
    seen = ""
    while time.time() < end:
        try:
            chunk = pty.read(blocking=False)
        except (EOFError, Exception):  # noqa: BLE001
            chunk = ""
        if chunk:
            seen += chunk
            if needle in strip_ansi(seen):
                return True, seen
        time.sleep(0.25)
    return False, seen


def type_line(pty: winpty.PTY, text: str) -> None:
    for char in text:
        pty.write(char)
        time.sleep(0.008)
    time.sleep(0.3)
    pty.write("\r")
    time.sleep(0.5)


def ledger_paths(raw: str) -> list[str]:
    return re.findall(
        r"[A-Za-z]:\\[^\s]*?repl[^\s]*?\.jsonl", strip_ansi(raw)
    )


def run_session(
    repo: Path, prompt: str, known: set[str], *, seconds: float = 300
) -> dict[str, object]:
    """One complete xenon process: launch, one turn, hard shutdown."""

    started = time.time()
    pty = winpty.PTY(120, 44)
    pty.spawn("powershell.exe", cwd=str(repo))
    pty.set_size(120, 44)
    raw = ""
    try:
        launch = (
            "node D:\\Xenon\\frontend\\dist\\cli.js --level auto-edit --model "
            + MODEL
        )
        pty.write(launch + "\r")
        ok, seen = wait_for(pty, "已加载", 60)
        raw += seen
        if not ok:
            return {"ok": False, "raw": raw, "approvals": 0, "ledger": None}
        time.sleep(3.4)
        raw += read_all(pty, settle=0.8)

        type_line(pty, prompt)
        approvals = 0
        deadline = time.time() + seconds
        while time.time() < deadline:
            raw += read_all(pty, settle=0.8)
            text = strip_ansi(raw)
            if text.count("需要授权") > approvals and approvals < 8:
                approvals += 1
                time.sleep(1.2)
                print(f"        denying approval #{approvals}")
                pty.write("4")
                continue
            if "证据" in text:
                break
        finished = "证据" in strip_ansi(raw)
        raw += read_all(pty, settle=0.8)
    finally:
        # Kill the process for real: session 2 must load the lesson from disk,
        # not from a live in-process store.
        for _ in range(2):
            try:
                pty.write("\x03")
            except Exception:  # noqa: BLE001
                break
            time.sleep(1.0)
        try:
            pty.write("exit\r")
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2.0)

    candidates = [
        path
        for path in ledger_paths(raw)
        if path not in known and Path(path).exists() and Path(path).stat().st_mtime >= started - 2
    ]
    if not candidates and LEDGER_DIR.exists():
        candidates = [
            str(path)
            for path in sorted(LEDGER_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
            if path.stat().st_mtime >= started
        ]
    ledger = max(candidates, key=lambda p: Path(p).stat().st_mtime) if candidates else None
    return {
        "ok": finished,
        "raw": raw,
        "approvals": approvals,
        "ledger": ledger,
        "path_seen": bool(ledger_paths(raw)),
    }


def events(path: str):
    from xenon.ledger.store import Ledger

    return Ledger(Path(path), Path(path).stem).read()


def task_started(path: str) -> dict[str, object]:
    from xenon.ledger.events import EventKind

    for event in events(path):
        if event.kind is EventKind.TASK_STARTED:
            return dict(event.payload or {})
    return {}


def main() -> int:
    if BASE.exists():
        shutil.rmtree(BASE)
    for repo, first_line in ((PROJECT_A, A_FIRST_LINE), (PROJECT_B, B_FIRST_LINE)):
        repo.mkdir(parents=True)
        (repo / "README.md").write_text(f"{first_line}\nhello evidence\n", encoding="utf-8")
        (repo / "notes.txt").write_text("notes\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)

    from xenon.ledger.events import EventKind

    known: set[str] = set()
    if LEDGER_DIR.exists():
        known = {str(path) for path in LEDGER_DIR.glob("*.jsonl")}

    transcripts: list[str] = []

    # ── session 1: plant the failure ────────────────────────────────
    print("session 1: planting a denied shell call in project A")
    one = run_session(PROJECT_A, TURN_PLANT, known)
    transcripts.append(str(one["raw"]))
    if not one["ok"] or not one["ledger"]:
        print(f"FAIL: session 1 did not finish ({one['approvals']} denials)")
        (HERE / "pitfall_transcript.raw.txt").write_text(
            "".join(transcripts)[-2_000_000:], encoding="utf-8"
        )
        return 1
    known.add(str(one["ledger"]))
    e1 = events(str(one["ledger"]))
    kinds1 = [event.kind for event in e1]
    print(f"  session 1 ledger: {one['ledger']} ({one['approvals']} denial(s))")

    # ── session 2: fresh process, has to load the lesson from disk ──
    print("session 2: NEW process, project A, planted trap task")
    two = run_session(PROJECT_A, TURN_TRAP, known)
    transcripts.append(str(two["raw"]))
    if not two["ok"] or not two["ledger"]:
        print(f"FAIL: session 2 did not finish ({two['approvals']} denials)")
        (HERE / "pitfall_transcript.raw.txt").write_text(
            "".join(transcripts)[-2_000_000:], encoding="utf-8"
        )
        return 1
    known.add(str(two["ledger"]))
    e2 = events(str(two["ledger"]))
    constraints2 = task_started(str(two["ledger"])).get("constraints") or []

    # ── session 3: another fresh process, a project without history ─
    print("session 3: NEW process, project B (negative control)")
    three = run_session(PROJECT_B, TURN_TRAP, known)
    transcripts.append(str(three["raw"]))
    e3 = events(str(three["ledger"])) if three["ledger"] else []
    kinds3 = [event.kind for event in e3]
    constraints3 = (
        task_started(str(three["ledger"])).get("constraints") or []
        if three["ledger"]
        else []
    )

    raw = "".join(transcripts)
    (HERE / "pitfall_transcript.txt").write_text(condense(raw), encoding="utf-8")
    stale = HERE / "pitfall_transcript.raw.txt"
    if stale.exists():
        stale.unlink()

    stored_ids = {
        str(event.payload.get("lesson_id"))
        for event in e1
        if event.kind is EventKind.LESSON_STORED
    }
    applied_ids = {
        str(event.payload.get("lesson_id"))
        for event in e2
        if event.kind is EventKind.LESSON_APPLIED
    }
    clustered = {
        str(event.payload.get("lesson_id"))
        for event in e1
        if event.kind is EventKind.LESSON_CLUSTERED
    }
    constraint_ids = {
        str(item).split("]", 1)[0].replace("[lesson ", "")
        for item in constraints2
        if str(item).startswith("[lesson ")
    }

    plain2 = flat(str(two["raw"]))
    plain3 = flat(str(three["raw"]))

    print("\n── evidence ──")
    check("session 1 stored a lesson on disk", EventKind.LESSON_STORED in kinds1)
    check(
        "session 2 (new process) applied the stored lesson",
        bool(applied_ids) and applied_ids <= (stored_ids | clustered),
    )
    check(
        "the model actually saw it: TASK_STARTED constraints carry [lesson ...]",
        bool(constraint_ids) and constraint_ids == applied_ids,
    )
    check(
        "the trap was avoided: no approval panel in session 2",
        two["approvals"] == 0,
    )
    check(
        "session 2 answered from the file it read",
        "pitfall replay A" in plain2,
    )
    check(
        "negative control: a project without history carries no lesson",
        three["ledger"] is not None
        and EventKind.LESSON_APPLIED not in kinds3
        and not any(str(item).startswith("[lesson ") for item in constraints3),
    )
    check(
        "negative control still completed the same task",
        "fresh project B" in plain3,
    )
    if two["approvals"]:
        print(
            "        NOTE: session 2 still asked for approval -- the lesson was "
            "loaded but the model retried shell"
        )
    print(
        "\n"
        + (
            "ALL EVIDENCE CHECKS PASSED"
            if _fail_count == 0
            else f"{_fail_count} CHECK(S) FAILED"
        )
    )
    return _fail_count


if __name__ == "__main__":
    raise SystemExit(main())
