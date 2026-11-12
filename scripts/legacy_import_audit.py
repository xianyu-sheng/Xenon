"""Audit xenon/engine/* importers for the legacy-retirement decision table."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(".")

engine_files = sorted(Path("xenon/engine").glob("*.py"))
local_names = {f.stem for f in engine_files if f.stem != "__init__"}

sources: dict[Path, str] = {}
for path in list(Path("xenon").rglob("*.py")) + list(Path("tests").rglob("*.py")):
    if "__pycache__" in path.parts:
        continue
    try:
        sources[path] = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        pass

for name in sorted(local_names):
    users: dict[str, int] = {}
    pattern = re.compile(
        rf"(?:from\s+xenon\.engine\.{name}\s+import|import\s+xenon\.engine\.{name}\b|from\s+xenon\.engine\s+import\s+[^\n]*\b)"
    )
    loose = re.compile(r"from\s+xenon\.engine\s+import\s+([^\n]+)")
    hits: list[str] = []
    for path, text in sources.items():
        if path.parts[:2] == ("xenon", "engine"):
            continue
        matched = False
        if pattern.search(text):
            matched = True
        else:
            for group in loose.finditer(text):
                names = [part.strip().split(" as ")[0].strip() for part in group.group(1).replace("(", " ").replace(")", " ").split(",")]
                if name in names:
                    matched = True
                    break
        if matched:
            hits.append(str(path).replace("\\", "/"))
    xenon_users = [h for h in hits if h.startswith("xenon/")]
    test_users = [h for h in hits if h.startswith("tests/")]
    print(f"== {name} ({len(xenon_users)} xenon, {len(test_users)} tests)")
    for h in xenon_users:
        print(f"   X {h}")
    for h in test_users[:6]:
        print(f"   T {h}")
    if len(test_users) > 6:
        print(f"   T ... +{len(test_users) - 6} more")
