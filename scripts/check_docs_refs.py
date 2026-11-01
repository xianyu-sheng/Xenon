#!/usr/bin/env python3
"""Docs-reference guard.

The engine demolition (#202) left several live documents pointing at files that
no longer exist — readers were being told about a registry, an event bus and a
`PlanDAG` that had been deleted.  Documentation that lies is a defect, so this
guard makes the drift impossible to merge.

Rules:

1. A reference to ``xenon/<path>.py`` in a **live** document must resolve.
2. A document marked ``历史文档（归档）`` is archival: its paths describe the
   past on purpose and are skipped (that marker is the only escape hatch).
3. ``docs/evidence/`` is raw evidence and is never scanned: it quotes old
   output verbatim.

Adding a file to ``EXEMPT`` because it "mostly works" defeats the guard. Fix
the document or mark it archival.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCAN = [Path("README.md"), *sorted(Path("docs").rglob("*.md"))]
SKIP_DIRS = {"docs/evidence"}

# A document carrying this marker is describing history, not the code.
ARCHIVAL_MARKER = "历史文档（归档）"

REF_RE = re.compile(r"`(xenon/[A-Za-z0-9_./-]+\.py)`")

EXEMPT: dict[str, str] = {}


def main() -> int:
    missing: list[tuple[str, str]] = []
    for rel in SCAN:
        path = ROOT / rel
        if not path.is_file():
            continue
        posix = rel.as_posix()
        if any(posix.startswith(skip) for skip in SKIP_DIRS):
            continue
        if posix in EXEMPT:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if ARCHIVAL_MARKER in text:
            continue
        for ref in sorted(set(REF_RE.findall(text))):
            if not (ROOT / ref).is_file():
                missing.append((posix, ref))

    if missing:
        print("docs-reference guard: documents point at files that do not exist")
        for doc, ref in missing:
            print(f"  - {doc} -> {ref}")
        print(
            "\nFix the reference, delete the claim, or mark the document with "
            f"'{ARCHIVAL_MARKER}' if it is describing history."
        )
        return 1

    print("docs-reference guard OK")
    print(f"  documents scanned: {len(SCAN)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
