"""ActFirst natural-language path (#220)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.harness.test_exec import _plan_dict, _repo
from xenon.harness import llm
from xenon.harness.cli import _parser, run_from_args
from xenon.harness.llm import (
    ActionParseError,
    LLMActionProposer,
    ModelConfig,
    ModelConfigError,
    build_llm_strategy,
    build_messages,
    parse_action,
    resolve_model_config,
)
from xenon.harness.runner import run_task
from xenon.kernel.contracts import (
    Abort,
    AskUser,
    CallTool,
    Finish,
    ModelView,
    TaskSpec,
    Think,
)
from xenon.ledger.store import Ledger


class FakeClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, list[dict[str, str]], dict]] = []

    def __call__(self, model_id, messages, **kwargs):
        self.calls.append((model_id, messages, kwargs))
        return self.responses.pop(0)


def _responses(*, invalid_first: bool = False) -> list[str]:
    plan = _plan_dict()
    files = plan["steps"][0]["params"]["files"]
    responses = [
        json.dumps(
            {
                "action": "call_tool",
                "capability": "edit",
                "params": {"files": files},
            }
        ),
        json.dumps(
            {
                "action": "call_tool",
                "capability": "verify",
                "params": {"command": "python -m pytest tests/test_add.py -q"},
            }
        ),
        json.dumps(
            {
                "action": "finish",
                "answer": "added add() and its test",
                "claims": [
                    {
                        "id": "c1",
                        "kind": "file_changed",
                        "target": "calc/add.py",
                    },
                    {
                        "id": "c2",
                        "kind": "test_passed",
                        "target": "tests/test_add.py",
                    },
                ],
            }
        ),
    ]
    if invalid_first:
        responses.insert(0, "this is not an action")
    return responses


def test_parse_action_supports_capability_protocol() -> None:
    action = parse_action(
        '```json\n{"action":"call_tool","capability":"read",'
        '"params":{"file_path":"a.py"},"targets":"a.py"}\n```'
    )
    assert isinstance(action, CallTool)
    assert action.intent.capability.value == "read"
    assert action.intent.params == {"file_path": "a.py"}
    assert action.intent.targets == ("a.py",)

    assert isinstance(parse_action('{"action":"think","text":"hmm"}'), Think)
    assert isinstance(
        parse_action('{"action":"ask_user","question":"which branch?"}'),
        AskUser,
    )
    assert isinstance(parse_action('{"action":"abort","reason":"nope"}'), Abort)
    finish = parse_action(
        '{"action":"finish","answer":"done","claims":[{"kind":"file_changed",'
        '"target":"a.py"}]}'
    )
    assert isinstance(finish, Finish)
    assert finish.claims[0].id == "claim-1"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("", "empty model response"),
        ("[]", "object"),
        ("not json", "not JSON"),
        ('{"action":"nope"}', "unsupported action"),
        ('{"action":"think"}', "think.text"),
        ('{"action":"call_tool","capability":"nope"}', "unknown capability"),
        ('{"action":"call_tool","capability":"read","params":[]}', "params"),
        ('{"action":"call_tool","capability":"vcs"}', "vcs capability"),
        (
            '{"action":"call_tool","capability":"shell","params":'
            '{"command":"git commit -m x"}}',
            "git commit/add",
        ),
        ('{"action":"ask_user"}', "ask_user.question"),
        ('{"action":"finish","claims":{}}', "finish.claims"),
        ('{"action":"finish","claims":[{"target":"a.py"}]}', "kind"),
        (
            '{"action":"finish","claims":[{"kind":"not_a_kind"}]}',
            "unknown claim kind",
        ),
    ],
)
def test_parse_action_fails_closed(payload: str, message: str) -> None:
    with pytest.raises(ActionParseError, match=message):
        parse_action(payload)


def test_build_messages_includes_state_and_protocol() -> None:
    spec = TaskSpec(task_id="t", objective="fix it", workspace="/workspace")
    view = ModelView(
        task=spec,
        system="RUNTIME SYSTEM",
        tools=(),
        working_set=(),
        debt=(),
        budget=spec.budget,
        input_seq=0,
        hash="h",
    )
    messages = build_messages(view)
    assert messages[0]["role"] == "system"
    assert "RUNTIME SYSTEM" in messages[0]["content"]
    assert "Do not run git commit" in messages[0]["content"]
    assert "fix it" in messages[1]["content"]


def test_llm_strategy_runs_real_workspace_and_finalizer(tmp_path: Path) -> None:
    _repo(tmp_path)
    client = FakeClient(_responses())
    config = ModelConfig(model_id="test/fake", max_tokens=1234)
    run = run_task(
        "add a function and test it",
        strategy=build_llm_strategy(config, client=client),
        workspace=tmp_path,
        ledger_dir=tmp_path.parent / f"{tmp_path.name}-ledger",
        task_id="llm-1",
        constraints=("model:test/fake",),
    )

    assert run.result.accepted is True, run.result.debt
    assert run.result.finalize_sha
    assert len(client.calls) == 3
    assert client.calls[0][2]["max_tokens"] == 1234
    assert any(
        event.kind.value == "tool_receipt"
        for event in Ledger(run.ledger_path, "llm-1").read()
    )
    assert Ledger(run.ledger_path, "llm-1").verify().valid
    assert (tmp_path / "calc" / "add.py").exists()
    assert (tmp_path / "tests" / "test_add.py").exists()


def test_invalid_model_turn_becomes_thought_and_retries(tmp_path: Path) -> None:
    _repo(tmp_path)
    client = FakeClient(_responses(invalid_first=True))
    config = ModelConfig(model_id="test/fake")
    run = run_task(
        "add a function and test it",
        strategy=build_llm_strategy(config, client=client),
        workspace=tmp_path,
        ledger_dir=tmp_path.parent / f"{tmp_path.name}-ledger",
        task_id="llm-retry",
    )

    assert run.result.accepted is True
    events = Ledger(run.ledger_path, "llm-retry").read()
    thoughts = [event for event in events if event.kind.value == "thought"]
    assert thoughts
    assert "Invalid ActFirst action" in thoughts[0].payload["text"]


def test_resolve_model_from_models_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "models.yaml"
    config_path.write_text(
        "models:\n"
        "  coder:\n"
        "    model_id: test/model\n"
        "    max_tokens: 4321\n"
        "    temperature: 0.1\n"
        "    base_url: http://localhost:1/v1\n"
        "roles:\n"
        "  planner: [coder]\n",
        encoding="utf-8",
    )
    assert resolve_model_config(config_path=config_path).model_id == "test/model"
    resolved = resolve_model_config(["coder"], config_path=config_path)
    assert resolved.max_tokens == 4321
    assert resolved.temperature == 0.1
    assert resolve_model_config(["test/other"], config_path=config_path).model_id == "test/other"
    with pytest.raises(ModelConfigError, match="unknown model"):
        resolve_model_config(["missing"], config_path=config_path)


def test_resolve_model_from_credentials_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    credentials = tmp_path / "credentials.yaml"
    credentials.write_text(
        "providers:\n"
        "  test:\n"
        "    api_key: secret\n"
        "    base_url: http://localhost:1/v1\n"
        "    models: [fake]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(llm, "_DEFAULT_MODELS_PATH", tmp_path / "missing.yaml")
    monkeypatch.setattr(llm, "_DEFAULT_CREDENTIALS_PATH", credentials)
    resolved = resolve_model_config()
    assert resolved.model_id == "test/fake"
    assert resolved.api_key == "secret"
    assert resolved.base_url == "http://localhost:1/v1"


def test_resolve_model_requires_configuration(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(llm, "_DEFAULT_MODELS_PATH", tmp_path / "missing.yaml")
    monkeypatch.setattr(
        llm, "_DEFAULT_CREDENTIALS_PATH", tmp_path / "missing-credentials.yaml"
    )
    with pytest.raises(ModelConfigError, match="no model configured"):
        resolve_model_config()


def test_cli_natural_language_path(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo(repo)
    client = FakeClient(_responses())
    monkeypatch.setattr(llm, "chat_completion", client)
    output = tmp_path / "out" / "result.json"
    args = _parser().parse_args(
        [
            "add a function and test it",
            "--workspace",
            str(repo),
            "--ledger-dir",
            str(tmp_path / "ledger"),
            "--task-id",
            "cli-llm",
            "--model",
            "test/fake",
            "--no-jsonl",
            "--output",
            str(output),
        ]
    )
    assert run_from_args(args) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["accepted"] is True
    assert payload["finalize_sha"]


def test_cli_requires_model_configuration(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(llm, "_DEFAULT_MODELS_PATH", tmp_path / "missing.yaml")
    monkeypatch.setattr(
        llm, "_DEFAULT_CREDENTIALS_PATH", tmp_path / "missing-credentials.yaml"
    )
    args = _parser().parse_args(["do work", "--workspace", str(tmp_path)])
    assert run_from_args(args) == 2


def test_main_exec_routes_positional_task(tmp_path: Path, monkeypatch) -> None:
    from xenon import main as xenon_main

    monkeypatch.setattr(
        "sys.argv",
        [
            "xenon",
            "exec",
            "do work",
            "--config",
            str(tmp_path / "missing.yaml"),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        xenon_main.cli()
    assert exc.value.code == 2


def test_main_exec_rejects_plan_plus_task(tmp_path: Path, monkeypatch) -> None:
    from xenon import main as xenon_main

    monkeypatch.setattr(
        "sys.argv",
        ["xenon", "exec", "do work", "--plan", str(tmp_path / "plan.json")],
    )
    with pytest.raises(SystemExit) as exc:
        xenon_main.cli()
    assert exc.value.code == 2


def test_cli_rejects_plan_plus_task(tmp_path: Path) -> None:
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(_plan_dict()), encoding="utf-8")
    args = _parser().parse_args(["do work", "--plan", str(plan)])
    assert run_from_args(args) == 2


def test_model_config_error_paths(tmp_path: Path, monkeypatch) -> None:
    missing = tmp_path / "missing.yaml"
    with pytest.raises(ModelConfigError, match="not found"):
        resolve_model_config(config_path=missing)

    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("models: [", encoding="utf-8")
    with pytest.raises(ModelConfigError, match="cannot read model config"):
        resolve_model_config(config_path=malformed)

    not_mapping = tmp_path / "list.yaml"
    not_mapping.write_text("- not-a-mapping\n", encoding="utf-8")
    with pytest.raises(ModelConfigError, match="must be an object"):
        resolve_model_config(config_path=not_mapping)


def test_model_entry_validation_and_role_selection(tmp_path: Path) -> None:
    config_path = tmp_path / "models.yaml"
    config_path.write_text(
        "models:\n"
        "  bad:\n"
        "    model_id: test/bad\n"
        "    max_tokens: nope\n"
        "    temperature: nope\n"
        "  coder:\n"
        "    model_id: test/coder\n"
        "roles:\n"
        "  planner: [missing, coder]\n",
        encoding="utf-8",
    )
    resolved = resolve_model_config(config_path=config_path)
    assert resolved.model_id == "test/coder"
    bad = resolve_model_config(["bad"], config_path=config_path)
    assert bad.max_tokens == 8192
    assert bad.temperature == 0.0
    # Case-insensitive alias/model-id matching.
    assert resolve_model_config(["CODER"], config_path=config_path).model_id == "test/coder"


def test_credentials_fallback_skips_unusable_providers(
    tmp_path: Path, monkeypatch
) -> None:
    credentials = tmp_path / "credentials.yaml"
    credentials.write_text(
        "providers:\n"
        "  empty:\n"
        "    api_key: x\n"
        "  broken: nope\n"
        "  good:\n"
        "    models: [test/fake]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(llm, "_DEFAULT_MODELS_PATH", tmp_path / "missing.yaml")
    monkeypatch.setattr(llm, "_DEFAULT_CREDENTIALS_PATH", credentials)
    assert resolve_model_config().model_id == "test/fake"


def test_proposer_passes_transport_options() -> None:
    client = FakeClient(['{"action":"think","text":"ok"}'])
    proposer = LLMActionProposer(
        ModelConfig(
            model_id="test/fake",
            base_url="http://localhost:1/v1",
            api_key="secret",
            reasoning_effort="low",
            max_tokens=99,
            temperature=0.2,
        ),
        client=client,
    )
    spec = TaskSpec(task_id="t", objective="x", workspace="/w")
    view = ModelView(
        task=spec,
        system="S",
        tools=(),
        working_set=(),
        debt=(),
        budget=spec.budget,
        input_seq=0,
        hash="h",
    )
    assert isinstance(proposer(view), Think)
    _, _, kwargs = client.calls[0]
    assert kwargs["base_url"] == "http://localhost:1/v1"
    assert kwargs["reasoning_effort"] == "low"
    assert kwargs["credentials"] == {"test": "secret"}
    assert kwargs["max_tokens"] == 99
    assert kwargs["temperature"] == 0.2


def test_cli_requires_plan_or_task() -> None:
    args = _parser().parse_args([])
    assert run_from_args(args) == 2
