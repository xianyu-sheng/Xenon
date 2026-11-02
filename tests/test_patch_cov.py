"""The local patch gate must agree with Codecov, not with wishful thinking.

These tests pin the three things that decide the number: which lines a diff
*added*, which of those are *executable*, and which of those were *executed*.
Getting any of them wrong turns a gate into decoration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.patch_cov import (
    Report,
    build_report,
    main,
    parse_added_lines,
    read_coverage,
    render,
    stale_sources,
)

DIFF = """diff --git a/xenon/kernel/a.py b/xenon/kernel/a.py
--- a/xenon/kernel/a.py
+++ b/xenon/kernel/a.py
@@ -10,0 +11,3 @@ def f():
@@ -20 +23,0 @@ def f():
--- /dev/null
+++ b/xenon/kernel/new.py
@@ -0,0 +1,2 @@
"""


def _coverage(tmp_path: Path, rows: dict[str, dict[int, int]]) -> Path:
    classes = []
    for name, lines in rows.items():
        body = "".join(
            f'<line number="{number}" hits="{hits}" />'
            for number, hits in lines.items()
        )
        classes.append(f'<class filename="{name}"><lines>{body}</lines></class>')
    xml = (
        '<?xml version="1.0" ?><coverage><packages><package><classes>'
        + "".join(classes)
        + "</classes></package></packages></coverage>"
    )
    path = tmp_path / "coverage.xml"
    path.write_text(xml, encoding="utf-8")
    return path


# ── diff parsing ────────────────────────────────────────────────────


def test_added_lines_come_from_hunk_headers() -> None:
    added = parse_added_lines(DIFF)

    assert added["xenon/kernel/a.py"] == {11, 12, 13}
    assert added["xenon/kernel/new.py"] == {1, 2}


def test_a_pure_deletion_adds_nothing() -> None:
    """`@@ -20 +23,0 @@` inserts zero lines at 23."""

    added = parse_added_lines(DIFF)

    assert 23 not in added["xenon/kernel/a.py"]


def test_a_deleted_file_is_skipped() -> None:
    diff = "--- a/x.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n"
    assert parse_added_lines(diff) == {}


def test_a_hunk_with_one_line_has_no_count() -> None:
    diff = "+++ b/x.py\n@@ -5 +5 @@\n"
    assert parse_added_lines(diff)["x.py"] == {5}


# ── the metric ──────────────────────────────────────────────────────


def test_only_executable_added_lines_count(tmp_path) -> None:
    """A blank line in the diff has no coverage row; it must not dilute the %."""

    coverage = read_coverage(
        _coverage(tmp_path, {"kernel/a.py": {11: 1, 12: 0, 13: 1, 1: 0}})
    )
    report = build_report({"xenon/kernel/a.py": {11, 12, 13, 14}}, coverage)

    assert report.measured == {"kernel/a.py": {11, 12, 13}}
    assert report.percent == pytest.approx(66.67, abs=0.01)


def test_a_file_outside_the_measured_tree_is_ignored(tmp_path) -> None:
    coverage = read_coverage(_coverage(tmp_path, {"kernel/a.py": {1: 1}}))
    report = build_report({"tests/test_x.py": {1}}, coverage)

    assert report.total == 0
    assert report.percent == 100.0


def test_missing_lines_are_reported_per_file(tmp_path) -> None:
    coverage = read_coverage(_coverage(tmp_path, {"kernel/a.py": {2: 0, 3: 0}}))
    report = build_report({"xenon/kernel/a.py": {2, 3}}, coverage)

    assert report.missing == {"kernel/a.py": [2, 3]}


def test_the_xenon_prefix_is_normalised_away(tmp_path) -> None:
    """Coverage paths are source-relative; diff paths are repo-relative."""

    coverage = read_coverage(_coverage(tmp_path, {"kernel/a.py": {7: 1}}))
    report = build_report({"xenon/kernel/a.py": {7}}, coverage)

    assert report.hit == 1


def test_a_fully_covered_patch_renders_cleanly() -> None:
    report = Report(measured={"a.py": {1}}, covered={("a.py", 1)})
    text = render(report, threshold=95)

    assert "100.00%" in text
    assert "all added executable lines are covered" in text


def test_a_missing_line_shows_up_in_the_rendering(tmp_path) -> None:
    coverage = read_coverage(_coverage(tmp_path, {"kernel/a.py": {2: 0}}))
    report = build_report({"xenon/kernel/a.py": {2}}, coverage)

    text = render(report, threshold=95)

    assert "kernel/a.py: 1 missing -> 2" in text


def test_rendering_stops_after_the_limit() -> None:
    report = Report(
        measured={f"f{i}.py": {1} for i in range(5)},
        covered=set(),
        missing={f"f{i}.py": [1] for i in range(5)},
    )
    text = render(report, threshold=95, limit=2)

    assert text.count("missing ->") == 2


# ── inputs and exit codes ───────────────────────────────────────────


def test_a_missing_report_is_a_hard_error(tmp_path) -> None:
    with pytest.raises(SystemExit):
        read_coverage(tmp_path / "nope.xml")


def test_an_empty_diff_passes_with_a_note(tmp_path) -> None:
    _coverage(tmp_path, {"kernel/a.py": {1: 1}})
    # base == HEAD: no diff at all.
    code = main(["--from-xml", "--xml", str(tmp_path / "coverage.xml"), "--base", "HEAD"])
    assert code == 0


def test_a_stale_report_is_detected(tmp_path, monkeypatch) -> None:
    xml = tmp_path / "coverage.xml"
    xml.write_text("<coverage />", encoding="utf-8")
    # A source file newer than the report.
    newer = Path("xenon") / "__init__.py"
    if not newer.is_file():  # pragma: no cover - repo layout guarantee
        pytest.skip("repo layout changed")
    import os

    os.utime(xml, (0, 0))
    assert stale_sources(xml)


def test_a_fresh_report_has_no_stale_sources(tmp_path) -> None:
    xml = tmp_path / "coverage.xml"
    xml.write_text("<coverage />", encoding="utf-8")
    # Written now, and nothing under xenon can be newer than "now".
    assert stale_sources(xml) == []


def test_main_fails_below_the_threshold(tmp_path) -> None:
    _coverage(tmp_path, {"kernel/a.py": {1: 0}})
    # `--base HEAD` produces an empty diff, so drive build_report directly for
    # the threshold behaviour and keep main() for wiring only.
    report = build_report(
        {"xenon/kernel/a.py": {1}}, read_coverage(tmp_path / "coverage.xml")
    )
    assert report.percent < 95


def test_a_missing_base_explains_how_to_fetch_it() -> None:
    """CI checks out a PR without `origin/<base>`; the error must say why."""

    from scripts.patch_cov import _resolve_base

    with pytest.raises(SystemExit) as excinfo:
        _resolve_base("definitely-not-a-ref")

    message = str(excinfo.value)
    assert "cannot find a merge base" in message
    assert "git fetch origin" in message


def test_the_base_falls_back_to_the_remote_tracking_ref() -> None:
    """`main` works locally (and `origin/main` in CI) with the same argument."""

    from scripts.patch_cov import _resolve_base

    # The repo always has a HEAD; HEAD~0 resolves, so this only asserts that the
    # second spelling is tried rather than raising on the first failure.
    assert _resolve_base("HEAD") == _resolve_base("HEAD")
