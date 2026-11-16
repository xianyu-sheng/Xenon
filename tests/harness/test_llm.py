"""ActFirst natural-language path (#220)."""

from __future__ import annotations

import hashlib
import json
import types
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
    LLMStepActor,
    LLMStepReviewer,
    ModelConfig,
    ModelConfigError,
    build_finish_messages,
    build_llm_loop_strategy,
    build_messages,
    build_plan_messages,
    build_reflect_messages,
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
    PropositionKind,
    StepContract,
    StepKind,
    TaskMode,
    TaskSpec,
    Think,
)
from xenon.kernel.policies import PermissionMode
from xenon.kernel.strategies import LoopProfile
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

    expanded = parse_action(
        '{"action":"call_tool","capability":"read",'
        '"prefer":"tool_result_read","params":{"result_id":"r-1"}}'
    )
    assert isinstance(expanded, CallTool)
    assert expanded.intent.prefer == "tool_result_read"


def test_a_cached_result_id_routes_to_the_result_reader() -> None:
    """A measured run wrote the tool name into the path slot.

    ``{"capability":"read","params":{"file_path":"tool_result_read",
    "result_id":"tr-…"}}`` bound to ``read_file``, which looked for a file
    called "tool_result_read" and failed — the model never saw the output it
    asked to page.  The presence of ``result_id`` is enough to tell which tool
    the call is for, whatever the other slots say.
    """

    action = parse_action(
        '{"action":"call_tool","capability":"read",'
        '"params":{"file_path":"tool_result_read","result_id":"tr-1",'
        '"mode":"raw"}}'
    )

    assert isinstance(action, CallTool)
    assert action.intent.prefer == "tool_result_read"
    assert action.intent.params == {"result_id": "tr-1", "mode": "raw"}
    # A plain read keeps working.
    plain = parse_action(
        '{"action":"call_tool","capability":"read",'
        '"params":{"file_path":"a.py"}}'
    )
    assert isinstance(plain, CallTool)
    assert plain.intent.prefer is None
    assert plain.intent.params == {"file_path": "a.py"}

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


@pytest.mark.parametrize(
    ("params", "expected_command", "expected_extra"),
    [
        ({"git_command": "status"}, "status", ""),
        ({"subcommand": "status"}, "status", ""),
        ({"action": "status"}, "status", ""),
        # The exact reply a measured run sent and had rejected as unparseable.
        ({"command": "git status --porcelain=v1 -b"}, "status", "--porcelain=v1 -b"),
        ({"git_command": "log", "action": "--oneline -3"}, "log", "--oneline -3"),
        ({"subcommand": "diff", "args": ["--stat"]}, "diff", "--stat"),
    ],
)
def test_parse_action_accepts_read_only_vcs_in_every_model_shape(
    params: dict[str, object], expected_command: str, expected_extra: str
) -> None:
    """A git read must bind no matter which key the model chose.

    The protocol advertises ``vcs``, the catalog ships a ``git`` spec, and the
    policy allows read-only queries.  Only this parser refused them, so every
    read looked like "invalid step proposal" until the step budget ran out.
    """

    action = parse_action(
        {
            "action": "call_tool",
            "capability": "vcs",
            "params": params,
        }
    )

    assert isinstance(action, CallTool)
    assert action.intent.capability.value == "vcs"
    assert action.intent.params["git_command"] == expected_command
    assert action.intent.params.get("action", "") == expected_extra


@pytest.mark.parametrize(
    "params",
    [
        {"git_command": "commit", "action": "-m x"},
        {"command": "git add ."},
        {"action": "push origin main"},
        {"command": "git status && rm -rf build"},
        {"git_command": "status | rm -rf build"},
    ],
)
def test_parse_action_rejects_vcs_writes_and_chains(params: dict[str, str]) -> None:
    with pytest.raises(ActionParseError, match="vcs capability"):
        parse_action(
            {"action": "call_tool", "capability": "vcs", "params": params}
        )


def test_parse_action_keeps_vcs_targets_and_repo_path() -> None:
    action = parse_action(
        {
            "action": "call_tool",
            "capability": "vcs",
            "params": {"git_command": "status", "repo_path": "sub"},
            "targets": ["sub"],
        }
    )

    assert isinstance(action, CallTool)
    assert action.intent.params["repo_path"] == "sub"
    assert action.intent.targets == ("sub",)


def test_build_messages_ships_a_fixed_prefix_and_one_turn() -> None:
    """Exactly two messages: the cacheable prefix and this turn's ask.

    The objective lives in the prefix, not in a per-request head block --
    anything repeated per call is bytes that have to match to be cached and
    tokens that are paid for on every request.
    """

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
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    # The system is exactly the view's prompt: no phase text may sit here,
    # because providers put the tool schemas directly behind it.
    assert messages[0]["content"] == "RUNTIME SYSTEM"
    assert messages[1]["role"] == "user"
    assert "Do not run git commit" in messages[1]["content"]
    assert "Return exactly one JSON action." in messages[1]["content"]


def test_the_turn_orders_task_stable_text_before_the_phase_protocol() -> None:
    """Lifetime order inside the turn: brief, earlier turns, then protocol.

    The earlier-turns projection is identical for every phase of one task, so
    it must sit ahead of the phase protocol; otherwise the finisher re-bills
    bytes the planner already paid for.
    """

    from dataclasses import dataclass

    @dataclass
    class _Record:
        task_id: str = "t0"
        objective: str = "earlier objective"
        status: str = "delivered"
        answer: str = "earlier answer"

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
        task_brief="TASK BRIEF",
        session_tasks=(_Record(),),
    )
    turn = build_messages(view)[1]["content"]

    assert turn.index("TASK BRIEF") < turn.index("Earlier turns this session")
    assert turn.index("Earlier turns this session") < turn.index(
        "Do not run git commit"
    )


def test_turn_context_carries_the_event_and_nothing_else() -> None:
    """The working set is not replayed; only the newest event is sent."""

    spec = TaskSpec(task_id="t", objective="fix it", workspace="/workspace")
    view = ModelView(
        task=spec,
        system="RUNTIME SYSTEM",
        tools=(),
        working_set=(
            {"seq": 1, "kind": "thought", "text": "look"},
            {"seq": 2, "kind": "tool_receipt", "tool": "read", "status": "ok"},
        ),
        debt=(),
        budget=spec.budget,
        input_seq=2,
        hash="h",
    )
    messages = build_messages(view, native=True)
    turn = messages[1]["content"]

    assert '"seq":2' in turn
    assert '"seq":1' not in turn
    assert "Call one tool for the next move." in turn


