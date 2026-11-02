"""Report coverage per Codecov component (local, deterministic).

`codecov.yml` sets different targets per component, so the numbers those targets
refer to must be reproducible without Codecov.  This script reads a
`coverage.xml` (produced by `pytest --cov-report=xml`) and prints the same
grouping the config uses, so a target can be checked before it is promised.

Usage:
    python scripts/cov_components.py coverage.xml
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

# Mirrors `component_management` in codecov.yml.  Source paths in coverage.xml
# are relative to the coverage source root, so they do not carry the `xenon/`
# prefix that Codecov's repo-relative globs use.
COMPONENTS: dict[str, tuple[str, ...]] = {
    "engine-core": ("kernel/", "harness/", "catalog/"),
    "ui-repl": ("repl/",),
    "legacy-organs": (
        "engine/",
        "nodes/",
        "tools/",
        "utils/",
        "mcp/",
        "memory/",
        "session/",
    ),
}

TARGETS: dict[str, float] = {
    "engine-core": 90.0,
    "ui-repl": 70.0,
}


def _group(filename: str) -> str:
    for name, prefixes in COMPONENTS.items():
        if filename.startswith(prefixes):
            return name
    return "other"


def measure(path: Path) -> tuple[dict[str, list[int]], int, int]:
    root = ET.parse(path).getroot()
    buckets: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    total = missed = 0
    for cls in root.iter("class"):
        lines = cls.find("lines")
        if lines is None:
            continue
        count = hit = 0
        for line in lines:
            if line.get("hits") is None:
                continue
            count += 1
            hit += 1 if int(line.get("hits")) > 0 else 0
        if not count:
            continue
        total += count
        missed += count - hit
        bucket = buckets[_group(cls.get("filename") or "")]
        bucket[0] += count
        bucket[1] += count - hit
    return buckets, total, missed


def render(buckets: dict[str, list[int]], total: int, missed: int) -> str:
    out = [f"{'component':16}{'lines':>8}{'cover':>9}{'missed':>8}{'target':>8}  status"]
    ordered = sorted(buckets.items(), key=lambda item: -item[1][0])
    for name, (count, misses) in ordered:
        cover = 100 * (count - misses) / count
        target = TARGETS.get(name)
        if target is None:
            status = "info"
        else:
            status = "OK" if cover >= target else f"BELOW ({target:.0f}%)"
        label = f"{target:.0f}%" if target is not None else "-"
        out.append(
            f"{name:16}{count:8d}{cover:8.1f}%{misses:8d}{label:>8}  {status}"
        )
    overall = 100 * (total - missed) / total
    out.append(f"{'TOTAL':16}{total:8d}{overall:8.1f}%{missed:8d}{'-':>8}  -")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    path = Path(argv[0]) if argv else Path("coverage.xml")
    if not path.is_file():
        print(f"no coverage report at {path}", file=sys.stderr)
        return 2
    buckets, total, missed = measure(path)
    print(render(buckets, total, missed))
    failing = [
        name
        for name, (count, misses) in buckets.items()
        if name in TARGETS and 100 * (count - misses) / count < TARGETS[name]
    ]
    return 1 if failing else 0


if __name__ == "__main__":
    raise SystemExit(main())
