"""Real-task component matrix for Xenon, driven through the shipped front-end.

Every scenario launches the real ``node frontend/dist/cli.js`` under ConPTY
(not the kernel API), talks to it by typing like a user, and judges the result
from the ledger and the workspace — never from product internals.  The model
is the real ``deepseek/deepseek-v4-flash``.

What it answers: "can each component carry a real task end to end, and what
does it cost in wall time / tokens?"

    read        read-only task: read tool, evidence footer, no approval
    write       create + run + verify: approval, shell receipt with output
    bugfix      fix code, run pytest, leave the test file untouched
    commands    /cost /cache /status /mode /tools /permissions /memory
    plan        /plan on -> planned turn -> /plan approve -> resume
    session     /save -> new process --resume -> history answers
    mcp         /mcp add local stdio server -> tool call -> restart persistence
    cancel      approve a slow shell call, Ctrl+C, REPL still answers
    dashboard   --dashboard URL/token, 401 without token, /api/state + query

Usage:
    PYTHONIOENCODING=utf-8 python docs/evidence/component-matrix/run_matrix.py
    ... --only read,write --model deepseek/deepseek-v4-flash
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, r"D:\Xenon")

import winpty  # noqa: E402

ROOT = Path(r"D:\Xenon")
HERE = Path(__file__).parent
BASE = Path(tempfile.gettempdir()) / "xenon-component-matrix"
RUN_TAG = f"{time.strftime('%H%M%S')}-{os.getpid()}"
MODEL = "deepseek/deepseek-v4-flash"
CLI = ROOT / "frontend" / "dist" / "cli.js"
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\]8;[^ \x07]*?(?:\x07|\x1b\\)|\r")

FINDINGS: list[dict[str, Any]] = []
METRICS: list[dict[str, Any]] = []


# ── text helpers ─────────────────────────────────────────────────


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def flat(text: str) -> str:
    return re.sub(r"\s+", " ", strip_ansi(text))


def condense(raw: str, *, keep: int = 1200) -> str:
    lines = strip_ansi(raw).splitlines()
    out: list[str] = []
    for line in lines:
        if out and line == out[-1]:
            continue
        out.append(line)
    return "\n".join(out[-keep:])


def one_line(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


def check(name: str, passed: bool, detail: str = "") -> None:
    FINDINGS.append({"name": name, "passed": bool(passed), "detail": detail})
    print(f"  {'PASS' if passed else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


# ── workspace / config ───────────────────────────────────────────


def fresh_repo(name: str) -> Path:
    # A unique directory per process: an earlier run's node process may still
    # hold its old cwd on Windows, and rmtree of a live cwd fails silently.
    repo = BASE / f"{name}-{RUN_TAG}"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "matrix@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Matrix"], cwd=repo, check=True)
    (repo / "README.md").write_text("# matrix project\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, check=True)
    return repo


def isolated_home(tag: str) -> Path:
    """A throw-away ``~/.xenon`` seeded with the real model config."""

    home = BASE / f"home-{tag}-{RUN_TAG}"
    (home / ".xenon").mkdir(parents=True)
    for name in ("models.yaml", "credentials.yaml", "settings.yaml", "shortcuts.yaml"):
        source = Path.home() / ".xenon" / name
        if source.exists():
            shutil.copy2(source, home / ".xenon" / name)
    return home


# ── the ConPTY front-end ─────────────────────────────────────────


class XenonUI:
    """One real xenon process; one thread, because winpty dislikes races."""

    def __init__(
        self,
        repo: Path,
        *,
        home: Path | None = None,
        extra_args: list[str] | None = None,
        model: str = MODEL,
    ) -> None:
        self.repo = repo
        self.home = home
        self.raw = ""
        self.approvals = 0
        self.pty = winpty.PTY(140, 46)
        self.pty.spawn("powershell.exe", cwd=str(repo))
        self.pty.set_size(140, 46)
        prefix = ""
        if home is not None:
            # Path.home() on Windows follows USERPROFILE (HOME is the fallback);
            # isolate config writes so /mcp add never touches the real profile.
            home_s = str(home).replace("'", "''")
            prefix = f"$env:USERPROFILE='{home_s}'; $env:HOME='{home_s}'; "
        args = " ".join(extra_args or [])
        launch = (
            f"node {CLI} --level auto-edit --model {model} {args}".strip()
        )
        self.pty.write(prefix + launch + "\r")
        ok = self.wait_text("已加载", 60)
        if not ok:
            raise RuntimeError("front-end did not reach the ready card")
        time.sleep(3.4)  # let the brand animation settle
        self.pump(0.8)

    # reading ----------------------------------------------------

    def pump(self, seconds: float = 0.6) -> None:
        # A fixed window, not a deadline that each chunk pushes back.  The
        # running spinner repaints continuously, so an extending deadline
        # never returned and the approval panel went unanswered for 300 s.
        end = time.time() + seconds
        while time.time() < end:
            try:
                chunk = self.pty.read(blocking=False)
            except Exception:  # noqa: BLE001
                chunk = ""
            if chunk:
                self.raw += chunk
            time.sleep(0.03)

    def text(self) -> str:
        return flat(self.raw)

    def wait_text(self, needle: str, seconds: float) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            self.pump(0.4)
            if needle in self.text():
                return True
        return False

    def wait_any(self, needles: tuple[str, ...], seconds: float) -> str | None:
        end = time.time() + seconds
        while time.time() < end:
            self.pump(0.4)
            text = self.text()
            for needle in needles:
                if needle in text:
                    return needle
        return None

    # writing ----------------------------------------------------

    def type_line(self, text: str) -> None:
        for char in text:
            self.pty.write(char)
            time.sleep(0.006)
        time.sleep(0.25)
        self.pty.write("\r")
        time.sleep(0.4)

    def ctrl_c(self) -> None:
        self.pty.write("\x03")

    # turns ------------------------------------------------------

    def _tail(self) -> str:
        return self.text()[-1600:]

    def _drive(
        self,
        done: Any,
        *,
        approve: str = "1",
        timeout: float = 300.0,
        ledger_done: bool = True,
    ) -> tuple[bool, int]:
        """Wait until *done(text)*, answering approval panels on the way.

        The panel is drawn by Ink, but the stream is append-only: the running
        spinner repaints push the panel out of the raw tail, and the panel
        itself is repainted into the stream more than once.  So visibility is
        judged on the whole stream, while the ledger's answered-approval count
        says whether the last key actually landed.  A panel stays "armed"
        until a resolution appears; a later, new panel re-arms it.
        """

        start = time.time()
        last_key = 0.0
        answered = 0
        sent_at_resolutions = -1
        resolutions_at_resolution = -1
        panel_total_at_resolution = 0
        ledger: Path | None = None
        while time.time() - start < timeout:
            self.pump(0.7)
            text = self.text()
            if done(text):
                self.approvals += answered
                return True, answered
            if ledger_done:
                if ledger is None:
                    ledger = newest_ledger(None, start)
                if ledger is not None and turn_finalized(ledger):
                    self.pump(1.2)
                    self.approvals += answered
                    return True, answered
            resolutions = approval_resolutions(ledger)
            panel_total = text.count("需要授权") + text.count("计划待审批")
            # Last key landed: freeze this panel until a *new* one is drawn.
            if (
                resolutions_at_resolution < 0
                and sent_at_resolutions >= 0
                and resolutions > sent_at_resolutions
            ):
                resolutions_at_resolution = resolutions
                panel_total_at_resolution = panel_total
            if (
                resolutions_at_resolution >= 0
                and panel_total > panel_total_at_resolution
            ):
                # A fresh panel rendered after the previous resolution.
                resolutions_at_resolution = -1
                sent_at_resolutions = -1
            armed = resolutions_at_resolution < 0
            retry_due = (
                sent_at_resolutions < 0
                or (
                    resolutions == sent_at_resolutions
                    and time.time() - last_key > 6.0
                )
            )
            if armed and panel_total > 0 and retry_due and answered < 8:
                self.pty.write(approve)
                answered += 1
                sent_at_resolutions = resolutions
                last_key = time.time()
        self.approvals += answered
        return False, answered

    def wait_turn(
        self, *, approve: str = "1", timeout: float = 300.0
    ) -> tuple[bool, int]:
        base = self.text().count("证据 ")
        return self._drive(
            lambda text: text.count("证据 ") > base,
            approve=approve,
            timeout=timeout,
        )

    def run_turn(self, prompt: str, *, approve: str = "1", timeout: float = 300.0):
        self.type_line(prompt)
        return self.wait_turn(approve=approve, timeout=timeout)

    def run_command_with_approval(
        self, command: str, expect: str, *, approve: str = "1", timeout: float = 300.0
    ) -> tuple[bool, int]:
        """Run a command that resumes work (``/plan approve``) and may ask."""

        base = self.text().count(expect)
        self.type_line(command)
        return self._drive(
            lambda text: text.count(expect) > base,
            approve=approve,
            timeout=timeout,
            ledger_done=False,
        )

    def run_command(self, command: str, expect: str, timeout: float = 30.0) -> bool:
        base = self.text().count(expect)
        self.type_line(command)
        end = time.time() + timeout
        while time.time() < end:
            self.pump(0.5)
            if self.text().count(expect) > base:
                return True
        return False

    def answer_prompt(self, command: str, question: str, expect: str) -> tuple[bool, bool]:
        """Run a command that opens a protocol prompt; Enter accepts the default."""

        self.type_line(command)
        asked = self.wait_text(question, 15)
        if asked:
            self.pty.write("\r")
        answered = self.wait_text(expect, 15) if asked else False
        return asked, answered

    def close(self) -> None:
        for _ in range(2):
            try:
                self.ctrl_c()
            except Exception:  # noqa: BLE001
                break
            time.sleep(0.8)
        try:
            self.pty.write("exit\r")
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.2)


# ── ledger ───────────────────────────────────────────────────────


def ledger_root(home: Path | None) -> Path:
    return (home or Path.home()) / ".xenon" / "ledger" / "repl"


def newest_ledger(home: Path | None, since: float) -> Path | None:
    root = ledger_root(home)
    if not root.is_dir():
        return None
    # Pure conversation turns write ``chat/chat-*.jsonl``; a non-recursive
    # glob silently lost them and the harness waited out its 300 s timeout.
    candidates = [
        path
        for path in root.rglob("*.jsonl")
        if path.is_file() and path.stat().st_mtime >= since - 2
    ]
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def turn_finalized(path: Path | None) -> bool:
    """The ledger's ``finalized`` event, which the UI footer may not show."""

    if path is None or not path.exists():
        return False
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    for line in lines:
        if '"kind": "finalized"' in line or '"kind":"finalized"' in line:
            return True
    return False