def test_a_rejected_plan_or_binding_is_the_turn() -> None:
    """Why the last move did not run must reach the model.

    The request carries no history; when a plan is rejected or a binding
    fails, the only signal the model gets is ``latest_respondable``.  A
    measured run re-submitted the same bad plan eleven times because the
    rejection was invisible.
    """

    spec = TaskSpec(task_id="t", objective="fix it", workspace="/workspace")
    for kind in ("binding_failed", "plan_updated"):
        view = ModelView(
            task=spec,
            system="RUNTIME SYSTEM",
            tools=(),
            working_set=(
                {"seq": 1, "kind": "task_started", "objective": "fix it"},
                {"seq": 2, "kind": kind, "reason": "no candidate"},
            ),
            debt=(),
            budget=spec.budget,
            input_seq=2,
            hash="h",
        )
        turn = build_messages(view, native=True)[1]["content"]
        assert f'"kind":"{kind}"' in turn, kind
        assert "no candidate" in turn


def test_llm_strategy_runs_real_workspace_and_finalizer(tmp_path: Path) -> None:
    _repo(tmp_path)
    client = FakeClient(_responses())
    config = ModelConfig(model_id="test/fake", max_tokens=1234)
    run = run_task(
        "add a function and test it",
        strategy=build_llm_loop_strategy(config, LoopProfile(), client=client),
        workspace=tmp_path,
        ledger_dir=tmp_path.parent / f"{tmp_path.name}-ledger",
        task_id="llm-1",
        # §8.1: a verify command asks before running; these tests assert the
        # loop/finalizer behavior, so they grant execution explicitly.
        permission=PermissionMode.BYPASS,
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
        strategy=build_llm_loop_strategy(config, LoopProfile(), client=client),
        workspace=tmp_path,
        ledger_dir=tmp_path.parent / f"{tmp_path.name}-ledger",
        task_id="llm-retry",
        permission=PermissionMode.BYPASS,
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
    client = FakeClient([json.dumps(_plan_dict())] + _responses())
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
            # §8.1: the plan contains a verify command; grant execution.
            "--permission",
            "bypass",
            "--output",
            str(output),
        ]
    )
    assert run_from_args(args) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["accepted"] is True
    assert payload["finalize_sha"]
    # The report reads this sibling for blocked runs, which never write a
    # `delivered` event.
    assert (tmp_path / "ledger" / "cli-llm.result.json").is_file()


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
        strategy=build_llm_loop_strategy(
            ModelConfig(model_id="test/fake"),
            LoopProfile(plan=True, step_scope=True, follow_plan=True),
            client=client,
        ),
        workspace=tmp_path,
        ledger_dir=tmp_path.parent / f"{tmp_path.name}-ledger",
        task_id="plan-llm-1",
        permission=PermissionMode.BYPASS,
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
        strategy=build_llm_loop_strategy(
            ModelConfig(model_id="test/fake"),
            LoopProfile(plan=True, step_scope=True, follow_plan=True),
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
        strategy=build_llm_loop_strategy(
            ModelConfig(model_id="test/fake"),
            LoopProfile(plan=True, step_scope=True, follow_plan=True),
            client=FakeClient(
                [json.dumps({"action": "finish", "answer": "done"})]
            ),
            resumed=True,
        ),
        objective=plan.objective,
        workspace=tmp_path,
        ledger_dir=ledger_dir,
        permission=PermissionMode.BYPASS,
        finalize=False,
    )

    assert resumed.result.accepted is True, resumed.result.debt
    events = Ledger(resumed.ledger_path, "resume-plan-1").read()
    assert len([e for e in events if e.kind is EventKind.TASK_STARTED]) == 1
    assert len([e for e in events if e.kind is EventKind.PLAN_ACCEPTED]) == 1
    assert any(e.kind is EventKind.PLAN_APPROVED for e in events)
    assert any(e.kind is EventKind.TOOL_RECEIPT for e in events)


def test_resume_task_applies_lessons_like_a_fresh_run(tmp_path: Path) -> None:
    """The learning chain must not stop at the plan-approval boundary.

    Both entry points go through ``LearningGateway.apply``; before the facade
    this was a second copy of the same calls, which is exactly where a path
    can silently lose the lesson.
    """

    from xenon.harness.lessons import LessonStore
    from xenon.kernel.lessons import Lesson
    from xenon.memory import MemoryBackendRegistry, MemoryService

    _repo(tmp_path)
    store = LessonStore(
        MemoryService(MemoryBackendRegistry(tmp_path / "memory"))
    )
    store.add(
        Lesson(
            id="L-resume",
            kind="pitfall",
            trigger_event="debt_opened",
            fingerprint="fp-resume",
            action_text="claim needs a receipt",
            shape="debt_opened|requirement_met",
        )
    )
    plan = parse_plan(_plan_dict())
    ledger_dir = tmp_path.parent / f"{tmp_path.name}-resume-lesson-ledger"
    planned = run_task(
        plan.objective,
        strategy=build_llm_loop_strategy(
            ModelConfig(model_id="test/fake"),
            LoopProfile(plan=True, step_scope=True, follow_plan=True),
            client=FakeClient([json.dumps(_plan_dict())]),
        ),
        workspace=tmp_path,
        ledger_dir=ledger_dir,
        task_id="resume-lesson-1",
        finalize=False,
        mode=TaskMode.PLAN,
        lesson_store=store,
    )
    accepted = Ledger(planned.ledger_path, "resume-lesson-1").read(
        EventKind.PLAN_ACCEPTED
    )[-1]
    digest = hashlib.sha256(
        canonical_json(accepted.payload["plan"]).encode("utf-8")
    ).hexdigest()

    resumed = resume_task(
        task_id="resume-lesson-1",
        plan=plan.proposal,
        plan_digest=digest,
        approval_id="approval-1",
        strategy=build_llm_loop_strategy(
            ModelConfig(model_id="test/fake"),
            LoopProfile(plan=True, step_scope=True, follow_plan=True),
            client=FakeClient(
                [json.dumps({"action": "finish", "answer": "done"})]
            ),
            resumed=True,
        ),
        objective=plan.objective,
        workspace=tmp_path,
        ledger_dir=ledger_dir,
        permission=PermissionMode.BYPASS,
        finalize=False,
        lesson_store=store,
    )

    applied = [
        event.payload["lesson_id"]
        for event in Ledger(resumed.ledger_path, "resume-lesson-1").read()
        if event.kind is EventKind.LESSON_APPLIED
    ]
    # Both executions carried it (one user task -> two execution phases), but
    # the application counter must count the *task* once.
    assert applied.count("L-resume") == 2
    assert store.get("L-resume").applied == 1


