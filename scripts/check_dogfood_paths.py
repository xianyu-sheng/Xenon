"""Deterministic end-to-end check of the paths the model cannot verify today.

No network, no model: a scripted strategy drives the real kernel, real
gateway, real tools and a real git repository, so the assertions are about
behaviour rather than about prompts.

Usage: python scripts/check_dogfood_paths.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from xenon.harness.runner import run_task
from xenon.kernel.contracts import (
    CallTool,
    Capability,
    Finish,
    ToolIntent,
)
from xenon.ledger.store import Ledger

ROWS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    ROWS.append((name, ok, detail))


class Script:
    """A strategy that replays a fixed list of proposals."""

    def __init__(self, actions: list[Any]) -> None:
        self.actions = list(actions)
        self.seen: list[Any] = []

    def propose(self, view: Any) -> Any:
        self.seen.append(view)
        if not self.actions:
            return Finish("done")
        return self.actions.pop(0)


def _git_repo(root: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "d@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Dogfood"], cwd=root, check=True)
    (root / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    return root


def _run(root: Path, actions: list[Any], objective: str = "do the work"):
    script = Script(actions)
    run = run_task(
        objective,
        strategy=script,
        workspace=root,
        ledger_dir=root / ".ledger",
        finalize=False,
        max_steps=0,
    )
    events = Ledger.open(run.ledger_path).read()
    receipts = [
        event.payload["receipt"]
        for event in events
        if event.kind.value == "tool_receipt"
    ]
    return run, events, receipts


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    with tempfile.TemporaryDirectory() as tmp:
        root = _git_repo(Path(tmp))

        # 1. A structured test run records a real TestRun fact.
        (root / "tests").mkdir()
        (root / "tests" / "test_ok.py").write_text(
            "def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8"
        )
        _run(
            root,
            [
                CallTool(
                    ToolIntent(
                        capability=Capability.VERIFY,
                        params={"test_paths": ["tests/test_ok.py"]},
                    )
                )
            ],
        )
        run, events, receipts = _run(
            root,
            [
                CallTool(
                    ToolIntent(
                        capability=Capability.VERIFY,
                        params={"test_paths": ["tests/test_ok.py"]},
                    )
                )
            ],
        )
        test_receipts = [item for item in receipts if item["tool"] == "test"]
        check(
            "结构化 test 工具被采用（而非 bash）",
            bool(test_receipts),
            f"tools={[r['tool'] for r in receipts]}",
        )
        if test_receipts:
            effects = [
                effect
                for effect in test_receipts[-1]["effects"]
                if effect.get("type") == "test_run"
            ]
            check(
                "test 产生真实 TestRun 事实",
                bool(effects) and effects[0].get("passed") == 1,
                json.dumps(effects[:1], ensure_ascii=False),
            )

        # 2. Read-only git is approved without a human.
        run, events, receipts = _run(
            root,
            [CallTool(ToolIntent(capability=Capability.VCS, params={"git_command": "status"}))],
        )
        git_receipts = [item for item in receipts if item["tool"] == "git"]
        check(
            "只读 git 无需审批即可执行",
            bool(git_receipts) and git_receipts[-1]["status"] == "ok",
            f"tools={[r['tool'] for r in receipts]} status="
            f"{git_receipts[-1]['status'] if git_receipts else 'none'}",
        )
        denies = [
            event.payload
            for event in events
            if event.kind.value == "policy_decision"
            and "VCS requires" in str(event.payload.get("reason"))
        ]
        check("只读 git 不触发 VCS 授权要求", not denies, f"denies={denies}")

        # 3. A refused action does not end the turn.
        run, events, receipts = _run(
            root,
            [
                CallTool(
                    ToolIntent(
                        capability=Capability.SHELL,
                        params={"command": "echo hi"},
                    )
                ),
                CallTool(
                    ToolIntent(
                        capability=Capability.READ,
                        params={"file_path": "README.md"},
                    )
                ),
            ],
        )
        decisions = [
            event.payload.get("decision")
            for event in events
            if event.kind.value == "control_decision"
        ]
        read_after_refusal = any(item["tool"] == "read" for item in receipts)
        check(
            "被拒绝的动作之后循环继续（不是整轮结束）",
            "refuse" in decisions and read_after_refusal,
            f"decisions={decisions} tools={[r['tool'] for r in receipts]}",
        )
        check(
            "最终仍返回答案（不因证据缺口变空）",
            bool(run.result.answer),
            f"phase={run.result.phase.value} answer={run.result.answer[:40]!r}",
        )

        # 4. Facts from an earlier plan round are credited later.
        run, events, receipts = _run(
            root,
            [
                CallTool(
                    ToolIntent(
                        capability=Capability.EDIT,
                        params={
                            "files": [{"path": "added.py", "content": "x = 1\n"}]
                        },
                    )
                )
            ],
        )
        writes = [
            item
            for item in receipts
            if any(
                effect.get("type") == "fs_write"
                for effect in item["effects"]
            )
        ]
        check(
            "写入产生可核对的 fs_write 事实",
            bool(writes),
            f"tools={[r['tool'] for r in receipts]}",
        )

        # 5. The report reads a blocked run's disclosed answer.
        report_json = subprocess.run(
            [sys.executable, "-m", "xenon", "report", str(run.ledger_path), "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        try:
            report = json.loads(report_json.stdout)
        except ValueError:
            report = {}
        check(
            "报告能读取账本并给出判定",
            report.get("schema") == "xenon.delivery_report/v0",
            f"exit={report_json.returncode} stderr={report_json.stderr[:80]}",
        )
        check(
            "报告区分「收据」与「答案」两种证据",
            "answer_verified" in report.get("status", {}),
            json.dumps(report.get("status", {}), ensure_ascii=False),
        )
        check(
            "已验证收据 + 无 claim ⇒ 答案仍标为未验证",
            report.get("status", {}).get("evidence_status") == "verified"
            and report.get("status", {}).get("answer_verified") is False,
            json.dumps(report.get("status", {}), ensure_ascii=False),
        )

    width = max(len(name) for name, _, _ in ROWS)
    failed = 0
    for name, ok, detail in ROWS:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"{mark}  {name:<{width}}  {detail}")
    print(f"\n{len(ROWS) - failed}/{len(ROWS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