def approval_resolutions(path: Path | None) -> int:
    """How many approval panels this ledger says were answered.

    ``user_approval`` covers policy approvals and escalations; a plan accepted
    through the plan callback lands as ``plan_approved``.  Counting only
    resolutions (not requests) is deliberate: an escalation request has no
    preceding request event, while its answer always leaves one of these.
    """

    if path is None or not path.exists():
        return 0
    resolved = 0
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("kind") in {"user_approval", "plan_approved"}:
            resolved += 1
    return resolved


def ledger_facts(path: Path | None) -> dict[str, Any]:
    from xenon.ledger.store import Ledger

    facts: dict[str, Any] = {
        "path": str(path) if path else None,
        "events": 0,
        "kinds": {},
        "tools": [],
        "evidence": "",
        "accepted": None,
        "debt": [],
        "answer": "",
        "metrics": {},
        "tokens": 0,
        "cache_hits": 0,
        "model": "",
    }
    if path is None or not path.exists():
        return facts
    events = Ledger(path, path.stem).read()
    facts["events"] = len(events)
    for event in events:
        kind = event.kind.value
        facts["kinds"][kind] = facts["kinds"].get(kind, 0) + 1
        if kind == "tool_receipt":
            receipt = (event.payload or {}).get("receipt") or {}
            facts["tools"].append(
                {
                    "tool": receipt.get("tool"),
                    "status": receipt.get("status"),
                    "observation": str(receipt.get("observation") or "")[:400],
                }
            )
        if kind == "model_usage":
            payload = event.payload or {}
            facts["tokens"] = max(
                facts["tokens"], int(payload.get("cumulative_total") or 0)
            )
            facts["cache_hits"] += int(payload.get("cache_hit_tokens") or 0)
            facts["model"] = str(payload.get("model") or facts["model"])
        if kind == "delivered":
            payload = event.payload or {}
            facts["answer"] = str(payload.get("answer") or facts["answer"])
        if kind == "finalized":
            payload = event.payload or {}
            facts["evidence"] = str(
                payload.get("evidence_status") or facts["evidence"]
            )
    result_path = path.with_suffix(".result.json")
    if result_path.exists():
        result = json.loads(result_path.read_text(encoding="utf-8"))
        facts["evidence"] = str(result.get("evidence_status") or "")
        facts["accepted"] = result.get("accepted")
        facts["debt"] = list(result.get("debt") or [])
        facts["answer"] = str(result.get("answer") or "")
        facts["metrics"] = dict(result.get("metrics") or {})
    return facts