def test_resumed_plan_materializes_an_unbound_write(
    tmp_path: Path,
) -> None:
    """An approved edit with no payload runs through the step actor.

    A plan-only phase cannot call a tool, so its planner cannot author the
    content of a file it has never read; the runtime demotes that payload and
    the step survives as work.  Execution has to be able to perform it -- the
    follow-plan profile therefore carries a step actor (measured C10: the
    alternative was replaying a placeholder over the source file).
    """

    _repo(tmp_path)
    payload = _plan_dict()
    del payload["steps"][0]["params"]
    plan = parse_plan(payload)
    ledger_dir = tmp_path.parent / f"{tmp_path.name}-materialize-ledger"
    planned = run_task(
        plan.objective,
        strategy=build_llm_loop_strategy(
            ModelConfig(model_id="test/fake"),
            LoopProfile(plan=True),
            client=FakeClient([json.dumps(payload)]),
        ),
        workspace=tmp_path,
        ledger_dir=ledger_dir,
        task_id="materialize-1",
        finalize=False,
        mode=TaskMode.PLAN,
    )
    assert planned.result.phase.value == "planned"
    accepted = Ledger(planned.ledger_path, "materialize-1").read(
        EventKind.PLAN_ACCEPTED
    )[-1]
    digest = hashlib.sha256(
        canonical_json(accepted.payload["plan"]).encode("utf-8")
    ).hexdigest()

    source = "def add(a, b):\n    return a + b\n"
    test_source = (
        "from calc.add import add\n"
        "\n"
        "def test_add():\n"
        "    assert add(1, 2) == 3\n"
    )
    client = FakeClient(
        [
            json.dumps(
                {
                    "action": "call_tool",
                    "capability": "edit",
                    "params": {
                        "files": [
                            {"path": "calc/add.py", "content": source},
                            {"path": "tests/test_add.py", "content": test_source},
                        ]
                    },
                }
            ),
            json.dumps({"action": "finish", "answer": "step done", "claims": []}),
            json.dumps({"action": "finish", "answer": "fixed", "claims": []}),
        ]
    )
    resumed = resume_task(
        task_id="materialize-1",
        plan=plan.proposal,
        plan_digest=digest,
        approval_id="approval-1",
        strategy=build_llm_loop_strategy(
            ModelConfig(model_id="test/fake"),
            LoopProfile(plan=True, step_scope=True, follow_plan=True),
            client=client,
            resumed=True,
        ),
        objective=plan.objective,
        workspace=tmp_path,
        ledger_dir=ledger_dir,
        permission=PermissionMode.BYPASS,
        finalize=False,
    )

    assert resumed.result.accepted is True, resumed.result.debt
    assert (tmp_path / "calc" / "add.py").read_text(encoding="utf-8") == source
    # The step actor was really asked: the first request is a step prompt.
    first_user = client.calls[0][1][-1]["content"]
    assert "Working on step" in first_user


def test_llm_plan_invalid_json_fails_closed(tmp_path: Path) -> None:
    """An empty plan is recorded, never executed.

    The planner's answer is submitted to the ledger (so the run can explain
    why nothing ran), and the follow-up asks for a plain answer instead of
    starting a step.  What must never happen is a tool call or a plan step.
    """
    _repo(tmp_path)
    client = FakeClient(
        ['{"steps":[]}', '{"action":"finish","answer":"no tool work","claims":[]}']
    )
    run = run_task(
        "add a function and test it",
        strategy=build_llm_loop_strategy(
            ModelConfig(model_id="test/fake"),
            LoopProfile(plan=True, step_scope=True, follow_plan=True),
            client=client,
        ),
        workspace=tmp_path,
        ledger_dir=tmp_path.parent / f"{tmp_path.name}-ledger",
        task_id="plan-llm-bad",
        finalize=False,
    )

    events = Ledger(run.ledger_path, "plan-llm-bad").read()
    assert not any(event.kind.value == "step_started" for event in events)
    assert not any(event.kind.value == "tool_receipt" for event in events)
    assert any(event.kind.value == "plan_proposed" for event in events)
    assert run.result.answer == "no tool work"


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
    assert messages[0]["content"] == "RUNTIME SYSTEM"
    assert "capability-based plan" in messages[1]["content"]
    # No state blob: the objective, workspace and tool list are already in the
    # system prompt and in the tool schemas.  Repeating them per role made each
    # role's cached prefix a different length.
    assert messages[1]["content"].endswith("Return exactly one JSON plan.")


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

    assert system == "RUNTIME SYSTEM"
    assert user.startswith("PlanFirst planning protocol")
    assert user.endswith("Return exactly one JSON plan.")
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


def test_every_phase_starts_with_the_same_system_bytes() -> None:
    """The tool schemas sit right behind the system, so it must not vary.

    Providers serialise system -> tools -> messages.  While each role appended
    its own protocol to the system, the divergence landed immediately before
    the schemas: measured on a real task, the actor hit 1280 cached tokens and
    finish hit 640 -- the entire tool block was billed at full price on every
    plan/review/finish call.  The phase text is the same text either way; it
    only has to live in the turn.
    """

    from dataclasses import replace

    from xenon.harness.llm import (
        _cache_tools,
        _native_tools,
        build_finish_messages,
        build_plan_messages,
        build_step_messages,
    )
    from xenon.kernel.contracts import StepContract, StepKind

    view = replace(
        _reflect_view(),
        tools=(
            {
                "name": "read_file",
                "description": "Read a file.",
                "parameters": {"path": "path"},
            },
        ),
    )
    phases = [
        build_messages(view),
        build_messages(view, native=True),
        build_plan_messages(view),
        build_finish_messages(view),
        build_reflect_messages(view, None),
        build_step_messages(view, StepContract(id=1, kind=StepKind.EDIT), calls=0),
    ]
    systems = {messages[0]["content"] for messages in phases}
    assert systems == {"RUNTIME SYSTEM"}
    # Every phase that is not the actor still sends the actor's exact list.
    assert _cache_tools(view, None) == _native_tools(view)
    assert _cache_tools(view, client=lambda *a, **k: "") is None


