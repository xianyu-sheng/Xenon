"""The eval runner drives the shipped kernel loop (#202)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.kernel_runner import (
    CODE_EDITING_EVAL_NAMES,
    EVAL_PROFILES,
    run_eval_task,
)
from xenon.harness.llm import ModelConfig
from xenon.kernel.policies import PermissionMode
from xenon.kernel.strategies import LoopProfile


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    return repo


class FakeClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    def __call__(self, model_id, messages, **kwargs):
        return self.responses.pop(0)


def test_every_legacy_eval_name_maps_to_a_loop_profile() -> None:
    assert set(EVAL_PROFILES) >= {
        "react",
        "plan-execute",
        "reflection",
        "plan-react",
        "plan-reflection",
        "react-reflection",
        "kernel",
    }
    assert all(isinstance(profile, LoopProfile) for profile in EVAL_PROFILES.values())
    assert "direct" not in CODE_EDITING_EVAL_NAMES


def test_eval_run_uses_the_kernel_and_records_a_trace(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    client = FakeClient(
        [
            json.dumps(
                {
                    "action": "call_tool",
                    "capability": "edit",
                    "params": {
                        "files": [
                            {
                                "path": "calc.py",
                                "content": "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n",
                            }
                        ]
                    },
                }
            ),
            json.dumps(
                {
                    "action": "finish",
                    "answer": "added sub",
                    "claims": [
                        {"id": "c1", "kind": "file_changed", "target": "calc.py"}
                    ],
                }
            ),
        ]
    )

    result = run_eval_task(
        objective="add sub() to calc.py",
        worktree=repo,
        model=ModelConfig(model_id="test/fake"),
        name="react",
        permission=PermissionMode.AUTO_EDIT,
        task_id="eval-unit-1",
        ledger_dir=tmp_path / "ledger",
        client=client,
    )
    assert result.ledger_path.exists()
    assert result.accepted is True
    assert result.answer
    assert [item["tool"] for item in result.tool_trace] == ["apply_patch"]
    written = (repo / "calc.py").read_text(encoding="utf-8")
    assert "def sub(a, b):" in written
    assert result.finalize_sha is None, "eval 不能产生交付提交"


def test_unknown_eval_profile_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(KeyError, match="unknown eval profile"):
        run_eval_task(
            objective="x",
            worktree=_repo(tmp_path),
            model=ModelConfig(model_id="test/fake"),
            name="not-a-profile",
        )


def test_finalize_is_disabled_so_git_diff_is_the_patch(tmp_path: Path) -> None:
    """Evals grade the working tree; the finalizer must not create a commit."""

    import inspect

    source = inspect.getsource(run_eval_task)
    assert "finalize=False" in source