def record(name: str, ui: XenonUI, facts: dict[str, Any], seconds: float) -> None:
    METRICS.append(
        {
            "name": name,
            "wall": seconds,
            "tokens": facts["tokens"],
            "tools": [str(tool["tool"]) for tool in facts["tools"]],
            "evidence": facts["evidence"],
            "accepted": facts["accepted"],
            "ledger": Path(str(facts["path"])).name if facts["path"] else "-",
        }
    )
    print(
        f"  ledger={Path(str(facts['path'])).name if facts['path'] else None}"
        f" evidence={facts['evidence']} accepted={facts['accepted']}"
        f" tools={[tool['tool'] for tool in facts['tools']]}"
        f" tokens={facts['tokens']} wall={seconds:.1f}s"
    )
    (HERE / f"{name}.transcript.txt").write_text(
        condense(ui.raw), encoding="utf-8"
    )
    if facts["path"] and Path(str(facts["path"])).exists():
        source = Path(str(facts["path"]))
        shutil.copy2(source, HERE / f"{name}.ledger.jsonl")
        result = source.with_suffix(".result.json")
        if result.exists():
            shutil.copy2(result, HERE / f"{name}.result.json")


# ── scenarios ────────────────────────────────────────────────────


def scenario_read(model: str) -> None:
    repo = fresh_repo("read")
    token = "MATRIX-7F3A"
    (repo / "README.md").write_text(f"# {token}\nhello matrix\n", encoding="utf-8")
    started = time.time()
    ui = XenonUI(repo, model=model)
    try:
        ok, approvals = ui.run_turn(
            "读取 README.md 的第一行并原样告诉我，不要修改任何文件。",
            approve="1",
        )
    finally:
        ui.close()
    elapsed = time.time() - started
    facts = ledger_facts(newest_ledger(None, started))
    record("read", ui, facts, elapsed)
    check("read: turn finished", ok)
    check("read: no approval needed", approvals == 0, f"{approvals}")
    check("read: answer quotes the file", token in facts["answer"])
    check("read: read receipt exists", any(t["tool"] == "read" for t in facts["tools"]))
    check("read: evidence reported", facts["evidence"] in {"partial", "verified"})
    check("read: no unmet debt", not facts["debt"])


