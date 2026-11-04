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

import re
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

STRING = re.compile(r'"[^"]{2,}"|f"[^"]{2,}"')


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
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            literals = STRING.findall(line)
            if not literals:
                continue
            for literal in literals:
                if any(allowed in literal for allowed in ALLOW):
                    continue
                hit = any(word in literal for word in BANNED) or any(
                    phrase in literal for phrase in BANNED_CONTEXT
                )
                if hit:
                    found.append((rel, lineno, literal.strip()[:120]))
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