def test_a_phase_that_calls_a_tool_is_retried_without_tools() -> None:
    """Sending schemas must not let an answered phase act.

    ``auto`` keeps thinking enabled (DeepSeek disables it for any other
    choice); the protocol already says not to call a tool, and the one reply
    that does anyway is retried with ``none`` instead of being passed to a
    JSON parser that would see an empty string.
    """

    from xenon.utils import llm_client

    calls: list[str] = []

    class _Response:
        def __init__(self, content: str, tool_calls: list[dict]) -> None:
            self.content = content
            self.tool_calls = tool_calls
            self.usage = types.SimpleNamespace()

    def _fake(model_id, messages, **kwargs):
        calls.append(str(kwargs.get("tool_choice")))
        if kwargs.get("tool_choice") == "none":
            return _Response('{"action":"finish","answer":"done"}', [])
        return _Response("", [{"id": "1", "name": "read_file", "arguments": {}}])

    real = llm_client.chat_completion_with_tools
    llm_client.chat_completion_with_tools = _fake
    try:
        raw = llm._call_model_with_tools(
            ModelConfig(model_id="test/fake"),
            build_finish_messages(_reflect_view()),
            [{"name": "read_file", "description": "x", "parameters": {}}],
            phase="finisher",
        )
    finally:
        llm_client.chat_completion_with_tools = real

    assert calls == ["auto", "none"]
    assert "finish" in raw


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
    protocol = messages[1]["content"]

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

    assert messages[0]["content"] == "RUNTIME SYSTEM"
    assert "Reflection review" in messages[1]["content"]
    assert "cannot declare the task done" in messages[1]["content"]
    # The claim under review AND the receipts it is checked against travel:
    # the reflector parses one reply and cannot call get_state, so a claim
    # reviewed without evidence would be a blind review.  The working set is
    # fold-bounded, so the request does not grow with history.
    assert "did the work" in messages[1]["content"]
    assert '"receipts"' in messages[1]["content"]


def test_reflection_strategy_wraps_the_act_strategy() -> None:
    strategy = build_llm_loop_strategy(
        ModelConfig(model_id="test/fake"),
        LoopProfile(reflect_rounds=1, reflect_calls=1),
        client=FakeClient([]),
    )

    assert isinstance(strategy, llm.LoopStrategy)
    assert strategy.profile.reflect_rounds == 1
    assert strategy.reflector is not None


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
    strategy = build_llm_loop_strategy(
        ModelConfig(model_id="test/fake"),
        LoopProfile(
            plan=True,
            plan_review=True,
            step_scope=True,
            step_calls=6,
            step_reflect=True,
        ),
        client=FakeClient([]),
    )

    assert isinstance(strategy, llm.LoopStrategy)
    assert strategy.plan_reviewer is not None
    assert strategy.step_reviewer is not None
    assert strategy.profile.step_reflect is True


# ── 截断：变成具体下一步，而不是打穿 loop（#322）──────────────────────


def test_truncated_reply_becomes_an_actionable_thought():
    """A cut-off reply is a recoverable condition, not a crash."""

    from xenon.utils.llm_errors import ResponseTruncatedError

    def boom(*args, **kwargs):
        raise ResponseTruncatedError("cut off after 3 continuations")

    proposer = LLMActionProposer(ModelConfig(model_id="test/fake"), client=boom)
    action = proposer(_reflect_view())

    assert type(action).__name__ == "Think"
    assert "output limit" in action.text
    assert "one JSON action" in action.text
    assert "short" in action.text


def test_truncated_step_reply_does_not_abort_the_step():
    from xenon.utils.llm_errors import ResponseTruncatedError

    def boom(*args, **kwargs):
        raise ResponseTruncatedError("cut off")

    actor = LLMStepActor(ModelConfig(model_id="test/fake"), client=boom)
    step = StepContract(
        id="s1",
        kind=StepKind.INSPECT,
        intent="look",
        targets=("a.py",),
    )

    action = actor(_reflect_view(), step, 1)

    assert type(action).__name__ == "Think"
    assert "output limit" in action.text


def test_an_unparseable_step_reply_says_why() -> None:
    """The parser's reason is the only feedback the model gets.

    A measured run showed the bare note "step 3: invalid step proposal" and
    the model repeated the same shape until the budget ran out.  Without the
    reason, the note is indistinguishable from "you cannot do this".
    """

    def bad(*args, **kwargs):
        return '{"action":"call_tool","capability":"nope"}'

    actor = LLMStepActor(ModelConfig(model_id="test/fake"), client=bad)
    step = StepContract(
        id="s1",
        kind=StepKind.INSPECT,
        intent="look",
        targets=("a.py",),
    )

    action = actor(_reflect_view(), step, 1)

    assert type(action).__name__ == "Think"
    assert "invalid step proposal" in action.text
    assert "unknown capability" in action.text


# ── PlanFirst finisher: the answer must survive a malformed claim (#363) ──


def test_a_claim_may_use_the_documented_kind_target_shorthand() -> None:
    """Models emit this; the protocol documents it instead of the parser guessing."""

    action = parse_action(
        {
            "action": "finish",
            "answer": "done",
            "claims": [
                "file_changed:calc.py",
                "test_passed:tests/test_calc.py",
            ],
        }
    )

    assert isinstance(action, Finish)
    assert [claim.kind for claim in action.claims] == [
        PropositionKind.FILE_CHANGED,
        PropositionKind.TEST_PASSED,
    ]
    assert action.claims[1].target == "tests/test_calc.py"


def test_a_shorthand_with_more_than_one_colon_fails_closed() -> None:
    with pytest.raises(ActionParseError):
        parse_action(
            {
                "action": "finish",
                "answer": "x",
                "claims": ["file_changed:a.py:b.py"],
            }
        )