def scenario_write(model: str) -> None:
    repo = fresh_repo("write")
    started = time.time()
    ui = XenonUI(repo, model=model)
    try:
        ok, approvals = ui.run_turn(
            "创建 hello.py（内容只有一行 print(246)），然后用 bash 运行 "
            "python hello.py，确认输出是 246，最后告诉我结果。不要修改其他文件。",
            approve="1",
        )
    finally:
        ui.close()
    elapsed = time.time() - started
    facts = ledger_facts(newest_ledger(None, started))
    record("write", ui, facts, elapsed)
    hello = repo / "hello.py"
    shell_obs = " ".join(
        t["observation"]
        for t in facts["tools"]
        if t["tool"] in {"bash", "shell", "command", "verify", "test"}
    )
    check("write: turn finished", ok)
    check("write: an approval was exercised", approvals >= 1, f"{approvals}")
    check("write: hello.py written with print(246)", hello.exists() and "print(246)" in hello.read_text(encoding="utf-8"))
    check("write: shell receipt carries the output", "246" in shell_obs, shell_obs[:60])
    check("write: no unmet debt", not facts["debt"])


def scenario_bugfix(model: str) -> None:
    repo = fresh_repo("bugfix")
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "buggy calc"], cwd=repo, check=True)
    test_before = (repo / "test_calc.py").read_text(encoding="utf-8")
    started = time.time()
    ui = XenonUI(repo, model=model)
    try:
        ok, approvals = ui.run_turn(
            "calc.py 里的 add 是错的，导致 pytest 失败。修复 calc.py 让 "
            "python -m pytest -q 通过；不要修改 test_calc.py。",
            approve="1",
        )
    finally:
        ui.close()
    elapsed = time.time() - started
    facts = ledger_facts(newest_ledger(None, started))
    record("bugfix", ui, facts, elapsed)
    code = (repo / "calc.py").read_text(encoding="utf-8")
    pytest_obs = " ".join(
        t["observation"]
        for t in facts["tools"]
        if "pytest" in t["observation"] or t["tool"] in {"bash", "shell", "verify", "test"}
    )
    test_run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"], cwd=repo, capture_output=True, text=True
    )
    check("bugfix: turn finished", ok)
    check("bugfix: add returns the sum", "a + b" in code, code.strip())
    check("bugfix: test file untouched", (repo / "test_calc.py").read_text(encoding="utf-8") == test_before)
    check("bugfix: pytest passes", test_run.returncode == 0, test_run.stdout.strip()[-80:])
    check("bugfix: the model ran the tests", "passed" in pytest_obs, pytest_obs[:60])
    check("bugfix: no unmet debt", not facts["debt"])


