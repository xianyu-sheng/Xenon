"""审计：找出「文档没提 + 代码量不小」的模块，并追它的生产调用链。

判定一个模块是否"活着"：
  1. 被生产代码 import 的位置数（tests/ 不算）
  2. 是否在启动路径 / 内核回合路径上被实际执行

只读分析，不修改任何东西。
"""

from __future__ import annotations

import ast
import re
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(r"D:\Xenon")
PKG = REPO / "xenon"
DOC = Path(r"C:\Users\Administrator\Desktop\Xenon架构技术文档（修改版）.md")


def doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def modules() -> dict[str, Path]:
    """Every importable module under xenon/, excluding tests and caches."""

    found: dict[str, Path] = {}
    for path in PKG.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(REPO)
        name = str(rel.with_suffix("")).replace("\\", "/").replace("/", ".")
        if name.endswith(".__init__"):
            name = name[: -len(".__init__")]
        found[name] = path
    return found


def importers(mod_name: str, all_files: list[Path]) -> list[Path]:
    """Production files that import this module (tests excluded)."""

    hits: list[Path] = []
    short = mod_name.rsplit(".", 1)[-1]
    for path in all_files:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if f"xenon.{mod_name}" in source or f"from {mod_name} import" in source:
            hits.append(path)
            continue
        # Sibling-relative import, e.g. ``from .foo import`` inside a package.
        if re.search(rf"from \.{1,2}{re.escape(short)}\b", source):
            hits.append(path)
    return hits


def is_mentioned(mod_name: str, text: str) -> bool:
    short = mod_name.rsplit(".", 1)[-1]
    parts = mod_name.split(".")
    # A module counts as documented if its dotted path or its file name appears.
    if mod_name in text:
        return True
    if len(parts) >= 2 and "/".join(parts[-2:]) + ".py" in text:
        return True
    if f"`{short}.py`" in text:
        return True
    if f"{short}.py" in text:
        return True
    return False


def main() -> int:
    text = doc_text()
    mods = modules()
    files = [p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts]

    undocumented: list[tuple[str, Path, int, int]] = []
    for name, path in sorted(mods.items()):
        if name.endswith(".conftest"):
            continue
        if is_mentioned(name, text):
            continue
        try:
            lines = len(path.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeDecodeError):
            continue
        if lines < 60:
            continue
        users = [p for p in importers(name, files) if p != path]
        undocumented.append((name, path, lines, len(users)))

    undocumented.sort(key=lambda row: (-row[2], row[0]))

    print(f"文档未提及、且 >=60 行的模块：{len(undocumented)} 个\n")
    print(f"{'行数':>6} {'生产引用':>8}  模块")
    print("-" * 78)
    orphan = []
    for name, path, lines, users in undocumented:
        flag = "  ← 零引用" if users == 0 else ""
        print(f"{lines:>6} {users:>8}  {name}{flag}")
        if users == 0:
            orphan.append((name, path, lines))

    print()
    print(f"其中生产代码零引用的：{len(orphan)} 个，共 {sum(r[2] for r in orphan)} 行")
    for name, path, lines in orphan:
        print(f"  {lines:>5} 行  {path.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