def test_a_bare_string_claim_without_a_target_fails_closed() -> None:
    with pytest.raises(ActionParseError):
        parse_action({"action": "finish", "answer": "x", "claims": ["file_changed"]})


def test_the_finisher_keeps_the_answer_when_only_claims_are_malformed() -> None:
    """A delivered run must not lose its summary over one bad claim field."""

    from xenon.harness.llm import _FINISH_PLACEHOLDER, LLMPlanFinisher

    view = types.SimpleNamespace(
        task=types.SimpleNamespace(objective="o", constraints=()),
        working_set=[],
        debt=[],
        system="sys",
    )
    reply = json.dumps(
        {
            "action": "finish",
            "answer": "Added sub() and the tests pass.",
            "claims": ["file_changed"],  # not parseable: no target
        }
    )
    finisher = LLMPlanFinisher(
        ModelConfig(model_id="test/fake"), client=lambda *a, **k: reply
    )

    action = finisher(view)

    assert isinstance(action, Finish)
    assert action.answer == "Added sub() and the tests pass."
    assert action.claims == ()
    assert _FINISH_PLACEHOLDER not in action.answer


def _finisher_view() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        task=types.SimpleNamespace(objective="o", constraints=()),
        working_set=[],
        debt=[],
        system="sys",
    )


def test_the_finisher_keeps_a_prose_reply_as_the_answer() -> None:
    """A prose summary is the human-readable answer, not a parse failure.

    Measured (Phase D, real MCP turn): the tool receipt carried
    ``echo:hello-e2e``, the finisher answered in prose, and the placeholder
    replaced it — the delivered turn said "final summary unavailable" even
    though the work had succeeded.
    """

    from xenon.harness.llm import _FINISH_PLACEHOLDER, LLMPlanFinisher

    finisher = LLMPlanFinisher(
        ModelConfig(model_id="test/fake"),
        client=lambda *a, **k: "工具返回的原文是 echo:hello-e2e。",
    )

    answer = finisher(_finisher_view()).answer

    assert answer == "工具返回的原文是 echo:hello-e2e。"
    assert _FINISH_PLACEHOLDER not in answer


def test_the_finisher_unwraps_a_fenced_prose_reply() -> None:
    from xenon.harness.llm import LLMPlanFinisher

    finisher = LLMPlanFinisher(
        ModelConfig(model_id="test/fake"),
        client=lambda *a, **k: "```text\n工具返回 echo:hello-e2e\n```",
    )

    assert finisher(_finisher_view()).answer == "工具返回 echo:hello-e2e"


@pytest.mark.parametrize("reply", ["", "   ", '{"action": "finish", "answer": '])
def test_the_finisher_keeps_the_placeholder_when_there_is_no_answer(reply: str) -> None:
    """An empty or broken-object reply has no human-readable text to keep."""

    from xenon.harness.llm import _FINISH_PLACEHOLDER, LLMPlanFinisher

    finisher = LLMPlanFinisher(
        ModelConfig(model_id="test/fake"), client=lambda *a, **k: reply
    )

    assert finisher(_finisher_view()).answer == _FINISH_PLACEHOLDER


def test_the_finish_protocol_documents_the_claim_shape() -> None:
    """The prompt must show the shape, or the model will invent one."""

    from xenon.harness.llm import _FINISH_PROTOCOL

    assert '"kind":"file_changed"' in _FINISH_PROTOCOL
    assert "file_changed:path" in _FINISH_PROTOCOL
    assert "test_passed:tests/test_x.py" in _FINISH_PROTOCOL


def _native_view() -> ModelView:
    from xenon.catalog.specs import CORE_SPECS
    from xenon.kernel.view.base import _tool_schema

    spec = TaskSpec(task_id="t", objective="fix it", workspace="/workspace")
    return ModelView(
        task=spec,
        system="SYSTEM",
        tools=tuple(_tool_schema(item, expanded=True) for item in CORE_SPECS),
        working_set=(),
        debt=(),
        budget=spec.budget,
        input_seq=0,
        hash="h",
    )


def test_native_tools_carry_real_json_schema() -> None:
    """Prose parameter names let a model invent them.

    ``{"file_path": "path to read"}`` is not a schema: it has no type and no
    required list, so nothing stops a call that says ``files``.
    """
    from xenon.harness.llm import _native_tools

    tools = {item["name"]: item for item in _native_tools(_native_view())}
    read = tools["read"]["parameters"]

    assert read["type"] == "object"
    assert read["properties"]["file_path"]["type"] == "string"
    assert read["required"] == ["file_path"]


def test_terminal_actions_are_offered_as_tools() -> None:
    from xenon.harness.llm import _native_tools

    names = {item["name"] for item in _native_tools(_native_view())}

    assert {"finish", "think", "abort", "ask_user"} <= names


def test_a_tool_call_maps_onto_a_capability() -> None:
    from xenon.harness.llm import action_from_tool_call

    action = action_from_tool_call(
        _native_view(), "read", {"file_path": "calc.py"}
    )

    assert isinstance(action, CallTool)
    assert action.intent.capability is Capability.READ
    assert action.intent.params == {"file_path": "calc.py"}
    assert action.intent.targets == ("calc.py",)


def test_a_new_file_write_is_a_write_not_a_guess() -> None:
    from xenon.harness.llm import action_from_tool_call

    action = action_from_tool_call(
        _native_view(),
        "apply_patch",
        {"files": [{"path": "tests/test_new.py", "content": "x"}]},
    )

    assert isinstance(action, CallTool)
    assert action.intent.capability is Capability.EDIT
    assert action.intent.targets == ("tests/test_new.py",)


def test_finish_claims_are_typed_from_the_tool_call() -> None:
    from xenon.harness.llm import action_from_tool_call

    action = action_from_tool_call(
        _native_view(),
        "finish",
        {
            "answer": "done",
            "claims": [{"kind": "file_changed", "target": "calc.py"}],
        },
    )

    assert isinstance(action, Finish)
    assert action.claims[0].kind is PropositionKind.FILE_CHANGED
    assert action.claims[0].target == "calc.py"


def test_an_unknown_tool_name_fails_closed() -> None:
    from xenon.harness.llm import ActionParseError, action_from_tool_call

    with pytest.raises(ActionParseError, match="unknown tool"):
        action_from_tool_call(_native_view(), "make_it_so", {})