def scenario_commands(model: str) -> None:
    repo = fresh_repo("commands")
    started = time.time()
    ui = XenonUI(repo, model=model)
    try:
        ui.run_turn("用一句话解释什么是证据账本，不要调用工具。")
        results = {
            "/cost": ui.run_command("/cost", "缓存"),
            "/cache": ui.run_command("/cache", "缓存"),
            "/context": ui.run_command("/context", "上下文"),
            "/mode": ui.run_command("/mode", "当前循环"),
            "/tools": ui.run_command("/tools", "工具"),
            "/memory lessons": ui.run_command("/memory lessons", "教训"),
        }
        asked, answered = ui.answer_prompt(
            "/permissions", "选择权限模式", "执行级别"
        )
        results["/permissions"] = asked and answered
        ui.type_line("/exit")
        time.sleep(3.0)
        ui.pump(2.0)
    finally:
        ui.close()
    elapsed = time.time() - started
    facts = ledger_facts(newest_ledger(None, started))
    record("commands", ui, facts, elapsed)
    for command, ok in results.items():
        check(f"commands: {command} answers", ok)
    check("commands: no unknown command", "未知命令" not in ui.text())
    check("commands: exit prints the saving report", "省" in ui.text())


def scenario_plan(model: str) -> None:
    repo = fresh_repo("plan")
    token = "MATRIX-PLAN-9C1"
    out = repo / "plan_out.txt"
    started = time.time()
    ui = XenonUI(repo, model=model)
    try:
        opened = ui.run_command("/plan on", "计划模式已开启", 15)
        # With "不要修改其他文件" read as a scope guard, this task resolves
        # WRITE and plan mode converts it to plan-only: a plan is proposed
        # and the write is deferred instead of escalating to an approval.
        ok, approvals = ui.run_turn(
            f"创建 plan_out.txt，内容为 {token}；不要修改其他文件。",
            approve="2",
            timeout=360,
        )
        deferred = not out.exists()
        pending = ui.run_command("/plan status", "计划待确认", 30)
        approved, _ = ui.run_command_with_approval(
            "/plan approve", "计划已批准", approve="1", timeout=420
        )
    finally:
        ui.close()
    elapsed = time.time() - started
    facts = ledger_facts(newest_ledger(None, started))
    record("plan", ui, facts, elapsed)
    check("plan: plan mode opened", opened)
    check("plan: plan-only turn finished", ok)
    check("plan: plan-only asked no write approval", approvals == 0, f"{approvals}")
    check("plan: the write was deferred", deferred)
    check("plan: /plan status reports a pending plan", pending)
    check("plan: /plan approve ran the plan", approved)
    check("plan: output exists with the token", out.exists() and token in out.read_text(encoding="utf-8"))
    check(
        "plan: plan events recorded",
        any(kind in facts["kinds"] for kind in ("plan_approved", "plan_accepted")),
        ",".join(sorted(kind for kind in facts["kinds"] if kind.startswith("plan_"))),
    )


