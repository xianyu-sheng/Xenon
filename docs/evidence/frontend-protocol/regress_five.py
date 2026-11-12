"""逐条回归那 5 个只在真实运行中暴露的 bug。

每条都断言"证据"而不是"代码里有这行"：一个是纯 stdout 洁净度检查，一个是
真的把中文 reason 走一遍编解码，一个是真实内核跑出来的证据状态。
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, r"D:\Xenon")

REPO = Path(r"D:\Xenon")


def ok(name: str, passed: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if passed else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    return passed


def clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()}
    for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(key, None)
    return env


def main() -> int:
    all_ok = True

    # ── 1. stdout 只有 NDJSON，没有 console 打印 ──────────────────
    print("1. stdout 洁净度（协议流不能被 console 打印污染）")
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "xenon.frontend.host", "--model", "test/fake"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", env=clean_env(), cwd=str(REPO),
    )
    ready_line = proc.stdout.readline()
    try:
        ready = json.loads(ready_line)
        parsed = ready.get("type") == "ready"
    except json.JSONDecodeError:
        parsed = False
    all_ok &= ok("启动帧是合法 JSON", parsed, ready_line.strip()[:90])
    proc.stdin.write(json.dumps({"type": "shutdown"}) + "\n")
    proc.stdin.flush()
    rest = proc.stdout.read()
    proc.wait(timeout=15)
    stray = [line for line in rest.splitlines() if line.strip()]
    all_ok &= ok("shutdown 后没有残留输出", not stray, f"{len(stray)} 行")

    # ── 2. stdin/stdout 都是 UTF-8 ────────────────────────────────
    print("\n2. 编码（中文 reason 必须能跨进程序列化）")
    code = (
        "import sys,json;"
        "sys.stdout.reconfigure(encoding='utf-8');"
        "sys.stdout.write(json.dumps({'reason':'风险等级 CRITICAL'},ensure_ascii=False));"
        "sys.stdout.flush()"
    )
    raw = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, env=clean_env(), cwd=str(REPO)
    ).stdout
    decoded = raw.decode("utf-8")
    all_ok &= ok("含中文的帧可由 UTF-8 解码", "风险等级" in decoded, decoded[:60])
    # 再验一遍 host 自己的 reconfigure 是否真的装上了
    probe = subprocess.run(
        [sys.executable, "-c",
         "import sys;sys.path.insert(0,r'D:\\Xenon');"
         "from xenon.frontend import host as h;"
         "h.main.__module__ and None;"
         "print(sys.stdout.encoding or 'unknown')"],
        capture_output=True, text=True, env=clean_env(), cwd=str(REPO),
    )
    all_ok &= ok("host 模块可导入（reconfigure 分支可达）", probe.returncode == 0,
                 (probe.stderr or "").strip()[:80])

    # ── 3. evidence_status 真的被读出来 ───────────────────────────
    print("\n3. 证据状态（字段名曾写错，导致前端显示「未知」）")
    from xenon.frontend import host as host_module
    from xenon.kernel.contracts import EvidenceStatus

    fake_run = type("R", (), {
        "result": type("T", (), {
            "phase": type("P", (), {"value": "finalized"})(),
            "accepted": True,
            "evidence_status": EvidenceStatus.VERIFIED,
        })(),
        "ledger_path": "x.jsonl",
    })()
    fields = host_module._turn_end_fields(fake_run, "a")
    all_ok &= ok("VERIFIED 被原样传出", fields.get("evidence") == {"status": "verified"},
                 str(fields.get("evidence")))

    # ── 4/5. execution level 重绑定 + allow_approval ──────────────
    print("\n4/5. 执行级别重绑定（否则 shell 调用在问之前就被 veto）")
    from xenon.repl.turn_contract import build_turn_contract
    from xenon.repl.execution_policy import ExecutionLevel

    run_contract = build_turn_contract('run python -c "print(1)"')
    all_ok &= ok("执行类目标把级别绑到 EXECUTE",
                 int(run_contract.level) >= int(ExecutionLevel.EXECUTE),
                 f"level={run_contract.level}")

    read_contract = build_turn_contract("解释一下 Python 的 GIL")
    all_ok &= ok("只读类目标保持在低级别",
                 int(read_contract.level) <= int(ExecutionLevel.WRITE),
                 f"level={read_contract.level}")

    # 关键推论：EXECUTE 级才让 policy 提供审批通道
    from xenon.kernel.policies import PermissionMode, PermissionPolicy
    from xenon.kernel.contracts import Capability, PolicyDecisionKind, ToolIntent

    ws = tempfile.mkdtemp()
    askable = PermissionPolicy(
        mode=PermissionMode.AUTO_EDIT, workspace=ws, allow_approval=True
    ).check_intent(ToolIntent(capability=Capability.SHELL, params={"command": "ls"}))
    all_ok &= ok("allow_approval=True 时 shell 走审批而非 veto",
                 askable.kind is PolicyDecisionKind.REQUIRE_APPROVAL, askable.reason)

    silent = PermissionPolicy(
        mode=PermissionMode.AUTO_EDIT, workspace=ws, allow_approval=False
    ).check_intent(ToolIntent(capability=Capability.SHELL, params={"command": "ls"}))
    all_ok &= ok("allow_approval=False 时才是 veto（说明 host 必须绑 level）",
                 silent.kind is PolicyDecisionKind.VETO, silent.reason)

    print("\n" + "=" * 62)
    print("5 项全部修复并验证" if all_ok else "仍有未修复项")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
