"""Structured plan contracts should not depend on one language's prose."""

from __future__ import annotations

from xenon.engine.plan_contract import (
    infer_step_kind,
    normalize_step_contract,
    step_requires_write,
    step_verify_cmd,
)


def test_normalize_infers_edit_and_verify_contracts() -> None:
    edit = normalize_step_contract(
        {"id": 1, "task": "write the source", "tool": "write_file", "params": {}}
    )
    verify = normalize_step_contract(
        {
            "id": 2,
            "task": "run the target test",
            "tool": "command",
            "params": {"command": "python -m pytest tests/test_x.py -q"},
        }
    )

    assert edit["kind"] == "edit"
    assert step_requires_write(edit) is True
    assert verify["kind"] == "verify"
    assert step_verify_cmd(verify) == "python -m pytest tests/test_x.py -q"


def test_explicit_contract_wins_and_targets_are_preserved() -> None:
    step = normalize_step_contract(
        {
            "id": 3,
            "task": "do the work",
            "kind": "verify",
            "requires_write": False,
            "verify_cmd": "pytest -q",
            "target_paths": ["src/app.py"],
        }
    )

    assert step["kind"] == "verify"
    assert step_requires_write(step) is False
    assert step["target_paths"] == ["src/app.py"]


def test_commit_and_clean_are_not_confused_with_verification() -> None:
    assert infer_step_kind({"task": "git commit -m done", "tool": "git"}) == "commit"
    assert infer_step_kind({"task": "确认工作区干净", "tool": "command"}) == "clean"
