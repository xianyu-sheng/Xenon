"""READ_ONLY 到底拦住了什么：三层对照。

一层的判定会喂给下一层，任何一层判错，用户看到的都是"什么都没发生"。
这里把三层分开测，并核对每一层是否符合技术文档 §8 的意图。

文档 §8 原文：「不产生变化（读类）：原则上完全不设限；会产生状态变更（写类）：
因为会造成实际变化，需要询问用户。」——文档没有定义 READ_ONLY 这一模式本身，
只说它存在（§八 现状段）。
"""

from __future__ import annotations

import sys
import tempfile

sys.path.insert(0, r"D:\Xenon")

from xenon.kernel.contracts import Capability, PolicyDecisionKind, ToolIntent
from xenon.kernel.policies import PermissionMode, PermissionPolicy
from xenon.repl.execution_policy import ExecutionLevel, classify_execution_policy

WS = tempfile.mkdtemp()


def line(text: str) -> None:
    print(text)


# ── 第一层：意图 → 执行级别 ────────────────────────────────────────
line("第一层：这句话授权到哪一级？")
line("")
for text in ("写一个 hello.py", "创建一个 hello.py", "运行 pytest"):
    policy = classify_execution_policy(text)
    line(f"  {text!r:<28} → {policy.level.name:<12} {policy.reason}")


# ── 第二层：级别 → 内核模式 ────────────────────────────────────────
line("")
line("第二层：级别映射到哪个 PermissionMode？")
line("")
from xenon.repl.runtime_adapter import permission_mode_for  # noqa: E402

for level in ExecutionLevel:
    line(f"  {level.name:<12} → {permission_mode_for(level).value}")


# ── 第三层：模式下，一次写/执行会被怎样裁决 ────────────────────────
line("")
line("第三层：在每种模式下，写工作区文件 / 执行命令分别得到什么裁决？")
line("")
CASES = [
    ("写工作区文件", ToolIntent(capability=Capability.EDIT, params={"file_path": f"{WS}/a.py", "content": "x"})),
    ("执行命令", ToolIntent(capability=Capability.SHELL, params={"command": "ls"})),
    ("读文件", ToolIntent(capability=Capability.READ, params={"file_path": f"{WS}/a.py"})),
]
header = "  " + "模式".ljust(14) + "".join(name.ljust(18) for name, _ in CASES)
line(header)
for mode in PermissionMode:
    policy = PermissionPolicy(mode=mode, workspace=WS, allow_approval=False)
    cells = []
    for _name, intent in CASES:
        decision = policy.check_intent(intent)
        cells.append(decision.kind.value.ljust(18))
    line("  " + mode.value.ljust(14) + "".join(cells))

# ── 第四层：READ_ONLY 下 allow_approval 能否让人放行 ──────────────
line("")
line("第四层：READ_ONLY 模式下，有审批通道时写/执行能否得到「询问」？")
line("")
for mode in (PermissionMode.READ_ONLY, PermissionMode.AUTO_EDIT):
    policy = PermissionPolicy(mode=mode, workspace=WS, allow_approval=True)
    for name, intent in CASES[:2]:
        decision = policy.check_intent(intent)
        line(f"  {mode.value:<12} {name:<10} → {decision.kind.value:<18} {decision.reason}")

line("")
line("结论要点：")
line("  · READ_ONLY = 不编辑、不执行；即便有审批通道也不能问，直接 VETO。")
line("  · 所以「写一个 hello.py」被判 READ_ONLY 的后果不是「问一下」，")
line("    而是模型连尝试的机会都没有 —— 它收到的拒绝对话里的人是看不见的。")
