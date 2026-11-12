"""对照实验：形状匹配到底有没有让'同一条命令的不同拼写'免问。

真实 TTY 里模型会不断换写法，看不出单点效果。这里用真实的 REPL + 真实的
panel 路径，只改模型给的命令字符串（模拟它的几种改写），观察面板次数。
"""

import pathlib
import sys
import tempfile

sys.path.insert(0, r"D:\Xenon")

import xenon.repl.session as S

S.SESSIONS_DIR = pathlib.Path(tempfile.mkdtemp())
import xenon.repl.approval_ux as AU

AU.sys = type("FakeSys", (), {"stdin": type("T", (), {"isatty": lambda self: True})()})()

from xenon.kernel.contracts import Capability, ToolIntent
from xenon.kernel.policies import PermissionMode, PermissionPolicy
from xenon.repl.repl import REPL
from xenon.repl.model_registry import ModelRegistry

repl = REPL(registry=ModelRegistry(), streaming=False)
answered = []


def ask(default="n", *, from_child=False):
    answered.append(1)
    return "a"


repl._ask_approval_choice = ask
ws = str(repl._workspace_root())

# 人按 a 授权第一条命令
repl._confirm_tool("bash", {"command": 'python -c "print(123)"'}, "CRITICAL")
print(f"面板次数（授权时）: {len(answered)}")
print(f"book: {[(g.capability.value, g.targets) for g in repl._grant_book]}")

# 之后内核用策略裁决同一意图的不同拼写
variants = [
    ('python -c "print(123)"', True, "原样重复"),
    ('python "-c" "print(123)"', True, "引号换写法"),
    ("python -c 'print(123)'", True, "单引号"),
    ('python  -c   "print(123)"', True, "多余空格"),
    ("python -c \"print(123)\"  # note", True, "行尾注释"),
    ('python -c "print(999)"', False, "改了参数"),
    ("git status", False, "换了程序"),
    ("rm -rf /", False, "危险命令"),
    ('python -c "print(123)" && rm -rf /', False, "链式(提权)"),
    ('"print(123)" | python -', False, "管道(stdin)"),
]

print("\n内核裁决：")
bad = 0
for command, should_allow, note in variants:
    policy = PermissionPolicy(
        mode=PermissionMode.AUTO_EDIT,
        workspace=ws,
        allow_approval=True,
        grants=tuple(repl._grant_book.active(ws)),
    )
    decision = policy.check_intent(
        ToolIntent(capability=Capability.SHELL, params={"command": command})
    )
    allowed = decision.kind.value == "allow"
    ok = allowed == should_allow
    bad += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  放行={allowed!s:5} 期望={should_allow!s:5}  {note}: {command}")

print(f"\n面板在授权后再未出现: {len(answered) == 1}")
print("全部通过" if bad == 0 else f"{bad} 项失败")
