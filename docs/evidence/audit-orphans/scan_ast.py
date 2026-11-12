"""准确的生产引用统计（AST，不是正则）。

正则漏掉了 ``from xenon.utils import atomic_write`` 这种「从包里取子模块」的写法，
把活的模块误报成孤儿。这里用 AST 解析每一条 import，并按包语义展开。
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(r"D:\Xenon")
PKG = REPO / "xenon"
DOC = Path(r"C:\Users\Administrator\Desktop\Xenon架构技术文档（修改版）.md")

PROD_DIRS = ("xenon", "evals", "scripts")


def prod_files() -> list[Path]:
    out: list[Path] = []
    for name in PROD_DIRS:
        root = REPO / name
        if root.exists():
            out += [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]
    return out


def module_of(path: Path) -> str:
    rel = path.relative_to(REPO).with_suffix("")
    parts = str(rel).replace("\\", "/").split("/")
    if parts[0] != "xenon":
        return "/".join(parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def imported_names(path: Path) -> set[str]:
    """Dotted names this file imports, expanded for package imports.

    ``from xenon.utils import atomic_write`` yields both
    ``xenon.utils`` and ``xenon.utils.atomic_write``; the latter is what tells
    us a submodule is really used.
    """

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return set()

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                # Relative import: resolve against this file's package.
                package = module_of(path).split(".")
                if path.name != "__init__.py":
                    package = package[:-1]
                package = package[: len(package) - (node.level - 1)]
                base = ".".join(package + ([base] if base else []))
            if base:
                names.add(base)
            for alias in node.names:
                if base:
                    names.add(f"{base}.{alias.name}")
    return names


def main() -> int:
    text = DOC.read_text(encoding="utf-8")
    files = prod_files()

    imports: dict[Path, set[str]] = {}
    for path in files:
        imports[path] = imported_names(path)

    modules = {
        module_of(p): p
        for p in files
        if str(p.relative_to(REPO)).replace("\\", "/").startswith("xenon/")
    }

    def documented(name: str) -> bool:
        short = name.rsplit(".", 1)[-1]
        if name in text or f"{short}.py" in text:
            return True
        parts = name.split(".")
        return len(parts) >= 2 and "/".join(parts[-2:]) + ".py" in text

    rows: list[tuple[int, int, str, list[str]]] = []
    for name, path in modules.items():
        if documented(name):
            continue
        try:
            lines = len(path.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeDecodeError):
            continue
        if lines < 60:
            continue
        users = [
            str(f.relative_to(REPO)).replace("\\", "/")
            for f, names in imports.items()
            if f != path and name in names
        ]
        rows.append((lines, len(users), name, users))

    rows.sort(key=lambda r: (r[1], -r[0]))
    print(f"文档未提及且 >=60 行：{len(rows)} 个（按生产引用数升序）\n")
    print(f"{'行数':>6} {'引用':>5}  模块")
    print("-" * 76)
    for lines, count, name, users in rows:
        if count > 2:
            continue
        print(f"{lines:>6} {count:>5}  {name}")
        for user in users:
            print(f"           └ {user}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
