"""Guard: user-facing text must not still tell the multi-engine story (#294).

The seven engines are gone; the words outlived them.  This check fails when a
*string literal* in the shipped package uses the retired vocabulary, so the
story cannot quietly come back — the same idea as the kernel-purity guard, one
layer up (what users read instead of what code imports).

Legitimate uses are allowlisted by exact substring, with a reason each.

Usage:
    python scripts/check_engine_vocabulary.py
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "xenon"

# Words that describe the retired design.  "引擎" is still fine when it names a
# real search engine; see ALLOW.
BANNED = ("范式", "多引擎")
BANNED_CONTEXT = ("思考引擎", "推理引擎", "引擎类型", "默认 ReAct 引擎")

ALLOW: tuple[str, ...] = (
    "Brave 搜索引擎",  # a product name, not our architecture
    "不是不同的引擎",  # the line that explains the retirement
)

# The tool-runtime package keeps its historical directory name; its docstrings
# are internal, and the guard should not block a legitimate mention there.
INTERNAL_PREFIXES = ("xenon/engine/",)

def _strings(path: Path) -> list[tuple[int, str]]:
    """Every string constant in the file, with the line the match sits on.

    Reading the source with a regex missed multi-line docstrings — the banned
    word sat on a line that carried no quote marks, so the old check passed
    while four docstrings still said 推理范式.  The AST knows where a string
    starts and ends; the line offset is counted from there.
    """

    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:  # a file we cannot read is not this guard's business
        return []
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append((node.lineno, node.value))
    return found


def _line_of(text: str, needle: str, first_line: int) -> int:
    before = text[: text.index(needle)]
    return first_line + before.count("\n")


def violations(root: Path = PACKAGE) -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    for path in root.rglob("*.py"):
        try:
            rel = path.relative_to(ROOT).as_posix()
        except ValueError:
            # A caller may point at a directory outside the repo (tests do):
            # the rule still applies, only the label changes.
            rel = path.as_posix()
        if any(rel.startswith(prefix) for prefix in INTERNAL_PREFIXES):
            continue
        for first_line, literal in _strings(path):
            if any(allowed in literal for allowed in ALLOW):
                continue
            hit = next(
                (
                    word
                    for word in (*BANNED, *BANNED_CONTEXT)
                    if word in literal
                ),
                None,
            )
            if hit is None:
                continue
            offset = _line_of(literal, hit, first_line)
            line = literal.splitlines()[offset - first_line].strip()
            found.append((rel, offset, line[:120]))
            break
    return found


def main() -> int:
    found = violations()
    if not found:
        print("engine-vocabulary guard OK: user-facing text says 'mode', not 'engine'")
        return 0
    print("engine-vocabulary guard: user-facing text still uses retired vocabulary")
    for rel, lineno, text in found:
        print(f"  - {rel}:{lineno} -> {text}")
    print(
        "\nUse 模式 (mode) for a preset of the one loop.  If the mention is a"
        "\nproduct name or an explicit explanation of the retirement, add it to"
        "\nALLOW with a one-line reason."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
