"""Coverage/error-path tests for the headless harness (#203)."""

from __future__ import annotations

import io
import json
import runpy
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from tests.harness.test_exec import _plan_dict, _repo
from xenon.harness import runner
from xenon.harness.cli import run_from_args
from xenon.harness.plan import PlanError, parse_plan
from xenon.harness.runner import result_json, write_result
from xenon.harness.sink import JsonlSink
from xenon.ledger.store import IntegrityReport, Ledger


def _base_plan() -> dict:
    return {
        "objective": "x",
        "steps": [
            {
                "id": 1,
                "kind": "think",
                "expects": [],
            }
        ],
    }


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (None, "plan must be an object"),
        ({"steps": []}, "objective is required"),
        ({"objective": "x", "steps": "bad"}, "steps must be a list"),
        ({"objective": "x", "steps": [None]}, "must be an object"),
        (
            {"objective": "x", "steps": [{"id": "1", "kind": "think"}]},
            "id must be an integer",
        ),
        (
            {"objective": "x", "steps": [{"id": 1, "kind": "nope"}]},
            "unknown step kind",
        ),
        (
            {
                "objective": "x",
                "steps": [
                    {
                        "id": 1,
                        "kind": "edit",
                        "capability": "nope",
                    }
                ],
            },
            "unknown capability",
        ),
        (
            {
                "objective": "x",
                "steps": [
                    {
                        "id": 1,
                        "kind": "edit",
                        "capability": "edit",
                        "params": "bad",
                    }
                ],
            },
            "params must be an object",
        ),
        (
            {
                "objective": "x",
                "steps": [
                    {
                        "id": 1,
                        "kind": "edit",
                        "capability": "edit",
                        "expects": [{"kind": "file_changed"}],
                    }
                ],
            },
            "id is required",
        ),
        (
            {
                "objective": "x",
                "steps": [
                    {
                        "id": 1,
                        "kind": "edit",
                        "capability": "edit",
                        "expects": [{"id": "e", "kind": "nope"}],
                    }
                ],
            },
            "unknown expectation kind",
        ),
        (
            {
                "objective": "x",
                "steps": [
                    {
                        "id": 1,
                        "kind": "edit",
                        "capability": "edit",
                        "expects": [{"id": "e", "kind": "file_changed", "expect": "bad"}],
                    }
                ],
            },
            "expect must be an object",
        ),
    ],
)
def test_invalid_plans_raise_plan_error(value, message: str) -> None:
    with pytest.raises(PlanError, match=message):
        parse_plan(value)


def test_empty_steps_are_rejected() -> None:
    with pytest.raises(PlanError, match="steps must not be empty"):
        parse_plan({"objective": "x", "steps": []})


def test_valid_plan_without_intent() -> None:
    plan = parse_plan(_base_plan())
    assert plan.proposal.steps[0].intent is None


def test_sink_writes_jsonl_and_defaults_to_stdout(capsys) -> None:
    class Event:
        @staticmethod
        def to_dict() -> dict:
            return {"seq": 1, "kind": "thought"}

    stream = io.StringIO()
    JsonlSink(stream).emit(Event())
    assert json.loads(stream.getvalue()) == {"seq": 1, "kind": "thought"}

    JsonlSink().emit(Event())
    assert json.loads(capsys.readouterr().out) == {
        "seq": 1,
        "kind": "thought",
    }


def test_result_json_and_write_result_round_trip(tmp_path: Path) -> None:
    _repo(tmp_path)
    plan = parse_plan(_plan_dict())
    run = runner.run_plan(
        plan,
        workspace=tmp_path,
        ledger_dir=tmp_path.parent / f"{tmp_path.name}-ledger",
        task_id="result-json",
    )
    payload = json.loads(result_json(run.result))
    assert payload["accepted"] is True
    stream = io.StringIO()
    write_result(run.result, stream)
    assert json.loads(stream.getvalue())["task_id"] == "result-json"


def test_run_plan_rejects_non_directory_workspace(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a directory"):
        runner.run_plan(
            parse_plan(_base_plan()),
            workspace=tmp_path / "missing",
        )


def test_run_plan_rejects_invalid_ledger_after_run(
    tmp_path: Path, monkeypatch
) -> None:
    _repo(tmp_path)
    monkeypatch.setattr(
        Ledger,
        "verify",
        lambda self: IntegrityReport(valid=False, reason="broken"),
    )
    with pytest.raises(RuntimeError, match="ledger verification failed"):
        runner.run_plan(
            parse_plan(_plan_dict()),
            workspace=tmp_path,
            ledger_dir=tmp_path.parent / f"{tmp_path.name}-ledger",
            task_id="broken-ledger",
        )


def _args(tmp_path: Path, **overrides) -> Namespace:
    base = {
        "plan": str(tmp_path / "plan.json"),
        "workspace": ".",
        "ledger_dir": str(tmp_path.parent / f"{tmp_path.name}-ledger"),
        "task_id": "cli-inproc",
        "permission": "auto-edit",
        "jsonl": False,
        "output": None,
        "no_finalize": False,
        "max_steps": 20,
    }
    base.update(overrides)
    return Namespace(**base)


def test_cli_in_process_success_without_jsonl(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo(repo)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_plan_dict()), encoding="utf-8")
    output = tmp_path / "out" / "result.json"
    args = _args(
        tmp_path,
        workspace=str(repo),
        output=str(output),
    )
    assert run_from_args(args) == 0
    assert output.exists()


def test_cli_plan_error_and_harness_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    assert run_from_args(_args(tmp_path, plan=str(bad))) == 2

    not_object = tmp_path / "list.json"
    not_object.write_text("[]", encoding="utf-8")
    assert run_from_args(_args(tmp_path, plan=str(not_object))) == 2

    good = tmp_path / "good.json"
    good.write_text(json.dumps(_base_plan()), encoding="utf-8")
    assert (
        run_from_args(
            _args(
                tmp_path,
                plan=str(good),
                workspace=str(tmp_path / "missing"),
            )
        )
        == 3
    )


def test_harness_module_entrypoint(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["xenon-harness", "--help"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("xenon.harness", run_name="__main__", alter_sys=True)
    assert exc.value.code == 0


def test_xenon_main_exec_without_plan(monkeypatch) -> None:
    from xenon import main as xenon_main

    monkeypatch.setattr(sys, "argv", ["xenon", "exec"])
    with pytest.raises(SystemExit) as exc:
        xenon_main.cli()
    assert exc.value.code == 2


def test_xenon_main_exec_missing_plan(monkeypatch) -> None:
    from xenon import main as xenon_main

    monkeypatch.setattr(
        sys,
        "argv",
        ["xenon", "exec", "--plan", "/definitely/missing.json"],
    )
    with pytest.raises(SystemExit) as exc:
        xenon_main.cli()
    assert exc.value.code == 2