def _native_view_with_mcp() -> ModelView:
    from xenon.catalog.specs import CORE_SPECS, mcp_tool_specs
    from xenon.kernel.view.base import _tool_schema

    mcp = mcp_tool_specs(
        {
            "trains:query_trains": (
                "trains",
                {
                    "name": "query_trains",
                    "description": "查询高铁车次",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "from_city": {"type": "string"},
                            "to_city": {"type": "string"},
                        },
                        "required": ["from_city", "to_city"],
                    },
                },
            )
        }
    )
    spec = TaskSpec(task_id="t", objective="query trains", workspace="/workspace")
    return ModelView(
        task=spec,
        system="SYSTEM",
        tools=tuple(
            _tool_schema(item, expanded=True) for item in (*CORE_SPECS, *mcp)
        ),
        working_set=(),
        debt=(),
        budget=spec.budget,
        input_seq=0,
        hash="h",
    )


def test_an_mcp_tool_ships_a_provider_safe_function_name() -> None:
    """``server:tool`` is the catalog key, not a wire name.

    Anthropic and OpenAI reject function names outside ``[A-Za-z0-9_-]``, so
    sending ``trains:query_trains`` made the first model call of every MCP
    task fail before the kernel could bind anything.
    """

    import re as _re

    from xenon.harness.llm import _native_tools, native_tool_name

    tools = {item["name"]: item for item in _native_tools(_native_view_with_mcp())}
    wire = native_tool_name("trains:query_trains")

    assert wire in tools
    assert _re.fullmatch(r"[A-Za-z0-9_-]{1,64}", wire)
    assert tools[wire]["parameters"]["required"] == ["from_city", "to_city"]
    # A safe name is left exactly as the catalog spells it.
    assert native_tool_name("read") == "read"


def test_a_sanitized_mcp_call_binds_to_the_catalog_name() -> None:
    from xenon.harness.llm import action_from_tool_call, native_tool_name

    action = action_from_tool_call(
        _native_view_with_mcp(),
        native_tool_name("trains:query_trains"),
        {"from_city": "昆山", "to_city": "上海"},
    )

    assert isinstance(action, CallTool)
    assert action.intent.capability is Capability.REMOTE
    assert action.intent.params == {
        "tool_name": "trains:query_trains",
        "tool_args": {"from_city": "昆山", "to_city": "上海"},
    }
    assert action.intent.prefer == "trains:query_trains"


def test_native_tool_names_stay_bounded_and_deterministic() -> None:
    from xenon.harness.llm import native_tool_name

    long_name = "server:" + "x" * 80
    first = native_tool_name(long_name)

    assert first == native_tool_name(long_name)
    assert len(first) <= 64
    assert first.replace("_", "").isalnum()
    assert first != native_tool_name("server:" + "y" * 80)


def _native_proposer(monkeypatch, tool_calls):
    """An actor whose provider reply carries the given tool calls."""

    from xenon.utils.llm_usage import LLMResponse

    def fake_call(*_args, **_kwargs):
        return LLMResponse(
            content="", tool_calls=list(tool_calls), finish_reason="tool_calls"
        )

    monkeypatch.setattr(
        "xenon.utils.llm_client.chat_completion_with_tools", fake_call
    )
    return LLMActionProposer(ModelConfig(model_id="test/fake"))


def test_a_parallel_reply_runs_the_first_call_and_names_the_rest(
    monkeypatch,
) -> None:
    """A provider may send several tool calls; the loop runs one.

    A measured reply to "inspect calc.py and util.py" carried two ``tool_use``
    blocks.  Taking only the first and dropping the second is what turned a
    read-then-write task into twelve reads and zero writes -- so the calls that
    did not run have to be named back to the model, not silently vanish.
    """

    proposer = _native_proposer(
        monkeypatch,
        [
            {"name": "read", "arguments": '{"file_path": "calc.py"}'},
            {"name": "read", "arguments": '{"file_path": "util.py"}'},
        ],
    )

    action = proposer(_native_view())

    assert isinstance(action, CallTool)
    assert action.intent.targets == ("calc.py",)
    assert len(proposer.skipped_calls) == 1
    assert "util.py" in proposer.skipped_calls[0]


def test_one_tool_call_produces_no_skip_noise(monkeypatch) -> None:
    proposer = _native_proposer(
        monkeypatch, [{"name": "read", "arguments": '{"file_path": "calc.py"}'}]
    )

    proposer(_native_view())

    assert proposer.skipped_calls == ()


def test_a_later_single_call_clears_an_earlier_skip(monkeypatch) -> None:
    """The note describes one reply, not a history of them.

    It is cleared per *call*, not inside the parallel-call bookkeeping: a reply
    with no tool calls returns before that bookkeeping runs, and the previous
    reply's dropped calls then resurfaced as if they belonged to the new one.
    """

    from xenon.utils.llm_usage import LLMResponse

    replies = [
        [
            {"name": "read", "arguments": '{"file_path": "calc.py"}'},
            {"name": "read", "arguments": '{"file_path": "util.py"}'},
        ],
        [{"name": "read", "arguments": '{"file_path": "x.py"}'}],
    ]

    def fake_call(*_args, **_kwargs):
        return LLMResponse(
            content="", tool_calls=list(replies.pop(0)), finish_reason="tool_calls"
        )

    monkeypatch.setattr(
        "xenon.utils.llm_client.chat_completion_with_tools", fake_call
    )
    proposer = LLMActionProposer(ModelConfig(model_id="test/fake"))

    proposer(_native_view())
    assert proposer.skipped_calls

    proposer(_native_view())
    assert proposer.skipped_calls == ()


def test_a_dropped_state_read_is_answered_not_deferred(monkeypatch) -> None:
    """``get_state`` beside a real call must not be lost.

    Rendering state only reads the view the proposer already holds, so there is
    nothing to defer -- and a measured run showed the cost of dropping it: the
    model asked "am I done?", got no answer, and re-read the same files until
    the budget ran out.
    """

    proposer = _native_proposer(
        monkeypatch,
        [
            {"name": "read", "arguments": '{"file_path": "calc.py"}'},
            {"name": "get_state", "arguments": '{"what": "state"}'},
        ],
    )

    action = proposer(_native_view())

    assert isinstance(action, CallTool)
    assert len(proposer.answered_alongside) == 1
    assert '"phase"' in proposer.answered_alongside[0]
    # It was answered, so it is not also reported as un-run.
    assert proposer.skipped_calls == ()


