"""The docs-reference guard must actually fail on a stale path."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_docs_refs.py"


def _run() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, cwd=ROOT
    )


def test_the_repository_docs_are_consistent():
    result = _run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "guard OK" in result.stdout


def test_the_guard_fails_on_a_missing_module():
    doc = ROOT / "docs" / "OBSERVABILITY.md"
    original = doc.read_text(encoding="utf-8")
    try:
        doc.write_text(
            original + "\n`xenon/engine/react_engine.py`\n", encoding="utf-8"
        )
        result = _run()
        assert result.returncode == 1
        assert "react_engine.py" in result.stdout
    finally:
        doc.write_text(original, encoding="utf-8")


def test_an_archival_document_may_describe_the_past():
    """The marker is the only escape hatch, and it must work."""

    doc = ROOT / "docs" / "EVAL_RESULTS.md"
    text = doc.read_text(encoding="utf-8")

    assert "历史文档（归档）" in text
    assert _run().returncode == 0
