from __future__ import annotations

import subprocess

from xenon.engine.execution_evidence import ExecutionEvidence
from xenon.engine.tool_tracker import ToolExecutionTracker


def test_evidence_separates_mutations_tests_and_failures():
    tracker = ToolExecutionTracker()
    tracker.record("edit_file", {"file_path": "pkg/a.py"}, True, "edited")
    tracker.record("command", {"action": "pytest -q tests/test_a.py"}, True, "passed")
    tracker.record("read_file", {"file_path": "missing.py"}, False, "", "missing")

    evidence = ExecutionEvidence.capture(tracker)

    assert evidence.changed_files == {"pkg/a.py"}
    assert evidence.successful_tests == ["pytest -q tests/test_a.py"]
    assert evidence.implementation_verified is True
    assert [call.tool_name for call in evidence.failed_calls] == ["read_file"]
    rendered = evidence.render()
    assert "2/3 成功" in rendered
    assert "missing" in rendered


def test_python_module_and_virtualenv_test_commands_are_recognized():
    for command in ("python -m pytest -q", ".venv/bin/pytest tests -q"):
        tracker = ToolExecutionTracker()
        tracker.record("command", {"action": command}, True, "passed")
        assert ExecutionEvidence.capture(tracker).successful_tests == [command]


def test_evidence_reads_real_worktree_diff(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=tmp_path, check=True
    )
    target = tmp_path / "value.txt"
    target.write_text("old\n")
    subprocess.run(["git", "add", "value.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    target.write_text("new\n")

    evidence = ExecutionEvidence.capture(ToolExecutionTracker(), tmp_path)

    assert "value.txt" in evidence.workspace_status
    assert "-old" in evidence.workspace_diff
    assert "+new" in evidence.workspace_diff