def test_a_dropped_side_effectful_call_is_deferred_not_answered(
    monkeypatch,
) -> None:
    """Only view-only calls are answerable here; the rest need the runtime."""

    proposer = _native_proposer(
        monkeypatch,
        [
            {"name": "read", "arguments": '{"file_path": "calc.py"}'},
            {"name": "apply_patch", "arguments": '{"files": []}'},
        ],
    )

    proposer(_native_view())

    assert proposer.answered_alongside == ()
    assert proposer.skipped_calls


def test_a_state_read_spelled_as_a_tool_call_is_answered() -> None:
    """A measured failure: the model asked for state through ``call_tool``.

    It wrote ``{"action":"call_tool","capability":"remote","params":{"tool":
    "get_state","what":"session"}}``.  Nothing exposes "remote", so binding
    failed twice and the run answered "I could not get the session history".
    The terminal vocabulary belongs to the harness, so the spelling is repaired
    here instead of teaching the kernel a tool name.
    """

    from xenon.harness.llm import action_or_state
    from xenon.kernel.contracts import StateRead

    raw = json.dumps(
        {
            "action": "call_tool",
            "capability": "remote",
            "params": {"tool": "get_state", "what": "session"},
        }
    )

    action = action_or_state(_reflect_view(), raw)

    assert isinstance(action, StateRead)
    assert "No earlier tasks in this session." in action.text


def test_an_unrelated_call_tool_is_left_to_the_binder() -> None:
    from xenon.harness.llm import action_or_state

    raw = json.dumps(
        {
            "action": "call_tool",
            "capability": "remote",
            "params": {"tool_name": "server:tool", "tool_args": {}},
        }
    )

    action = action_or_state(_reflect_view(), raw)

    # It stays a tool call: the name is a real remote tool, not a terminal action.
    assert type(action).__name__ == "CallTool"


def test_a_plan_step_cannot_be_a_state_read() -> None:
    """Planning answered for a state query instead of asking it.

    Measured: the plan's only step was ``{"capability":"read","params":
    {"tool":"get_state","what":"session"}}``.  Steps bind to catalog tools, so
    the run finished without the answer and without a single call.  The plan is
    rejected with a message the model can act on.
    """

    from xenon.harness.plan import PlanError, parse_plan

    with pytest.raises(PlanError, match="not a plan step"):
        parse_plan(
            {
                "objective": "look up earlier work",
                "steps": [
                    {
                        "id": 1,
                        "kind": "inspect",
                        "capability": "read",
                        "params": {"tool": "get_state", "what": "session"},
                    }
                ],
            }
        )


def test_a_state_step_is_demoted_to_a_step_the_actor_acts_on() -> None:
    """The plan keeps the step but drops the intent, so the actor can ask.

    A step naming ``get_state`` binds to no tool (it is not one).  Demoting it
    keeps the plan's shape and hands the asking to the actor, the only party
    that can answer it.
    """

    from xenon.harness.llm import LLMPlanProposer

    client = FakeClient(
        [
            json.dumps(
                {
                    "plan": {
                        "objective": "look up earlier work",
                        "steps": [
                            {
                                "id": 1,
                                "kind": "inspect",
                                "capability": "read",
                                "params": {"tool": "get_state", "what": "session"},
                            }
                        ],
                    }
                }
            )
        ]
    )
    proposer = LLMPlanProposer(ModelConfig(model_id="m/test"), client=client)

    proposal = proposer(_reflect_view())

    assert len(proposal.steps) == 1
    assert proposal.steps[0].intent is None
    assert proposal.steps[0].kind.value == "inspect"


def test_a_prose_plan_reply_gets_one_repair_turn() -> None:
    """The planner runs once; a prose reply must not end the task.

    Measured task ("write tests for slugify.py"): the planner answered in
    prose, the plan was rejected as "model response is not JSON", and the
    turn finished with zero tools and "no evidence".  A single repair turn —
    carrying the parser's reason — is what turns a dead end into a retry.
    """

    from xenon.harness.llm import LLMPlanProposer

    client = FakeClient(
        [
            "Sure! First I will read the file, then write tests.",
            json.dumps(
                {
                    "plan": {
                        "objective": "write tests",
                        "steps": [
                            {
                                "id": 1,
                                "kind": "edit",
                                "capability": "edit",
                                "params": {
                                    "files": [
                                        {
                                            "path": "tests/test_x.py",
                                            "content": "def test_x():\n    pass\n",
                                        }
                                    ]
                                },
                            }
                        ],
                    }
                }
            ),
        ]
    )
    proposer = LLMPlanProposer(ModelConfig(model_id="m/test"), client=client)

    proposal = proposer(_reflect_view())

    assert len(proposal.steps) == 1
    assert len(client.calls) == 2
    repair = client.calls[1][1]
    assert "could not be parsed" in repair[-1]["content"]
    assert "not JSON" in repair[-1]["content"]


def test_a_second_bad_plan_reply_still_fails_closed() -> None:
    from xenon.harness.llm import LLMPlanProposer

    client = FakeClient(["nope", "still nope"])
    proposer = LLMPlanProposer(ModelConfig(model_id="m/test"), client=client)

    proposal = proposer(_reflect_view())

    assert proposal.steps == ()
    assert "invalid plan proposal" in proposal.analysis


def test_the_step_prompt_carries_the_same_tail_context() -> None:
    """The plan-first strategy has its own turn builder — and it dropped both
    the compaction brief and the session pointer.

    Measured: a step-driven run never saw "Earlier tasks this session", went
    looking through ledger files on disk instead of asking, and answered
    "no history" when reading was forbidden.  One tail builder, used by every
    strategy's turn message, is what keeps that from drifting again.
    """

    from dataclasses import replace

    from xenon.harness.llm import build_step_messages
    from xenon.kernel.contracts import StepContract, StepKind

    view = replace(
        _reflect_view(),
        session_history=("t1 · fix calc.py → verified",),
        working_set=(
            {
                "seq": 2,
                "kind": "context_summary",
                "summary_note": "BRIEF: mod_1 done",
            },
        ),
    )
    step = StepContract(id=1, kind=StepKind.INSPECT)

    messages = build_step_messages(view, step, calls=0)
    turn = messages[-1]["content"]

    assert "Earlier tasks this session" in turn
    assert "BRIEF: mod_1 done" in turn


