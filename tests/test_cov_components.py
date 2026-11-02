"""The component targets must be reproducible, not aspirational."""

from __future__ import annotations

from pathlib import Path

from scripts.cov_components import COMPONENTS, TARGETS, measure, render


def _report(tmp_path: Path, files: dict[str, list[int]]) -> Path:
    """Write a minimal coverage.xml: filename -> [hits per line]."""

    classes = []
    for name, hits in files.items():
        lines = "".join(
            f'<line number="{i}" hits="{h}" />' for i, h in enumerate(hits, 1)
        )
        classes.append(
            f'<class filename="{name}"><lines>{lines}</lines></class>'
        )
    xml = (
        '<?xml version="1.0" ?><coverage><packages><package><classes>'
        + "".join(classes)
        + "</classes></package></packages></coverage>"
    )
    path = tmp_path / "coverage.xml"
    path.write_text(xml, encoding="utf-8")
    return path


def test_lines_are_grouped_by_component(tmp_path: Path) -> None:
    path = _report(
        tmp_path,
        {
            "kernel/runtime.py": [1, 1, 1, 1],
            "repl/repl.py": [1, 0, 0, 0],
            "engine/task_state.py": [1, 1, 0, 0],
            "main.py": [0, 0],
        },
    )

    buckets, total, missed = measure(path)

    assert buckets["engine-core"] == [4, 0]
    assert buckets["ui-repl"] == [4, 3]
    assert buckets["legacy-organs"] == [4, 2]
    assert buckets["other"] == [2, 2]
    assert total == 14 and missed == 7


def test_the_render_names_each_component_and_its_target(tmp_path: Path) -> None:
    path = _report(tmp_path, {"kernel/runtime.py": [1, 1], "repl/repl.py": [0, 0]})
    buckets, total, missed = measure(path)

    text = render(buckets, total, missed)

    assert "engine-core" in text and "90%" in text
    assert "ui-repl" in text and "70%" in text
    assert "TOTAL" in text


def test_a_component_below_its_target_is_flagged(tmp_path: Path) -> None:
    # engine-core at 50% must not read OK against a 90% target.
    path = _report(tmp_path, {"kernel/runtime.py": [1, 0]})
    buckets, total, missed = measure(path)

    text = render(buckets, total, missed)

    assert "BELOW" in text


def test_the_targets_match_the_codecov_config() -> None:
    """A target in this script that is missing from codecov.yml is a lie."""

    config = Path("codecov.yml")
    if not config.is_file():  # pragma: no cover - repo layout guarantee
        return
    text = config.read_text(encoding="utf-8")

    for name in COMPONENTS:
        # The script's key is the component's *name* in codecov.yml; the id is an
        # internal slug and is allowed to differ.
        assert f"name: {name}" in text, f"{name} not declared in codecov.yml"
    for name, target in TARGETS.items():
        assert f"target: {target:.0f}%" in text, f"{name} target {target} missing"
