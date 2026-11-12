"""对可疑模块逐个追调用链：它真的会被执行吗？

对每个候选给出三件事：
  1. 生产代码里的 import / 调用点
  2. 是否在启动路径、内核回合路径上被触达
  3. 判定：活的 / 仅被别的死模块引用 / 真的没人用
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(r"D:\Xenon")
PKG = REPO / "xenon"

CANDIDATES = [
    ("xenon/repl/command_groups/evidence.py", "repl/command_groups/evidence"),
    ("xenon/repl/clickable_paths.py", "repl/clickable_paths"),
    ("xenon/harness/request_budget.py", "harness/request_budget"),
    ("xenon/harness/coding_contract.py", "harness/coding_contract"),
    ("xenon/utils/provider_events.py", "utils/provider_events"),
    ("xenon/repl/command_groups/lessons.py", "repl/command_groups/lessons"),
    ("xenon/session/archive.py", "session/archive"),
    ("xenon/session/projection.py", "session/projection"),
    ("xenon/memory/locking.py", "memory/locking"),
]

EXCLUDE = ("/tests/", "\\tests\\", "__pycache__")


def files() -> list[Path]:
    return [
        p
        for p in PKG.rglob("*.py")
        if "__pycache__" not in p.parts
    ]


def hits_for(module_path: str, name: str, all_files: list[Path]) -> list[str]:
    short = name.rsplit("/", 1)[-1]
    out: list[str] = []
    for path in all_files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if str(path).replace("\\", "/").endswith(module_path):
            continue
        marker = f"xenon.{name.replace('/', '.')}"
        for pattern in (marker, rf"from \.{1,2}{re.escape(short)}\b", rf"\b{short}\.py\b"):
            if re.search(pattern, text):
                rel = str(path.relative_to(REPO)).replace("\\", "/")
                out.append(rel)
                break
    return out


def registered(name: str, all_files: list[Path]) -> bool:
    """Is it pulled in by the command-registry auto-discovery?"""

    if "command_groups/" not in name:
        return False
    discovery = REPO / "xenon/repl/commands.py"
    return "_import_command_group_modules" in discovery.read_text(encoding="utf-8")


def main() -> int:
    all_files = files()
    print(f"源码文件：{len(all_files)}\n")
    for module_path, name in CANDIDATES:
        path = REPO / module_path
        if not path.exists():
            print(f"✗ {module_path} 不存在\n")
            continue
        lines = len(path.read_text(encoding="utf-8").splitlines())
        users = hits_for(module_path, name, all_files)
        auto = registered(name, all_files)
        print(f"── {module_path}  ({lines} 行)")
        if auto:
            print("   由 command_registry 自动发现导入（斜杠命令）")
        if users:
            print(f"   引用 {len(users)} 处：")
            for user in users[:8]:
                print(f"     · {user}")
            if len(users) > 8:
                print(f"     … 另 {len(users) - 8} 处")
        else:
            print("   生产代码零引用")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
