"""ActFirst natural-language path (#220)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tests.harness.test_exec import _plan_dict, _repo
from xenon.harness import llm
from xenon.harness.cli import _parser, run_from_args
from xenon.harness.llm import (
    ActionParseError,
    LLMActionProposer,
    LLMPlanFinisher,
    LLMPlanReviewer,
    LLMReflector,
    LLMStepReviewer,
    ModelConfig,
    ModelConfigError,
    build_llm_plan_strategy,
    build_llm_reflection_strategy,
    build_llm_strategy,
    build_messages,
    build_plan_messages,
    parse_action,
    resolve_model_config,
)
from xenon.harness.plan import parse_plan
from xenon.harness.runner import resume_task, run_task
from xenon.kernel.contracts import (
    Abort,
    AskUser,
    CallTool,
    Capability,
    Finish,
    ModelView,
    TaskMode,
    TaskSpec,
    Think,
)
from xenon.ledger.events import EventKind, canonical_json
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


class _Approval:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.calls = 0

    def request(self, view, action, decision) -> bool:
        self.calls += 1
        return self.allowed


class _TwoActions:
    def __init__(self, actions) -> None:
        self.actions = list(actions)

    def propose(self, view):
        return self.actions.pop(0)


def test_run_task_approval_allows_shell_once(tmp_path: Path) -> None:
    from xenon.kernel.contracts import CallTool, Capability, Finish, ToolIntent

    approval = _Approval(True)
    run = run_task(
        "run a shell command",
        strategy=_TwoActions(
            [
                CallTool(
                    ToolIntent(
                        capability=Capability.SHELL,
                        params={"command": "echo approved"},
                    )
                ),
                Finish("done"),
            ]
        ),
        workspace=tmp_path,
        ledger_dir=tmp_path.parent / f"{tmp_path.name}-ledger",
        task_id="approval-shell",
        finalize=False,
        approval=approval,
        allow_approval=True,
    )
    assert approval.calls == 1
    assert run.result.accepted is True, run.result.debt
    events = Ledger(run.ledger_path, "approval-shell").read()
    assert any(event.kind.value == "user_approval" for event in events)
    assert any(event.kind.value == "tool_receipt" for event in events)


def test_llm_plan_strategy_executes_a_capability_plan(tmp_path: Path) -> None:
    _repo(tmp_path)
    client = FakeClient([
        json.dumps(_plan_dict()),
        json.dumps({"action": "finish", "answer": "added add() and its test"}),
    ])
    run = run_task(
        "add a function and test it",
        strategy=build_llm_plan_strategy(
            ModelConfig(model_id="test/fake"), client=client
        ),
        workspace=tmp_path,
        ledger_dir=tmp_path.parent / f"{tmp_path.name}-ledger",
        task_id="plan-llm-1",
        finalize=True,
    )

    assert run.result.accepted is True, run.result.debt
    assert run.result.finalize_sha
    assert run.result.answer == "added add() and its test"
    assert len(client.calls) == 2
    events = Ledger(run.ledger_path, "plan-llm-1").read()
    assert any(event.kind.value == "plan_accepted" for event in events)
    assert any(event.kind.value == "tool_receipt" for event in events)


def test_resume_task_executes_an_approved_plan_in_the_same_ledger(
    tmp_path: Path,
) -> None:
    _repo(tmp_path)
    plan = parse_plan(_plan_dict())
    ledger_dir = tmp_path.parent / f"{tmp_path.name}-resume-ledger"
    planned = run_task(
        plan.objective,
        strategy=build_llm_plan_strategy(
            ModelConfig(model_id="test/fake"),
            client=FakeClient([json.dumps(_plan_dict())]),
        ),
        workspace=tmp_path,
        ledger_dir=ledger_dir,
        task_id="resume-plan-1",
        finalize=False,
        mode=TaskMode.PLAN,
    )
    assert planned.result.phase.value == "planned"
    accepted = Ledger(planned.ledger_path, "resume-plan-1").read(
        EventKind.PLAN_ACCEPTED
    )[-1]
    digest = hashlib.sha256(
        canonical_json(accepted.payload["plan"]).encode("utf-8")
    ).hexdigest()

    resumed = resume_task(
        task_id="resume-plan-1",
        plan=plan.proposal,
        plan_digest=digest,
        approval_id="approval-1",
        strategy=build_llm_plan_strategy(
            ModelConfig(model_id="test/fake"),
            client=FakeClient(
                [json.dumps({"action": "finish", "answer": "done"})]
            ),
            resumed=True,
        ),
        objective=plan.objective,
        workspace=tmp_path,
        ledger_dir=ledger_dir,
        finalize=False,
    )

    assert resumed.result.accepted is True, resumed.result.debt
    events = Ledger(resumed.ledger_path, "resume-plan-1").read()
    assert len([e for e in events if e.kind is EventKind.TASK_STARTED]) == 1
    assert len([e for e in events if e.kind is EventKind.PLAN_ACCEPTED]) == 1
    assert any(e.kind is EventKind.PLAN_APPROVED for e in events)
    assert any(e.kind is EventKind.TOOL_RECEIPT for e in events)


def test_llm_plan_invalid_json_fails_closed(tmp_path: Path) -> None:
    _repo(tmp_path)
    client = FakeClient(['{"steps":[]}'])
    run = run_task(
        "add a function and test it",
        strategy=build_llm_plan_strategy(
            ModelConfig(model_id="test/fake"), client=client
        ),
        workspace=tmp_path,
        ledger_dir=tmp_path.parent / f"{tmp_path.name}-ledger",
        task_id="plan-llm-bad",
        finalize=False,
    )

    assert run.result.accepted is False
    events = Ledger(run.ledger_path, "plan-llm-bad").read()
    assert not any(event.kind.value == "step_started" for event in events)
    assert not any(event.kind.value == "tool_receipt" for event in events)


def test_build_plan_messages_includes_schema() -> None:
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
    messages = build_plan_messages(view)
    assert "RUNTIME SYSTEM" in messages[0]["content"]
    assert "capability-based plan" in messages[0]["content"]
    assert "fix it" in messages[1]["content"]


def test_plan_finisher_rejects_non_finish_actions() -> None:
    spec = TaskSpec(task_id="t", objective="read it", workspace="/workspace")
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
    client = FakeClient(
        [json.dumps({"action": "call_tool", "capability": "read", "params": {}})]
    )

    action = LLMPlanFinisher(ModelConfig(model_id="test/fake"), client=client)(view)

    assert isinstance(action, Finish)
    assert "summary unavailable" in action.answer


def test_build_plan_messages_uses_real_newlines() -> None:
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

    system = build_plan_messages(view)[0]["content"]
    user = build_plan_messages(view)[1]["content"]

    assert "RUNTIME SYSTEM\nPlanFirst planning protocol" in system
    assert "\n" in user
    assert "\\n" not in system
    assert "\\n" not in user


def _reflect_view() -> ModelView:
    spec = TaskSpec(task_id="t", objective="fix it", workspace="/workspace")
    return ModelView(
        task=spec,
        system="RUNTIME SYSTEM",
        tools=(),
        working_set=(),
        debt=(),
        budget=spec.budget,
        input_seq=0,
        hash="h",
    )


def test_reflector_returns_one_corrective_call_tool() -> None:
    client = FakeClient(
        [
            json.dumps(
                {
                    "reflect": {
                        "action": "call_tool",
                        "capability": "edit",
                        "params": {"files": [{"path": "a.py", "content": "x"}]},
                    }
                }
            )
        ]
    )

    action = LLMReflector(ModelConfig(model_id="test/fake"), client=client)(
        _reflect_view(), Finish("done")
    )

    assert isinstance(action, CallTool)
    assert action.intent.capability is Capability.EDIT


def test_reflector_declining_returns_none() -> None:
    client = FakeClient([json.dumps({"reflect": None})])

    assert (
        LLMReflector(ModelConfig(model_id="test/fake"), client=client)(
            _reflect_view(), Finish("done")
        )
        is None
    )


@pytest.mark.parametrize(
    "payload",
    [
        json.dumps({"reflect": {"action": "finish", "answer": "all good"}}),
        json.dumps({"reflect": {"action": "ask_user", "question": "?"}}),
        json.dumps({"reflect": "done"}),
        "not json at all",
    ],
)
def test_reflector_never_claims_completion(payload: str) -> None:
    client = FakeClient([payload])

    action = LLMReflector(ModelConfig(model_id="test/fake"), client=client)(
        _reflect_view(), Finish("done")
    )

    assert action is None
    assert "proposed_claims" in llm.build_reflect_messages(
        _reflect_view(), Finish("done")
    )[1]["content"]


def test_plan_protocol_enumerates_capabilities() -> None:
    """A tool name in ``capability`` must not invalidate a whole plan."""

    from xenon.harness.plan import parse_plan

    messages = llm.build_plan_messages(_reflect_view())
    protocol = messages[0]["content"]

    assert "capability` MUST be one of: read, search, edit, verify, vcs, shell, remote" in (
        protocol
    )

    proposal = parse_plan(
        {
            "objective": "fix it",
            "analysis": "x",
            "steps": [
                {
                    "id": 1,
                    "kind": "execute",
                    "capability": "bash",
                    "params": {"command": "pytest -q"},
                    "targets": [],
                    "expects": [],
                    "depends_on": [],
                },
                {
                    "id": 2,
                    "kind": "execute",
                    "capability": "grep",
                    "params": {"pattern": "x"},
                    "targets": [],
                    "expects": [],
                    "depends_on": [],
                },
                {
                    "id": 3,
                    "kind": "edit",
                    "capability": "apply_patch",
                    "params": {},
                    "targets": [],
                    "expects": [],
                    "depends_on": [],
                },
            ],
        }
    ).proposal

    assert [step.intent.capability for step in proposal.steps] == [
        Capability.SHELL,
        Capability.SEARCH,
        Capability.EDIT,
    ]


def test_reflect_messages_describe_the_receipts() -> None:
    messages = llm.build_reflect_messages(_reflect_view(), Finish("did the work"))

    assert "RUNTIME SYSTEM" in messages[0]["content"]
    assert "Reflection review" in messages[0]["content"]
    assert "cannot declare the task done" in messages[0]["content"]
    assert "fix it" in messages[1]["content"]


def test_reflection_strategy_wraps_the_act_strategy() -> None:
    strategy = build_llm_reflection_strategy(
        ModelConfig(model_id="test/fake"), client=FakeClient([])
    )

    assert isinstance(strategy, llm.ReflectFirst)
    assert strategy.max_reflections == 1


def _plan_proposal():
    from xenon.harness.plan import parse_plan

    return parse_plan(
        {
            "objective": "fix it",
            "analysis": "read then edit",
            "steps": [
                {
                    "id": 1,
                    "kind": "edit",
                    "capability": "edit",
                    "params": {"files": []},
                    "targets": ["a.py"],
                    "expects": [
                        {"id": "e1", "kind": "file_changed", "target": "a.py"}
                    ],
                    "depends_on": [],
                }
            ],
        }
    ).proposal


def test_plan_reviewer_returns_the_corrected_plan() -> None:
    revised = {
        "objective": "fix it",
        "analysis": "read, edit, then verify",
        "steps": [
            {
                "id": 1,
                "kind": "edit",
                "capability": "edit",
                "params": {"files": []},
                "targets": ["a.py"],
                "expects": [{"id": "e1", "kind": "file_changed", "target": "a.py"}],
                "depends_on": [],
            },
            {
                "id": 2,
                "kind": "verify",
                "capability": "verify",
                "params": {"command": "pytest -q"},
                "targets": ["tests/test_a.py"],
                "expects": [],
                "depends_on": [1],
            },
        ],
    }
    client = FakeClient([json.dumps({"plan": revised})])

    plan = LLMPlanReviewer(ModelConfig(model_id="test/fake"), client=client)(
        _reflect_view(), _plan_proposal()
    )

    assert plan is not None
    assert [step.id for step in plan.steps] == [1, 2]


def test_plan_reviewer_declining_returns_none() -> None:
    client = FakeClient([json.dumps({"plan": None})])

    plan = LLMPlanReviewer(ModelConfig(model_id="test/fake"), client=client)(
        _reflect_view(), _plan_proposal()
    )

    assert plan is None


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        json.dumps({"plan": "add a verify step"}),
        json.dumps({"plan": {"steps": "nope"}}),
    ],
)
def test_plan_reviewer_rejects_unusable_output(payload: str) -> None:
    client = FakeClient([payload])

    assert (
        LLMPlanReviewer(ModelConfig(model_id="test/fake"), client=client)(
            _reflect_view(), _plan_proposal()
        )
        is None
    )


def test_step_reviewer_returns_one_corrective_call() -> None:
    client = FakeClient(
        [
            json.dumps(
                {
                    "reflect": {
                        "action": "call_tool",
                        "capability": "verify",
                        "params": {"command": "pytest -q"},
                    }
                }
            )
        ]
    )
    step = _plan_proposal().steps[0]

    action = LLMStepReviewer(ModelConfig(model_id="test/fake"), client=client)(
        _reflect_view(), step, Finish("step done")
    )

    assert isinstance(action, CallTool)
    assert action.intent.capability is Capability.VERIFY


@pytest.mark.parametrize(
    "payload",
    [
        json.dumps({"reflect": None}),
        json.dumps({"reflect": {"action": "finish", "answer": "ok"}}),
        json.dumps({"reflect": "done"}),
    ],
)
def test_step_reviewer_never_completes_or_judges(payload: str) -> None:
    client = FakeClient([payload])
    step = _plan_proposal().steps[0]

    action = LLMStepReviewer(ModelConfig(model_id="test/fake"), client=client)(
        _reflect_view(), step, Finish("step done")
    )

    assert action is None


def test_plan_reflection_strategy_wires_both_reviewers() -> None:
    strategy = llm.build_llm_plan_reflection_strategy(
        ModelConfig(model_id="test/fake"), client=FakeClient([])
    )

    assert isinstance(strategy, llm.PlanReact)
    assert strategy.plan_reviewer is not None
    assert strategy.step_reviewer is not None
    assert strategy.max_step_reviews == 1