def scenario_session(model: str) -> None:
    repo = fresh_repo("session")
    token = "MATRIX-4719"
    name = f"matrix-{int(time.time())}"
    started = time.time()
    ui = XenonUI(repo, model=model)
    try:
        ui.run_turn(f"请记住这个数字：{token}。不要调用任何工具。")
        saved = ui.run_command(f"/save {name}", "会话已保存", 30)
    finally:
        ui.close()
    resumed = XenonUI(repo, extra_args=["--resume", name], model=model)
    try:
        restored = resumed.wait_text("已恢复会话", 30)
        history = token in resumed.text()
        ok, _ = resumed.run_turn("我上一轮让你记住的数字是什么？只回答数字。")
    finally:
        resumed.close()
    elapsed = time.time() - started
    facts = ledger_facts(newest_ledger(None, started))
    record("session", resumed, facts, elapsed)
    check("session: saved", saved)
    check("session: --resume reports the restored session", restored)
    check("session: the saved conversation is rendered", history)
    check("session: answer comes from the restored history", token in facts["answer"])
    check("session: resumed turn finished", ok)


def scenario_mcp(model: str) -> None:
    repo = fresh_repo("mcp")
    home = isolated_home("mcp")
    server = ROOT / "e2e_scratch" / "mcp_train_server.py"
    python = sys.executable.replace("\\", "/")
    add = f"/mcp add train {python} {str(server).replace(chr(92), '/')}"
    started = time.time()
    ui = XenonUI(repo, home=home, model=model)
    try:
        added = ui.run_command(add, "train", 60)
        tools = ui.run_command("/mcp tools", "query_trains", 30)
        ok, approvals = ui.run_turn(
            "用 query_trains 工具查一下昆山到上海的高铁，把最早一班的车次和发车时间告诉我。",
            approve="1",
        )
    finally:
        ui.close()
    elapsed = time.time() - started
    facts = ledger_facts(newest_ledger(home, started))
    record("mcp", ui, facts, elapsed)
    # Restart: the server must still be there without re-adding it.
    restarted = XenonUI(repo, home=home, model=model)
    try:
        persisted = restarted.run_command("/mcp tools", "query_trains", 30)
    finally:
        restarted.close()
    check("mcp: server added", added)
    check("mcp: tool discovered", tools)
    check("mcp: turn finished", ok)
    check("mcp: the tool was called", any("query_trains" in str(t["tool"]) for t in facts["tools"]), str([t["tool"] for t in facts["tools"]]))
    check("mcp: answer carries real train data", "G7002" in facts["answer"] or "07:12" in facts["answer"])
    check("mcp: tools survive a restart", persisted)


def scenario_cancel(model: str) -> None:
    repo = fresh_repo("cancel")
    started = time.time()
    ui = XenonUI(repo, model=model)
    try:
        ui.type_line(
            "运行 shell 命令 ping -n 10 127.0.0.1，然后把输出告诉我。"
        )
        # Wait for the approval, approve it, then interrupt the running tool.
        deadline = time.time() + 120
        while time.time() < deadline and "需要授权" not in ui.text()[-1200:]:
            ui.pump(0.5)
        approved = "需要授权" in ui.text()[-1200:]
        if approved:
            ui.pty.write("1")
            time.sleep(2.0)
        ui.ctrl_c()
        cancelled = ui.wait_any(("已取消", "取消", "中断"), 20)
        responsive = ui.run_command("/context", "上下文", 20)
        ui.type_line("/exit")
        time.sleep(2.0)
    finally:
        ui.close()
    elapsed = time.time() - started
    facts = ledger_facts(newest_ledger(None, started))
    record("cancel", ui, facts, elapsed)
    check("cancel: approval appeared and was approved", approved)
    check("cancel: Ctrl+C reports a cancellation", cancelled is not None, str(cancelled))
    check("cancel: the REPL still answers commands", responsive)


