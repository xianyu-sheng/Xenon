"""Local patch-coverage gate: know before you push, not by email.

Codecov's patch status is a real gate in this repository, but it only speaks
*after* a push.  That is the wrong moment to learn that the REPL layer — the part
the full test suite covers least — has 60% coverage on the lines you added.

This script answers the same question offline:

* which lines did this branch add?  (from `git diff` — same idea as Codecov)
* of those, which were executed?  (from `coverage.xml`)
* what is the patch percentage, and exactly which lines are missing?

Only *executable* added lines count.  A blank line or a comment cannot be
covered, and neither Codecov nor this script pretends otherwise.

Usage::

    python scripts/patch_cov.py                 # run the suite, then check
    python scripts/patch_cov.py --from-xml      # reuse an existing coverage.xml
    python scripts/patch_cov.py --base origin/main --threshold 95

Exit codes: 0 pass, 1 below threshold or nothing measurable, 2 usage/setup error.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_THRESHOLD = 95.0
DEFAULT_BASE = "main"
DEFAULT_XML = Path("coverage.xml")

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True, slots=True)
class Hunk:
    path: str
    start: int
    count: int

    def lines(self) -> range:
        return range(self.start, self.start + self.count)


@dataclass
class Report:
    measured: dict[str, set[int]] = field(default_factory=dict)
    covered: set[tuple[str, int]] = field(default_factory=set)
    missing: dict[str, list[int]] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(len(lines) for lines in self.measured.values())

    @property
    def hit(self) -> int:
        return sum(
            len([line for line in lines if (path, line) in self.covered])
            for path, lines in self.measured.items()
        )

    @property
    def percent(self) -> float:
        total = self.total
        return 100.0 * self.hit / total if total else 100.0


def parse_added_lines(diff: str) -> dict[str, set[int]]:
    """Added line numbers per file, from a unified diff with zero context."""

    added: dict[str, set[int]] = {}
    current: str | None = None
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            path = raw[4:].strip()
            if path == "/dev/null":
                current = None
                continue
            current = path[2:] if path.startswith("b/") else path
            added.setdefault(current, set())
            continue
        match = _HUNK.match(raw)
        if match and current is not None:
            start = int(match.group(1))
            count = int(match.group(2) or 1)
            # A zero-length hunk means a pure deletion: nothing was added.
            added[current].update(range(start, start + count))
    return added


def _resolve_base(base: str, *, cwd: Path | None = None) -> str:
    """Find the base ref, trying the remote-tracking form too.

    CI checks out a PR head without `origin/<base>` as a local ref, so a plain
    `--base main` dies with "bad revision".  Trying the two spellings keeps one
    command working locally and in CI; guessing beyond that would be worse than
    failing, so the error lists what was tried.
    """

    tried: list[str] = []
    for candidate in (base, f"origin/{base}"):
        tried.append(candidate)
        merge_base = subprocess.run(
            ["git", "merge-base", candidate, "HEAD"],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=False,
        )
        if merge_base.returncode == 0 and merge_base.stdout.strip():
            return merge_base.stdout.strip()
    raise SystemExit(
        "cannot find a merge base for "
        + " / ".join(tried)
        + chr(10)
        + "hint: `git fetch origin " + base + "` (CI does this explicitly)"
    )


def git_diff(base: str, *, cwd: Path | None = None) -> str:
    """`git diff` against the merge base, so a stale base is not counted twice."""

    target = _resolve_base(base, cwd=cwd)
    diff = subprocess.run(
        [
            "git",
            "-c",
            "core.quotepath=false",
            "diff",
            "--unified=0",
            "--diff-filter=d",
            target,
            "HEAD",
            "--",
            "*.py",
        ],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if diff.returncode != 0:
        raise SystemExit(f"git diff failed: {diff.stderr.strip()[:200]}")
    return diff.stdout


def read_coverage(path: Path) -> dict[str, dict[int, int]]:
    """``filename -> {line: hits}`` from a Cobertura-style coverage.xml."""

    if not path.is_file():
        raise SystemExit(
            f"no coverage report at {path}; run pytest --cov-report=xml first"
        )
    root = ET.parse(path).getroot()
    measured: dict[str, dict[int, int]] = {}
    for cls in root.iter("class"):
        name = cls.get("filename") or ""
        if not name:
            continue
        lines = cls.find("lines")
        if lines is None:
            continue
        entry = measured.setdefault(name, {})
        for line in lines:
            number = line.get("number")
            hits = line.get("hits")
            if number is None or hits is None:
                continue
            entry[int(number)] = max(entry.get(int(number), 0), int(hits))
    return measured


def _normalise(path: str) -> str:
    """Coverage paths are relative to the source root; diff paths are repo paths."""

    text = path.replace("\\", "/").lstrip("./")
    return text[6:] if text.startswith("xenon/") else text


def build_report(
    added: dict[str, set[int]], coverage: dict[str, dict[int, int]]
) -> Report:
    report = Report()
    lookup = {_normalise(name): lines for name, lines in coverage.items()}
    for path, lines in added.items():
        key = _normalise(path)
        executed = lookup.get(key)
        if executed is None:
            # Not measured at all (tests, scripts, docs): Codecov does not count
            # it either, so neither do we.
            continue
        executable = {line for line in lines if line in executed}
        if not executable:
            continue
        report.measured[key] = executable
        missed = sorted(line for line in executable if executed[line] == 0)
        if missed:
            report.missing[key] = missed
        for line in executable:
            if executed[line] > 0:
                report.covered.add((key, line))
    return report


def render(report: Report, *, threshold: float, limit: int = 40) -> str:
    lines = [
        f"patch coverage: {report.percent:.2f}% "
        f"({report.hit}/{report.total} executable added lines, "
        f"threshold {threshold:.0f}%)"
    ]
    if not report.missing:
        lines.append("all added executable lines are covered")
        return "\n".join(lines)
    lines.append("")
    shown = 0
    for path, missing in sorted(
        report.missing.items(), key=lambda item: -len(item[1])
    ):
        text = ", ".join(str(number) for number in missing)
        lines.append(f"  {path}: {len(missing)} missing -> {text}")
        shown += len(missing)
        if shown >= limit:
            break
    return "\n".join(lines)


def stale_sources(xml: Path) -> list[str]:
    """Source files newer than the coverage report.

    A reused report can describe a tree that no longer exists, and then every
    "missing" line is a lie.  Reporting the staleness is cheaper than silently
    being wrong.
    """

    if not xml.is_file():
        return []
    stamp = xml.stat().st_mtime
    newer: list[str] = []
    for path in Path("xenon").rglob("*.py"):
        try:
            if path.stat().st_mtime > stamp:
                newer.append(str(path))
        except OSError:
            continue
    return newer


def run_suite(xml: Path) -> None:
    print("[*] running the offline suite to produce a coverage report ...")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests",
            "-q",
            "--timeout=120",
            "--cov=xenon",
            f"--cov-report=xml:{xml}",
        ],
        check=False,
    )
    if result.returncode not in (0, 5):  # 5 = no tests collected (should not happen)
        raise SystemExit(f"pytest failed with {result.returncode}; fix it first")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument(
        "--threshold", type=float, default=DEFAULT_THRESHOLD, help="percent"
    )
    parser.add_argument("--xml", default=str(DEFAULT_XML))
    parser.add_argument(
        "--from-xml",
        action="store_true",
        help="reuse an existing coverage.xml instead of running the suite",
    )
    args = parser.parse_args(argv)

    xml = Path(args.xml)
    if not args.from_xml:
        run_suite(xml)
    else:
        newer = stale_sources(xml)
        if newer:
            print(
                f"[!] {xml} is older than {len(newer)} source file(s) "
                f"(e.g. {newer[0]}) — results may be wrong; drop --from-xml"
            )

    added = parse_added_lines(git_diff(args.base))
    report = build_report(added, read_coverage(xml))
    print(render(report, threshold=args.threshold))
    if report.total == 0:
        print("[!] nothing measurable in this diff (no executable added lines)")
        return 0
    return 0 if report.percent >= args.threshold else 1


if __name__ == "__main__":
    raise SystemExit(main())