def test_a_replan_round_sees_the_debt_and_receipts() -> None:
    """A later plan round is asked to fix what execution already learned.

    Measured on C10: four rounds proposed the same placeholder edit because the
    planner only ever received "Return exactly one JSON plan." -- not the
    pytest failure, not the unmet `test_passed` obligation.
    """

    from dataclasses import replace

    from xenon.harness.llm import build_plan_messages
    from xenon.kernel.contracts import (
        Debt,
        Obligation,
        PropositionKind,
    )

    debt = Debt(
        obligation=Obligation(
            id="e5", kind=PropositionKind.TEST_PASSED, target="tests/test_x.py"
        ),
        reason="receipt status: failed",
    )
    view = replace(
        _reflect_view(),
        debt=(debt,),
        working_set=(
            {
                "seq": 9,
                "tool": "test",
                "observation": "1 failed, 3 passed",
            },
        ),
    )

    user = build_plan_messages(view)[-1]["content"]

    assert "1 failed, 3 passed" in user
    assert "receipt status: failed" in user
    assert user.rstrip().endswith("Return exactly one JSON plan.")


def test_a_first_plan_ask_stays_a_bare_request() -> None:
    """No receipts, no debt: the request is the single line it always was."""

    from xenon.harness.llm import build_plan_messages

    assert build_plan_messages(_reflect_view())[-1]["content"].endswith(
        "Return exactly one JSON plan."
    )


def test_the_step_prompt_shows_a_violated_verdict() -> None:
    """A repair turn must know which obligation is still open.

    Measured on C10: the edit step's actor saw only its own read receipt, read
    again, and the step was released with a `file_changed` gap after three
    reads and zero writes.  The verdict the gate already reached is the fact
    the actor was missing.
    """

    from dataclasses import replace

    from xenon.harness.llm import build_step_messages
    from xenon.kernel.contracts import (
        StepContract,
        StepKind,
        Verdict,
        VerdictStatus,
        VerificationStage,
    )

    view = replace(
        _reflect_view(),
        step_verdicts=(
            Verdict(
                obligation_id="e4",
                stage=VerificationStage.V1,
                status=VerdictStatus.VIOLATED,
                reason="no changed file fact",
            ),
        ),
    )
    step = StepContract(id=4, kind=StepKind.EDIT, targets=("calc.py",))

    turn = build_step_messages(view, step, calls=1)[-1]["content"]

    assert "step_verification" in turn
    assert "e4" in turn
    assert "no changed file fact" in turn


def test_the_step_prompt_carries_the_plan_binding() -> None:
    """The actor must see the step's intent and what proves it done.

    Measured on C10: the plan put the repair instruction in the step's intent
    params and its `file_changed` expectation in `expects`, but the prompt
    showed only `id/kind/targets` and bare obligation ids.  The actor read the
    same file three times and the step was released with a gap.
    """

    from dataclasses import replace

    from xenon.harness.llm import build_step_messages
    from xenon.kernel.contracts import (
        Capability,
        Obligation,
        ObligationKind,
        StepContract,
        StepKind,
        ToolIntent,
    )

    step = StepContract(
        id=4,
        kind=StepKind.EDIT,
        intent=ToolIntent(
            capability=Capability.EDIT,
            params={"file_path": "calc.py", "instructions": "empty input → 0"},
        ),
        targets=("calc.py",),
        expects=(
            Obligation(
                id="e4", kind=ObligationKind.FILE_CHANGED, target="calc.py"
            ),
        ),
    )

    turn = build_step_messages(replace(_reflect_view()), step, calls=1)[-1]["content"]

    assert "empty input → 0" in turn
    assert "calc.py" in turn
    assert '"kind":"file_changed"' in turn


def test_the_step_protocol_documents_how_to_write() -> None:
    """`edit` is allowed by the step protocol, so its payload shape must be
    there: the step system block replaces the generic action protocol, and an
    undocumented write is a write the actor never makes."""

    from dataclasses import replace

    from xenon.harness.llm import build_step_messages
    from xenon.kernel.contracts import StepContract, StepKind

    messages = build_step_messages(
        replace(_reflect_view()), StepContract(id=1, kind=StepKind.EDIT), calls=0
    )
    turn = messages[1]["content"]

    assert '"capability":"edit"' in turn
    assert '"files"' in turn
    assert '"edits"' in turn


def test_the_step_reviewer_sees_the_refusal_notes() -> None:
    """The live profile is plan + step_scope + step_reflect.

    After the step actor finishes, the reviewer is the only phase that gets a
    say -- the plan's own intent is not replayed and the main actor is not
    asked.  It received the step and the verdicts but never the harness's
    notes, so a real read-only count run proposed another refused command,
    closed the step, and delivered an unverified number ("这里没有弹出授权").
    """

    from dataclasses import replace

    from xenon.harness.llm import build_step_review_messages
    from xenon.kernel.contracts import Finish, StepContract, StepKind

    step = StepContract(id=1, kind=StepKind.EXECUTE)
    view = replace(_reflect_view(), active_step=step, steps=(step,))

    quiet = build_step_review_messages(view, step, Finish("step done"))[-1]
    assert "read-only mode" not in quiet["content"]

    refused = replace(
        view,
        notes=(
            "shell: refused by policy (read-only mode). This action did not "
            "run. Do not repeat it; If the task genuinely needs exactly this "
            "call, ask for it with `request_escalation`",
        ),
    )
    turn = build_step_review_messages(refused, step, Finish("step done"))[-1]

    assert "read-only mode" in turn["content"]
    assert "request_escalation" in turn["content"]


def test_the_step_review_protocol_documents_the_escalation_option() -> None:
    """Option C: the reviewer's answer to a refused call is an ask, not a
    reworded command.  The protocol used to offer only call_tool or null."""

    from xenon.harness.llm import _STEP_REVIEW_PROTOCOL

    assert "request_escalation" in _STEP_REVIEW_PROTOCOL