def scenario_dashboard(model: str) -> None:
    repo = fresh_repo("dashboard")
    ui = XenonUI(repo, extra_args=["--dashboard"], model=model)
    state: dict[str, Any] = {}
    try:
        seen = ui.wait_text("仪表盘已启动", 30)
        match = re.search(
            r"http://127\.0\.0\.1:(\d+)/\?t=([0-9a-f]{16,})", strip_ansi(ui.raw)
        )
        if match:
            port, token = match.group(1), match.group(2)
            base = f"http://127.0.0.1:{port}"
            state["url"] = match.group(0)
            try:
                with urllib.request.urlopen(f"{base}/api/state", timeout=8) as resp:
                    state["no_token_status"] = resp.status
            except urllib.error.HTTPError as exc:
                state["no_token_status"] = exc.code
            with urllib.request.urlopen(
                f"{base}/api/state?t={token}", timeout=8
            ) as resp:
                state["no_token_ok_status"] = resp.status
                state["snapshot"] = json.loads(resp.read().decode("utf-8"))
            query = f"{base}/api/query?t={token}&kind=dashboard.snapshot&params=%7B%7D"
            with urllib.request.urlopen(query, timeout=8) as resp:
                state["query"] = json.loads(resp.read().decode("utf-8"))
    finally:
        ui.close()
    print(
        f"  dashboard={state.get('url')} no_token={state.get('no_token_status')}"
        f" query_keys={sorted((state.get('query') or {}).keys())[:6]}"
    )
    check("dashboard: startup line shown", seen)
    check("dashboard: URL and token parsed", "url" in state)
    check("dashboard: no token is rejected", state.get("no_token_status") in {401, 403}, str(state.get("no_token_status")))
    check("dashboard: /api/state renders with the token", state.get("no_token_ok_status") == 200)
    check("dashboard: snapshot has state", isinstance(state.get("snapshot", {}).get("state"), dict))
    check("dashboard: dashboard.snapshot query answers", bool(state.get("query")))


SCENARIOS: dict[str, Callable[[str], None]] = {
    "read": scenario_read,
    "write": scenario_write,
    "bugfix": scenario_bugfix,
    "commands": scenario_commands,
    "plan": scenario_plan,
    "session": scenario_session,
    "mcp": scenario_mcp,
    "cancel": scenario_cancel,
    "dashboard": scenario_dashboard,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="", help="comma separated scenario names")
    parser.add_argument("--model", default=MODEL)
    args = parser.parse_args()
    wanted = [name.strip() for name in args.only.split(",") if name.strip()]
    names = wanted or list(SCENARIOS)

    BASE.mkdir(parents=True, exist_ok=True)
    print(f"model={args.model} scenarios={names}")
    for name in names:
        scenario = SCENARIOS.get(name)
        if scenario is None:
            print(f"unknown scenario: {name}")
            return 2
        print(f"\n── {name} ──")
        try:
            scenario(args.model)
        except Exception as exc:  # noqa: BLE001 — keep running the matrix
            check(f"{name}: scenario ran", False, f"{type(exc).__name__}: {exc}")

    failed = [item for item in FINDINGS if not item["passed"]]
    total_wall = sum(item["wall"] for item in METRICS)
    total_tokens = sum(item["tokens"] for item in METRICS)
    lines = [
        "# 组件真实任务矩阵",
        "",
        f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 模型：`{args.model}`",
        "- 入口：`node frontend/dist/cli.js`（真实 ConPTY，auto-edit）",
        f"- 结果：**{len(FINDINGS) - len(failed)}/{len(FINDINGS)} PASS**",
        f"- 用时合计：{total_wall:.1f}s · token 合计：{total_tokens:,}",
        "",
        "## 组件用时与证据",
        "",
        "| 场景 | 用时 | tokens | 工具 | 证据 | accepted | 账本 |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in METRICS:
        lines.append(
            f"| {item['name']} | {item['wall']:.1f}s | {item['tokens']:,} | "
            f"{', '.join(item['tools']) or '-'} | {item['evidence'] or '-'} | "
            f"{item['accepted']} | {item['ledger']} |"
        )
    lines += [
        "",
        "## 检查项",
        "",
        "| 检查项 | 结果 | 说明 |",
        "|---|---|---|",
    ]
    for item in FINDINGS:
        lines.append(
            f"| {item['name']} | {'PASS' if item['passed'] else 'FAIL'} | "
            f"{one_line(item['detail']).replace('|', '/')} |"
        )
    (HERE / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        f"\n{len(FINDINGS) - len(failed)}/{len(FINDINGS)} checks passed"
        f" -> {HERE / 'report.md'}"
    )
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
