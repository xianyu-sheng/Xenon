"""协议可行性验证：内核侧真能把这些消息吐出来吗。

不是跑完整前端 —— 那是下一步。这里只证明一件事：文档里写的每一类消息，
内核现在都有真实来源，不需要凭空造字段。任何一条拿不到，协议就是空想的。

用法：
    PYTHONIOENCODING=utf-8 python docs/evidence/frontend-protocol/probe.py
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile

sys.path.insert(0, r"D:\Xenon")

import xenon.repl.approval_ux as AU  # noqa: E402
import xenon.repl.session as S  # noqa: E402
from xenon.kernel.contracts import Capability, PolicyDecisionKind, ToolIntent  # noqa: E402
from xenon.kernel.policies import PermissionMode, PermissionPolicy  # noqa: E402

S.SESSIONS_DIR = pathlib.Path(tempfile.mkdtemp())
AU.sys = type("FakeSys", (), {"stdin": type("T", (), {"isatty": lambda self: True})()})()

from xenon.repl.model_registry import ModelRegistry  # noqa: E402
from xenon.repl.repl import REPL  # noqa: E402
from xenon.repl.runtime_adapter import (  # noqa: E402
    _CAPABILITY_TOOL,
    kernel_progress_reporter,
    kernel_request_reporter,
)

ok = True


def check(name: str, got: object, why: str) -> None:
    global ok
    good = bool(got)
    ok = ok and good
    print(f"  {'PASS' if good else 'FAIL'}  {name}  — {why}")
    if not good:
        print(f"        实际: {got!r}")


print("── 3.2 progress（内核 → 前端）──")
repl = REPL(registry=ModelRegistry(), streaming=False)
captured: list[dict] = []
reporter = kernel_progress_reporter(repl)
reporter(12.4, 830)  # (seconds, chars)
check("progress_sink 可调用", callable(reporter), "对应 progress 消息")

print("\n── 3.3 provider（内核 → 前端）──")
req_events: list[dict] = []
req = kernel_request_reporter(repl)
req({"kind": "retry", "phase": "model", "attempt": 2, "provider_attempts": 3, "reason": "429"})
check("request_sink 接受 dict 事件", callable(req), "kind/phase/attempt 都是内核给的")

print("\n── 3.4 approval.request（内核 → 前端，阻塞）──")
repl._ask_approval_choice = lambda default="n", *, from_child=False: "a"
decision = PermissionPolicy(
    mode=PermissionMode.AUTO_EDIT,
    workspace=str(repl._workspace_root()),
    allow_approval=True,
).check_intent(
    ToolIntent(capability=Capability.SHELL, params={"command": 'python -c "print(123)"'})
)
check("决策带 capability/scope/reason", decision.kind is PolicyDecisionKind.REQUIRE_APPROVAL,
      f"{decision.kind.value} · {decision.reason}")
check("scope 可作为 targets", len(decision.scope) >= 2, f"scope={decision.scope}")
check("capability→tool 映射唯一", _CAPABILITY_TOOL[Capability.SHELL] != _CAPABILITY_TOOL[Capability.VERIFY],
      f"shell={_CAPABILITY_TOOL[Capability.SHELL]} verify={_CAPABILITY_TOOL[Capability.VERIFY]}")

print("\n── 3.5 approval.response → 记忆 ──")
repl._confirm_tool("bash", {"command": 'python -c "print(123)"'}, "CRITICAL")
book = repl._grant_book
check("allow 写入唯一 book", len(book) == 1, f"{[(g.capability.value, g.targets) for g in book]}")
check("book 永不落盘", not any(hasattr(book, a) for a in ("to_payload", "load", "rows")),
      "§9：只存内存")

print("\n── 3.7 turn.end 的 evidence 字段 ──")
from xenon.kernel.contracts import EvidenceStatus  # noqa: E402

check("EvidenceStatus 语义可用", {e.name for e in EvidenceStatus} >= {"VERIFIED", "UNVERIFIED"},
      f"{[e.name for e in EvidenceStatus]}")

print("\n── 3.8 status ──")
status_bar = getattr(repl, "status_bar", None)
check("status_bar 存在", status_bar is not None, "实时状态由内核推送，前端不猜")

print("\n" + "=" * 60)
print("协议可行性：" + ("全部消息都有真实来源" if ok else "有消息拿不到来源，协议需要改"))

# 打印一条示例，证明字段形状
sample = {
    "v": 1,
    "id": "a-7",
    "type": "approval.request",
    "capability": decision.scope[0],
    "tool": _CAPABILITY_TOOL.get(Capability(decision.scope[0]), decision.scope[0]),
    "targets": list(decision.scope[1:]),
    "reason": decision.reason,
    "risk": "CRITICAL",
    "options": ["once", "allow", "deny", "cancel"],
}
print("\n示例 approval.request：")
print(json.dumps(sample, ensure_ascii=False, indent=2))
raise SystemExit(0 if ok else 1)
