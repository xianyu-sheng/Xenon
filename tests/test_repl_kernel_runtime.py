"""Opt-in REPL kernel runtime adapter (#224 / #202 first slice)."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from rich.console import Console

from tests.harness.test_exec import _plan_dict, _repo
from xenon.engine.callbacks import ConsoleCallback
from xenon.engine.context import AgentContext
from xenon.harness.llm import ModelConfig, build_llm_loop_strategy
from xenon.harness.plan import parse_plan
from xenon.harness.runner import run_task
from xenon.kernel.contracts import CallTool, Capability, TaskMode, ToolIntent
from xenon.kernel.policies import PermissionMode
from xenon.kernel.strategies import LoopProfile
from xenon.ledger.events import EventKind, EventSource, canonical_json
from xenon.ledger.store import Ledger
from xenon.modes import MODE_CATALOG
from xenon.repl.execution_policy import ExecutionLevel
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.runtime_adapter import (
    KernelAdapterError,
    KernelApprovalProvider,
    ReplEventSink,
    RuntimeAdapter,
    model_config_for,
    permission_mode_for,
    resolve_max_steps,
    resume_kernel_plan,
    run_kernel_turn,
    should_route_to_kernel,
)


class FakeClient:
    """Scripted model.

    The default loop is plan-first, so a plan call now precedes every tool
    script.  Answering it here keeps each test's script about the behaviour it
    is actually testing, instead of prepending a plan to eight lists.
    """

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.last_answer = ""

    def __call__(self, model_id, messages, **kwargs):
        texts = [
            str(item.get("content") or "")
            for item in messages
            if isinstance(item, dict)
        ]
        if any("Return exactly one JSON plan." in text for text in texts):
            if self.responses and '"steps"' in self.responses[0]:
                return self.responses.pop(0)
            return _plan_for_actions(max(1, len(self.responses)))
        if any("JSON step review" in text for text in texts):
            # A reviewer that finds nothing to fix.
            return json.dumps({"review": None})
        if any("Return exactly one JSON reflection" in text for text in texts):
            return json.dumps({"reflect": None})
        if self.responses:
            item = self.responses.pop(0)
            try:
                self.last_answer = str(json.loads(item).get("answer") or "")
            except (ValueError, AttributeError):
                pass
            return item
        # A step close and the finisher may both ask once more than the test
        # scripted.  The finisher synthesizes from the working set, so it
        # repeats the last step answer rather than inventing a new one.
        return json.dumps(
            {
                "action": "finish",
                "answer": self.last_answer or "done",
                "claims": [],
            }
        )


def _responses() -> list[str]:
    plan = _plan_dict()
    files = plan["steps"][0]["params"]["files"]
    return [
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
                    {"kind": "file_changed", "target": "calc/add.py"},
                    {"kind": "test_passed", "target": "tests/test_add.py"},
                ],
            }
        ),
    ]


def _plan_for_actions(count: int) -> str:
    """A plan with one free-form step per scripted action.

    The default loop is plan-first, so a scripted model must answer the plan
    call before it gets to the actions a test cares about.  One step per action
    keeps the script order and lets each step close on its own receipt.
    """

    return json.dumps(
        {
            "objective": "scripted",
            "analysis": "scripted",
            "steps": [
                {"id": index + 1, "kind": "execute", "expects": []}
                for index in range(max(1, count))
            ],
        }
    )


class FakeStatus:
    def __init__(self) -> None:
        self.tools = 0
        self.kernel_snapshots: list[dict | None] = []

    def add_tool_call(self) -> None:
        self.tools += 1

    def set_kernel_status(self, snapshot: dict | None) -> None:
        self.kernel_snapshots.append(snapshot)


class FakeCtx:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str | None]] = []

    def add_assistant_message(self, text: str, *, model_used=None) -> None:
        self.messages.append((text, model_used))


class FakeAgentContext:
    def __init__(self) -> None:
        self.values: dict[str, object] = {"_task_state": {"active": True}}

    def set(self, key: str, value: object) -> None:
        self.values[key] = value


class FakeRepl:
    def __init__(self, root: Path, registry: ModelRegistry) -> None:
        self.project_ctx = SimpleNamespace(root=root, working_dir=root)
        self.registry = registry
        self.console = Console(file=io.StringIO(), force_terminal=False)
        self.status_bar = FakeStatus()
        self.ctx_mgr = FakeCtx()
        self.agent_context = FakeAgentContext()
        self._show_thinking = False
        self._captured_log = ""
        self._last_thinking_panel = None
        self.finished: list[tuple[str, str]] = []
        self.rendered: list[tuple[str, str]] = []
        self.events: list[tuple[str, dict]] = []
        self.confirm_calls: list[tuple[str, dict, str]] = []
        self._turn_tree: Any = None

    def _confirm_tool(self, tool, params, risk, *, from_child=False):
        self.confirm_calls.append((tool, dict(params), risk))
        return False, "denied by test"

    def _make_callback(self):
        return ConsoleCallback(verbose=False)

    def _start_log_capture(self) -> None:
        pass

    def _stop_log_capture(self) -> str:
        return ""

    def _finish_turn_node(
        self, status, *, engine="", reasons=None, panel=None, metrics=None
    ):
        self.finished.append((status, engine))

    def _track_session_files(self, panel) -> None:
        pass

    def _render_assistant_text(self, text, *, title="", **_kwargs) -> None:
        self.rendered.append((title, text))

    def _record_event(self, event_type, **data) -> None:
        self.events.append((event_type, data))


def test_repl_reads_kernel_runtime_env(monkeypatch) -> None:
    from xenon.repl.repl import REPL

    monkeypatch.setenv("XENON_RUNTIME", "kernel")
    repl = REPL(streaming=False)
    assert repl._runtime_mode == "kernel"


def test_repl_defaults_to_kernel(monkeypatch) -> None:
    from xenon.repl.repl import REPL

    monkeypatch.delenv("XENON_RUNTIME", raising=False)
    repl = REPL(streaming=False)
    assert repl._runtime_mode == "kernel"


def test_plan_mode_read_only_routes_kernel(tmp_path: Path, monkeypatch) -> None:
    from xenon.repl.repl import REPL
    from xenon.repl.turn_contract import contract_for_level

    monkeypatch.chdir(tmp_path)
    registry = ModelRegistry()
    registry.add_model("test/fake", "fake")
    registry.assign_role("planner", ["fake"])
    repl = REPL(registry=registry, streaming=False, optimize_prompts=False)
    contract = contract_for_level(ExecutionLevel.READ_ONLY)
    monkeypatch.setattr(
        repl,
        "_resolve_turn_contract",
        lambda *args, **kwargs: (
            contract,
            contract.to_execution_policy(),
            contract.intent,
            None,
        ),
    )
    monkeypatch.setattr(repl, "_has_mcp_tools", lambda: False)
    calls: list[dict] = []
    monkeypatch.setattr(
        "xenon.repl.runtime_adapter.run_kernel_turn",
        lambda *args, **kwargs: calls.append(kwargs),
    )
    repl._plan_mode_active = True
    repl.agent_context.set("_task_state", {"active": True})

    repl._handle_chat("读一下 README 再给方案")

    assert calls
    assert calls[0]["execution_policy"].level == ExecutionLevel.READ_ONLY
    assert calls[0]["plan_only"] is False
    assert repl.agent_context.get("_task_state") is None


def test_kernel_turn_does_not_materialize_legacy_task_state(
    tmp_path: Path, monkeypatch
) -> None:
    from xenon.repl.repl import REPL
    from xenon.repl.turn_contract import build_turn_contract

    monkeypatch.chdir(tmp_path)
    registry = ModelRegistry()
    registry.add_model("test/fake", "fake")
    registry.assign_role("planner", ["fake"])
    repl = REPL(registry=registry, streaming=False, optimize_prompts=False)
    contract = build_turn_contract("写一个 hello.py 文件，内容是 print('hello')")
    assert contract.requires_tools
    monkeypatch.setattr(
        repl,
        "_resolve_turn_contract",
        lambda *args, **kwargs: (
            contract,
            contract.to_execution_policy(),
            contract.intent,
            None,
        ),
    )
    monkeypatch.setattr(repl, "_has_mcp_tools", lambda: False)
    monkeypatch.setattr(
        "xenon.repl.runtime_adapter.should_route_to_kernel",
        lambda **kwargs: True,
    )
    calls: list[bool] = []
    monkeypatch.setattr(
        "xenon.repl.runtime_adapter.run_kernel_turn",
        lambda *args, **kwargs: calls.append(True),
    )
    repl.agent_context.set("_task_state", {"active": True, "objective": "stale"})

    repl._handle_chat("写一个 hello.py 文件，内容是 print('hello')")

    assert calls == [True]
    assert repl.agent_context.get("_task_state") is None


def test_routing_and_permission_mapping() -> None:
    assert should_route_to_kernel(
        runtime_mode="kernel",
        requires_tools=True,
        skill_name=None,
        plan_mode=False,
        pending=None,
        recipe=None,
    )
    assert not should_route_to_kernel(
        runtime_mode="legacy",
        requires_tools=True,
        skill_name=None,
        plan_mode=False,
        pending=None,
        recipe=None,
    )
    assert should_route_to_kernel(
        runtime_mode="kernel",
        requires_tools=True,
        skill_name=None,
        plan_mode=False,
        pending=None,
        recipe=None,
        mode_name="react",
    )
    assert should_route_to_kernel(
        runtime_mode="kernel",
        requires_tools=True,
        skill_name=None,
        plan_mode=False,
        pending=None,
        recipe=None,
        mode_name="plan-execute",
    )
    assert should_route_to_kernel(
        runtime_mode="kernel",
        requires_tools=True,
        skill_name=None,
        plan_mode=True,
        pending=None,
        recipe=None,
        mode_name="react",
    )
    assert should_route_to_kernel(
        runtime_mode="kernel",
        requires_tools=True,
        skill_name=None,
        plan_mode=True,
        plan_deferred_write=True,
        pending=None,
        recipe=None,
        mode_name="react",
    )
    for kernel_mode in (
        "plan-react",
        "reflection",
        "react-reflection",
        "plan-reflection",
    ):
        assert should_route_to_kernel(
            runtime_mode="kernel",
            requires_tools=True,
            skill_name=None,
            plan_mode=False,
            pending=None,
            recipe=None,
            mode_name=kernel_mode,
        ), f"{kernel_mode} 已有 kernel parity"
    for legacy_mode in ("not-a-mode",):
        assert not should_route_to_kernel(
            runtime_mode="kernel",
            requires_tools=True,
            skill_name=None,
            plan_mode=False,
            pending=None,
            recipe=None,
            mode_name=legacy_mode,
        ), f"{legacy_mode} 没有 kernel parity，不能静默改跑 ActFirst"
    # A skill turn is a prompt variant, not another execution model.
    assert should_route_to_kernel(
        runtime_mode="kernel",
        requires_tools=True,
        skill_name="skill",
        plan_mode=False,
        pending=None,
        recipe=None,
    )
    # An external query is a task shape, not another runtime: with the engines
    # gone it runs on the kernel whether or not MCP is available.
    assert should_route_to_kernel(
        runtime_mode="kernel",
        requires_tools=True,
        skill_name=None,
        plan_mode=False,
        pending=None,
        recipe=None,
        external_query=True,
    )
    assert should_route_to_kernel(
        runtime_mode="kernel",
        requires_tools=True,
        skill_name=None,
        plan_mode=False,
        pending=None,
        recipe=None,
        external_query=True,
        mcp_available=True,
    )
    assert permission_mode_for(ExecutionLevel.READ_ONLY) is PermissionMode.READ_ONLY
    assert permission_mode_for(ExecutionLevel.WRITE) is PermissionMode.AUTO_EDIT
    assert permission_mode_for(ExecutionLevel.EXECUTE) is PermissionMode.AUTO_EDIT


def test_model_config_from_registry() -> None:
    registry = ModelRegistry()
    registry.add_model(
        "deepseek/deepseek-v4-pro",
        "ds",
        max_tokens=4096,
        temperature=0.7,
        base_url="http://localhost:1/v1",
        api_key="secret",
    )
    config = model_config_for(registry, ["deepseek/deepseek-v4-pro"])
    assert isinstance(config, ModelConfig)
    assert config.model_id == "deepseek/deepseek-v4-pro"
    assert config.max_tokens == 4096
    assert config.temperature == 0.0
    assert config.base_url == "http://localhost:1/v1"
    assert config.api_key == "secret"


def test_event_sink_projects_receipts_without_content() -> None:
    callback = ConsoleCallback(verbose=False)
    sink = ReplEventSink(callback)
    sink.emit(
        SimpleNamespace(
            kind=SimpleNamespace(value="tool_receipt"),
            payload={
                "receipt": {
                    "tool": "read",
                    "status": "ok",
                    "effects": [
                        {"type": "read", "path": "a.py", "content": "SECRET"}
                    ],
                }
            },
        )
    )
    panel = callback.get_thinking_panel()
    assert panel is not None
    assert panel.tool_call_count == 1
    assert "SECRET" not in repr(panel)


@pytest.mark.parametrize(
    ("kind", "payload", "needle"),
    [
        (
            "step_verified",
            {
                "step_id": 2,
                "status": "unmet",
                "verdicts": [
                    {"obligation_id": "e1", "status": "violated"},
                ],
            },
            "step 2 verification unmet: e1",
        ),
        (
            "step_explored",
            {
                "step_id": 3,
                "decision": "budget_exhausted",
                "calls": 6,
            },
            "步内预算用尽",
        ),
        (
            "plan_reviewed",
            {"decision": "amended", "added_steps": 1},
            "补充了 1 个步骤",
        ),
        (
            "reflection_proposed",
            {"decision": "correction", "capability": "verify"},
            "反思补一次修正",
        ),
    ],
)
def test_event_sink_surfaces_verification_and_review_stages(
    kind: str, payload: dict, needle: str
) -> None:
    """Verification and review stages must reach the panel, not only the ledger."""

    callback = ConsoleCallback(verbose=False)
    sink = ReplEventSink(callback)

    sink.emit(
        SimpleNamespace(kind=SimpleNamespace(value=kind), payload=payload)
    )

    panel = callback.get_thinking_panel()
    assert panel is not None
    rendered = " ".join(
        [*panel.warnings, *(str(step) for step in panel.steps)]
    )
    assert needle in rendered, rendered


class ApprovalRepl:
    def __init__(self, result=True) -> None:
        self.result = result
        self.calls = []

    def _confirm_tool(self, tool, params, risk, *, from_child=False):
        self.calls.append((tool, params, risk))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_kernel_approval_provider_maps_capability_to_repl_gate() -> None:
    repl = ApprovalRepl((True, ""))
    provider = KernelApprovalProvider(repl)
    action = CallTool(
        ToolIntent(
            capability=Capability.SHELL,
            params={"command": "echo hello"},
        )
    )
    assert provider.request(None, action, None) is True
    assert repl.calls == [("bash", {"command": "echo hello"}, "CRITICAL")]

    repl.result = (False, "no")
    assert provider.request(None, action, None) is False
    repl.result = RuntimeError("ui")
    assert provider.request(None, action, None) is False
    assert provider.request(None, SimpleNamespace(), None) is False


def test_a_child_approval_never_remembers_a_scope() -> None:
    """#373: a child's brokered answer is one-shot — even a full "always"
    answer cannot create a session grant, a rule or an allowed-once entry.

    The parent UI may *see* the answer choices; the provider must not pass the
    scope back, because the child's kernel reads ``last_scope`` and would
    otherwise remember on the child's behalf.
    """

    repl = ApprovalRepl((True, ""))
    provider = KernelApprovalProvider(repl)
    # The human (in a real session) chose "session" — but this answer came
    # from a child's brokered request, so the scope must stay empty.
    repl._last_approval_scope = "session"
    request = {
        "capability": "shell",
        "params": {"command": "git log"},
        "risk": "approval",
    }
    approved, reason = provider.approve_request(request)

    assert approved is True
    assert provider.last_scope == ""
    assert provider.last_reason == ""
    assert repl.calls == [("bash", {"command": "git log"}, "CRITICAL")]


def test_a_child_approval_restores_the_provider_scope_afterwards() -> None:
    """Answering a child must not leak ``from_child`` into the next parent
    turn: after ``approve_request`` returns, a normal request remembers again.
    """

    repl = ApprovalRepl((True, ""))
    repl._last_approval_scope = "scoped"
    provider = KernelApprovalProvider(repl)
    request = {
        "capability": "shell",
        "params": {"command": "git log"},
    }
    provider.approve_request(request)

    action = CallTool(
        ToolIntent(
            capability=Capability.SHELL,
            params={"command": "git log"},
        )
    )
    provider.request(None, action, None)
    assert provider.last_scope == "scoped"


class FakeMCPRegistry:
    clients = {"github": object()}
    tool_map = {"github:search": ("github", {"name": "search"})}

    def has_pending_servers(self) -> bool:
        return False


def test_runtime_adapter_selects_core_mcp_profile(tmp_path: Path) -> None:
    registry = ModelRegistry()
    registry.add_model("test/fake", "fake")
    repl = FakeRepl(tmp_path, registry)
    repl._mcp_registry = FakeMCPRegistry()
    repl.agent_context = AgentContext(
        {"_mcp_registry": repl._mcp_registry, "_task_state": None}
    )
    adapter = RuntimeAdapter.from_repl(
        repl, model_ids=["test/fake"], level=ExecutionLevel.EXECUTE
    )
    assert adapter.catalog_profile == "core+mcp"
    assert adapter.gateway_context is repl.agent_context
    assert adapter.mcp_registry is repl._mcp_registry
    assert adapter.allow_approval is True

    read_only = RuntimeAdapter.from_repl(
        repl, model_ids=["test/fake"], level=ExecutionLevel.READ_ONLY
    )
    assert read_only.permission is PermissionMode.READ_ONLY
    assert read_only.allow_approval is True
    assert read_only.catalog_profile == "core+mcp"


def test_runtime_adapter_offers_an_approval_channel_outside_read_only(
    tmp_path: Path,
) -> None:
    """§8.1: auto-edit may ask before running a command.

    The channel used to exist only at EXECUTE, so ``accept_edits`` (AUTO_EDIT)
    denied a pytest call outright while ``default`` (SCOPED) prompted for the
    same call — the stricter-sounding mode was the permissive one.
    """

    registry = ModelRegistry()
    registry.add_model("test/fake", "fake")
    repl = FakeRepl(tmp_path, registry)
    write = RuntimeAdapter.from_repl(
        repl, model_ids=["test/fake"], level=ExecutionLevel.WRITE
    )
    execute = RuntimeAdapter.from_repl(
        repl, model_ids=["test/fake"], level=ExecutionLevel.EXECUTE
    )
    read = RuntimeAdapter.from_repl(
        repl, model_ids=["test/fake"], level=ExecutionLevel.READ_ONLY
    )
    assert write.allow_approval is True
    assert execute.allow_approval is True
    assert read.allow_approval is False
    assert execute.approval is not None


def test_runtime_adapter_defaults_to_no_global_step_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single loop brakes per step and per round, not with a 24-call wall."""

    monkeypatch.delenv("XENON_MAX_STEPS", raising=False)
    registry = ModelRegistry()
    registry.add_model("test/fake", "fake")
    repl = FakeRepl(tmp_path, registry)

    adapter = RuntimeAdapter.from_repl(
        repl, model_ids=["test/fake"], level=ExecutionLevel.EXECUTE
    )
    assert adapter.max_steps == 0
    assert resolve_max_steps(None) == 0
    assert resolve_max_steps(12) == 12

    monkeypatch.setenv("XENON_MAX_STEPS", "40")
    assert resolve_max_steps(None) == 40
    capped = RuntimeAdapter.from_repl(
        repl, model_ids=["test/fake"], level=ExecutionLevel.EXECUTE
    )
    assert capped.max_steps == 40

    monkeypatch.setenv("XENON_MAX_STEPS", "not-a-number")
    assert resolve_max_steps(None) == 0

    # /max-steps is a per-session override that wins over the environment.
    repl._max_steps = 7
    overridden = RuntimeAdapter.from_repl(
        repl, model_ids=["test/fake"], level=ExecutionLevel.EXECUTE
    )
    assert overridden.max_steps == 7


def test_runtime_adapter_resolves_a_loop_profile_per_mode(tmp_path: Path) -> None:
    registry = ModelRegistry()
    registry.add_model("test/fake", "fake")
    repl = FakeRepl(tmp_path, registry)

    # Every catalog mode now resolves to a loop profile; there is no per-mode
    # strategy class and no legacy-only mode left.
    for mode in MODE_CATALOG:
        registry.set_mode(mode)
        adapter = RuntimeAdapter.from_repl(
            repl, model_ids=["test/fake"], level=ExecutionLevel.EXECUTE
        )
        profile = adapter.strategy_profile()
        assert profile is not None, mode

    registry.set_mode("plan-execute")
    plan = RuntimeAdapter.from_repl(
        repl, model_ids=["test/fake"], level=ExecutionLevel.EXECUTE
    ).strategy_profile()
    assert plan is not None and plan.plan and plan.follow_plan

    registry.set_mode("plan-reflection")
    reviewed = RuntimeAdapter.from_repl(
        repl, model_ids=["test/fake"], level=ExecutionLevel.EXECUTE
    ).strategy_profile()
    assert reviewed is not None
    assert reviewed.plan_review and reviewed.step_reflect

    # Unknown modes fail closed instead of falling back to the bare react phase.
    unknown = RuntimeAdapter(
        workspace=tmp_path,
        model_config=ModelConfig(model_id="test/fake"),
        permission=PermissionMode.AUTO_EDIT,
        strategy_mode="no-such-mode",
    )
    assert unknown.strategy_profile() is None
    with pytest.raises(KernelAdapterError, match="unknown kernel strategy"):
        unknown.run("read README.md", task_id="unknown-mode")


def test_runtime_adapter_runs_real_workspace(tmp_path: Path) -> None:
    _repo(tmp_path)
    adapter = RuntimeAdapter(
        workspace=tmp_path,
        model_config=ModelConfig(model_id="test/fake"),
        ledger_dir=tmp_path.parent / f"{tmp_path.name}-ledger",
        client=FakeClient(_responses()),
    )
    run = adapter.run("add a function and test it", task_id="adapter-1")
    assert run.result.accepted is True
    assert run.result.finalize_sha
    assert (tmp_path / "calc" / "add.py").exists()
    assert run.ledger_path.exists()
    assert run.ledger_path.with_suffix(".result.json").exists()


def test_run_kernel_turn_renders_and_updates_repl(tmp_path: Path) -> None:
    _repo(tmp_path)
    registry = ModelRegistry()
    registry.add_model("test/fake", "fake")
    repl = FakeRepl(tmp_path, registry)
    run = run_kernel_turn(
        repl,
        objective="add a function and test it",
        user_input="add a function and test it",
        model_ids=["test/fake"],
        execution_policy=SimpleNamespace(level=ExecutionLevel.WRITE),
        callback=ConsoleCallback(verbose=False),
        client=FakeClient(_responses()),
    )
    assert run is not None
    assert run.result.accepted is True
    assert repl.finished == [("passed", "kernel")]
    assert repl.rendered
    # The title now carries the evidence label, not a bare "Kernel".
    assert repl.rendered[-1][0].startswith("Kernel · ")
    assert "交付: commit" in repl.rendered[-1][1]
    assert repl.ctx_mgr.messages
    assert "交付: commit" in repl.ctx_mgr.messages[-1][0]
    assert repl.ctx_mgr.messages[-1][1] == "test/fake"
    assert repl.events[-1][0] == "runtime/kernel"
    assert repl.events[-1][1]["accepted"] is True
    assert repl.agent_context.values["_task_state"] is None


def test_run_kernel_turn_records_raw_user_objective(tmp_path: Path) -> None:
    _repo(tmp_path)
    registry = ModelRegistry()
    registry.add_model("test/fake", "fake")
    repl = FakeRepl(tmp_path, registry)
    run = run_kernel_turn(
        repl,
        objective="## transformed prompt ##",
        user_input="add a function and test it",
        model_ids=["test/fake"],
        execution_policy=SimpleNamespace(level=ExecutionLevel.WRITE),
        callback=ConsoleCallback(verbose=False),
        client=FakeClient(_responses()),
    )
    assert run is not None
    started = next(
        event
        for event in Ledger(run.ledger_path, run.task_id).read()
        if event.kind.value == "task_started"
    )
    assert started.payload["objective"] == "## transformed prompt ##"
    assert started.payload["user_objective"] == "add a function and test it"


def test_model_config_errors_and_defaults() -> None:
    from xenon.repl.runtime_adapter import KernelAdapterError

    with pytest.raises(KernelAdapterError, match="no model"):
        model_config_for(None, [])
    config = model_config_for(None, ["test/unconfigured"])
    assert config.model_id == "test/unconfigured"
    assert config.max_tokens == 8192


def test_runtime_adapter_rejects_missing_workspace(tmp_path: Path) -> None:
    from xenon.repl.runtime_adapter import KernelAdapterError

    with pytest.raises(KernelAdapterError, match="not a directory"):
        RuntimeAdapter(
            workspace=tmp_path / "missing",
            model_config=ModelConfig(model_id="test/fake"),
        )


class RecordingCallback:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def on_think(self, text) -> None:
        self.calls.append(("think", text))

    def on_act(self, action, params) -> None:
        self.calls.append(("act", (action, params)))

    def on_observe(self, observation) -> None:
        self.calls.append(("observe", observation))

    def on_error(self, error) -> None:
        self.calls.append(("error", error))

    def on_warning(self, warning) -> None:
        self.calls.append(("warning", warning))


def _event(kind: str, payload: dict) -> SimpleNamespace:
    return SimpleNamespace(kind=SimpleNamespace(value=kind), payload=payload)


def test_event_sink_projects_kernel_write_paths() -> None:
    callback = RecordingCallback()
    sink = ReplEventSink(callback)
    sink.emit(
        _event(
            "tool_receipt",
            {
                "receipt": {
                    "tool": "apply_patch",
                    "status": "ok",
                    "effects": [
                        {
                            "type": "fs_write",
                            "paths": ["pkg/a.py"],
                            "created_paths": ["pkg/a.py"],
                        }
                    ],
                }
            },
        )
    )

    kind, payload = callback.calls[0]
    assert kind == "act"
    tool, params = payload
    assert tool == "apply_patch"
    assert params["file_paths"] == ["pkg/a.py"]
    assert params["created_paths"] == ["pkg/a.py"]


def test_event_sink_handles_all_event_kinds() -> None:
    callback = RecordingCallback()
    sink = ReplEventSink(callback)
    sink.emit(_event("thought", {"text": "plan"}))
    sink.emit(_event("tool_receipt", {"receipt": "not-a-mapping"}))
    sink.emit(_event("tool_receipt", {
        "receipt": {
            "tool": "read",
            "status": "failed",
            "effects": [{"type": "read"}],
            "error": "boom",
        }
    }))
    sink.emit(_event("policy_decision", {"kind": "veto", "reason": "no"}))
    sink.emit(_event("policy_decision", {"kind": "allow", "reason": "ok"}))
    sink.emit(_event("debt_opened", {
        "kind": "file_changed", "target": "a.py", "reason": "missing"
    }))
    sink.emit(_event("user_question", {"question": "which one?"}))
    kinds = [kind for kind, _ in callback.calls]
    assert kinds == [
        "think", "act", "observe", "error", "warning", "warning", "warning"
    ]
    assert "boom" in str(callback.calls[2][1])


def test_result_summary_branches_and_blocked_rendering(tmp_path: Path) -> None:
    import xenon.repl.runtime_adapter as adapter
    from xenon.kernel.contracts import (
        Debt,
        EvidenceStatus,
        Obligation,
        PropositionKind,
        TaskPhase,
        TaskResult,
    )

    ledger = tmp_path / "ledger.jsonl"
    accepted = TaskResult(
        task_id="t",
        accepted=True,
        phase=TaskPhase.FINALIZED,
        answer="",
        evidence_status=EvidenceStatus.VERIFIED,
    )
    assert adapter._result_summary(accepted) == "任务已完成并交付。"
    blocked = TaskResult(
        task_id="t",
        accepted=False,
        phase=TaskPhase.BLOCKED,
        answer="",
        debt=(
            Debt(
                obligation=Obligation(
                    id="o1",
                    kind=PropositionKind.FILE_CHANGED,
                    target="a.py",
                ),
                reason="missing evidence",
            ),
        ),
        evidence_status=EvidenceStatus.UNVERIFIED,
    )
    assert "missing evidence" in adapter._result_summary(blocked)
    blocked_no_debt = TaskResult(
        task_id="t",
        accepted=False,
        phase=TaskPhase.ABORTED,
        answer="",
        evidence_status=EvidenceStatus.UNVERIFIED,
    )
    assert "aborted" in adapter._result_summary(blocked_no_debt)

    repl = FakeRepl(tmp_path, ModelRegistry())
    repl._show_thinking = True
    repl._captured_log = "captured kernel log"
    adapter._render_kernel_result(
        repl,
        adapter.HarnessRun(
            result=blocked,
            task_id="t",
            ledger_path=ledger,
        ),
        ConsoleCallback(verbose=False),
        model_id="test/fake",
    )
    # A stopped run is an execution brake; a run with gaps is labelled.
    assert repl.rendered[-1][0] == "Kernel · 已停止"
    assert repl.finished == [("degraded", "kernel")]
    assert repl.ctx_mgr.messages
    assert repl.agent_context.values["_task_state"] is None


def test_runtime_adapter_plan_only_stops_after_plan(tmp_path: Path) -> None:
    _repo(tmp_path)
    client = FakeClient([json.dumps(_plan_dict())])
    adapter = RuntimeAdapter(
        workspace=tmp_path,
        model_config=ModelConfig(model_id="test/fake"),
        ledger_dir=tmp_path.parent / f"{tmp_path.name}-plan-ledger",
        finalize=False,
        client=client,
        strategy_mode="react",
        plan_only=True,
    )

    run = adapter.run("add a function and test it", task_id="adapter-plan-only")

    assert run.result.phase.value == "planned"
    assert run.result.accepted is False
    assert "edit, verify, finalize" in run.result.answer
    events = Ledger(run.ledger_path, "adapter-plan-only").read()
    assert any(event.kind.value == "plan_accepted" for event in events)
    assert not any(event.kind.value == "tool_receipt" for event in events)
    assert not any(event.kind.value == "step_started" for event in events)
    assert client.responses == []


def test_runtime_adapter_resume_executes_approved_plan(tmp_path: Path) -> None:
    _repo(tmp_path)
    plan = parse_plan(_plan_dict())
    ledger_dir = tmp_path.parent / f"{tmp_path.name}-resume-adapter-ledger"
    planned = run_task(
        plan.objective,
        strategy=build_llm_loop_strategy(
            ModelConfig(model_id="test/fake"),
            LoopProfile(plan=True, step_scope=True, follow_plan=True),
            client=FakeClient([json.dumps(_plan_dict())]),
        ),
        workspace=tmp_path,
        ledger_dir=ledger_dir,
        task_id="resume-adapter",
        finalize=False,
        mode=TaskMode.PLAN,
    )
    assert planned.result.phase.value == "planned"
    accepted = Ledger(planned.ledger_path, "resume-adapter").read(
        EventKind.PLAN_ACCEPTED
    )[-1]
    digest = hashlib.sha256(
        canonical_json(accepted.payload["plan"]).encode("utf-8")
    ).hexdigest()
    adapter = RuntimeAdapter(
        workspace=tmp_path,
        model_config=ModelConfig(model_id="test/fake"),
        ledger_dir=ledger_dir,
        # §8.1: the plan runs a verify command, which needs an explicit grant.
        permission=PermissionMode.BYPASS,
        finalize=False,
        client=FakeClient(
            [json.dumps({"action": "finish", "answer": "done"})]
        ),
    )

    run = adapter.resume(
        task_id="resume-adapter",
        plan=plan.proposal,
        plan_digest=digest,
        approval_id="approval-1",
        objective=plan.objective,
    )

    assert run.result.accepted is True, run.result.debt
    events = Ledger(run.ledger_path, "resume-adapter").read()
    assert len([e for e in events if e.kind is EventKind.TASK_STARTED]) == 1
    assert len([e for e in events if e.kind is EventKind.PLAN_ACCEPTED]) == 1
    assert any(e.kind is EventKind.PLAN_APPROVED for e in events)
    assert any(e.kind is EventKind.TOOL_RECEIPT for e in events)


def test_resume_kernel_plan_renders_execution_result(tmp_path: Path) -> None:
    _repo(tmp_path)
    plan = parse_plan(_plan_dict())
    ledger_dir = tmp_path.parent / f"{tmp_path.name}-resume-render-ledger"
    planned = run_task(
        plan.objective,
        strategy=build_llm_loop_strategy(
            ModelConfig(model_id="test/fake"),
            LoopProfile(plan=True, step_scope=True, follow_plan=True),
            client=FakeClient([json.dumps(_plan_dict())]),
        ),
        workspace=tmp_path,
        ledger_dir=ledger_dir,
        task_id="resume-render",
        finalize=False,
        mode=TaskMode.PLAN,
    )
    accepted = Ledger(planned.ledger_path, "resume-render").read(
        EventKind.PLAN_ACCEPTED
    )[-1]
    digest = hashlib.sha256(
        canonical_json(accepted.payload["plan"]).encode("utf-8")
    ).hexdigest()
    registry = ModelRegistry()
    registry.add_model("test/fake", "fake")
    repl = FakeRepl(tmp_path, registry)
    # The resumed plan executes a verify command, and §8.1 asks before that.
    # This fake stands in for the human who answers the panel.
    repl._confirm_tool = lambda tool, params, risk, *, from_child=False: (True, "")

    run = resume_kernel_plan(
        repl,
        task_id="resume-render",
        plan=plan.proposal,
        plan_digest=digest,
        approval_id="approval-1",
        objective=plan.objective,
        client=FakeClient(
            [json.dumps({"action": "finish", "answer": "done"})]
        ),
        ledger_dir=ledger_dir,
    )

    assert run is not None, repl.ctx_mgr.messages
    assert run.result.accepted is True, run.result.debt
    assert repl.rendered[-1][0].startswith("Kernel · ")
    assert repl.finished == [("passed", "kernel")]


def test_resume_kernel_plan_reports_invalid_digest(tmp_path: Path) -> None:
    _repo(tmp_path)
    plan = parse_plan(_plan_dict())
    registry = ModelRegistry()
    registry.add_model("test/fake", "fake")
    repl = FakeRepl(tmp_path, registry)

    run = resume_kernel_plan(
        repl,
        task_id="missing-plan",
        plan=plan.proposal,
        plan_digest="not-the-plan",
        approval_id="approval-1",
        objective=plan.objective,
        client=FakeClient([]),
    )

    assert run is None
    assert "Kernel plan execution error" in repl.ctx_mgr.messages[-1][0]


def test_run_kernel_turn_renders_planned_as_pending_approval(
    tmp_path: Path,
) -> None:
    _repo(tmp_path)
    registry = ModelRegistry()
    registry.add_model("test/fake", "fake")
    repl = FakeRepl(tmp_path, registry)
    client = FakeClient([json.dumps(_plan_dict())])

    run = run_kernel_turn(
        repl,
        objective="add a function and test it",
        user_input="add a function and test it",
        model_ids=["test/fake"],
        execution_policy=SimpleNamespace(level=ExecutionLevel.READ_ONLY),
        callback=ConsoleCallback(verbose=False),
        client=client,
        plan_only=True,
    )

    assert run is not None
    assert run.result.phase.value == "planned"
    assert repl.finished == [("pending-approval", "kernel")]
    assert repl.rendered[-1][0] == "Kernel · plan"
    assert "Kernel · blocked" not in repl.rendered[-1][0]
    assert repl.agent_context.values["_task_state"] is None


def test_run_kernel_turn_denies_shell_approval(tmp_path: Path) -> None:
    _repo(tmp_path)
    registry = ModelRegistry()
    registry.add_model("test/fake", "fake")
    repl = FakeRepl(tmp_path, registry)
    client = FakeClient(
        [
            json.dumps(
                {
                    "action": "call_tool",
                    "capability": "shell",
                    "params": {"command": "echo hello"},
                }
            ),
            # Shell proposal, step close, then the finisher's answer: the
            # denied shell does not prove the step, so closing it asks once.
            json.dumps({"action": "think", "text": "the shell was refused"}),
            json.dumps({"action": "finish", "answer": "echo was not run", "claims": []}),
        ]
    )
    run = run_kernel_turn(
        repl,
        objective="run echo",
        user_input="run echo",
        model_ids=["test/fake"],
        execution_policy=SimpleNamespace(level=ExecutionLevel.EXECUTE),
        callback=ConsoleCallback(verbose=False),
        client=client,
    )
    assert run is not None
    # The refused shell never ran, but the turn completes with its answer;
    # the gap is the label, not a stop.
    assert run.result.accepted is True
    assert run.result.answer == "echo was not run"
    assert run.result.phase.value == "finalized"
    assert run.result.evidence_status.value in {"partial", "unverified"}
    assert repl.confirm_calls == [
        ("bash", {"command": "echo hello"}, "CRITICAL")
    ]
    assert repl.finished == [("passed", "kernel")]
    assert repl.agent_context.values["_task_state"] is None


def test_run_kernel_turn_reports_adapter_exception(tmp_path: Path, monkeypatch) -> None:
    import xenon.repl.runtime_adapter as adapter

    registry = ModelRegistry()
    registry.add_model("test/fake", "fake")
    repl = FakeRepl(tmp_path, registry)

    def boom(self, *args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(adapter.RuntimeAdapter, "run", boom)
    result = adapter.run_kernel_turn(
        repl,
        objective="do work",
        user_input="do work",
        model_ids=["test/fake"],
        execution_policy=SimpleNamespace(level=ExecutionLevel.WRITE),
        callback=ConsoleCallback(verbose=False),
    )
    assert result is None
    assert repl.finished == [("interrupted", "kernel")]
    assert "boom" in repl.ctx_mgr.messages[-1][0]
    assert repl.agent_context.values["_task_state"] is None


class LazyMCPRegistry:
    clients = {"github": object()}
    tool_map: dict[str, tuple[str, dict]] = {}

    def __init__(self) -> None:
        self.discovered = False
        self.calls: list[tuple[str, dict]] = []

    def has_pending_servers(self) -> bool:
        return True

    def discover_tools(self) -> None:
        self.discovered = True
        self.tool_map = {
            "github:search_repositories": (
                "github",
                {"name": "search_repositories"},
            )
        }

    def call_tool(self, tool_name: str, arguments: dict) -> dict:
        self.calls.append((tool_name, arguments))
        return {
            "content": [
                {"type": "text", "text": "mode result: xianyu-sheng/Xenon"}
            ]
        }


class AdapterApproval:
    def __init__(self) -> None:
        self.calls = 0

    def request(self, view, action, decision) -> bool:
        self.calls += 1
        return True


def test_runtime_adapter_discovers_mcp_tools_and_runs_remote(
    tmp_path: Path,
) -> None:
    registry = LazyMCPRegistry()
    context = AgentContext({"_mcp_registry": registry})
    client = FakeClient(
        [
            json.dumps(
                {
                    "action": "call_tool",
                    "capability": "remote",
                    "params": {
                        "tool_name": "github:search_repositories",
                        "tool_args": {"query": "xenon"},
                    },
                }
            ),
            json.dumps({"action": "finish", "answer": "found"}),
        ]
    )
    approval = AdapterApproval()
    adapter = RuntimeAdapter(
        workspace=tmp_path,
        model_config=ModelConfig(model_id="test/fake"),
        ledger_dir=tmp_path.parent / f"{tmp_path.name}-adapter-ledger",
        finalize=False,
        client=client,
        approval=approval,
        allow_approval=True,
        catalog_profile="core+mcp",
        gateway_context=context,
        mcp_registry=registry,
    )
    run = adapter.run("query github", task_id="adapter-mcp")
    assert registry.discovered is True
    assert registry.calls == [
        ("github:search_repositories", {"query": "xenon"})
    ]
    assert approval.calls == 1
    assert run.result.accepted is True
    assert "mode result" in json.dumps(
        [
            event.payload
            for event in Ledger(run.ledger_path, "adapter-mcp").read()
            if event.kind.value == "tool_receipt"
        ],
        ensure_ascii=False,
    )


class SelectionMCPRegistry(LazyMCPRegistry):
    def discover_tools(self) -> None:
        self.discovered = True
        self.tool_map = {
            "github:search_repositories": (
                "github",
                {
                    "name": "search_repositories",
                    "description": "Search GitHub repositories.",
                },
            ),
            "github:create_issue": (
                "github",
                {
                    "name": "create_issue",
                    "description": "Create an issue in a repository.",
                },
            ),
            "weather:forecast": (
                "weather",
                {
                    "name": "forecast",
                    "description": "Get a weather forecast.",
                },
            ),
        }


def test_runtime_adapter_injects_only_selected_mcp_tools(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("XENON_MCP_TOOLS", raising=False)
    monkeypatch.setenv("XENON_MCP_TOOL_LIMIT", "1")
    registry = SelectionMCPRegistry()
    context = AgentContext({"_mcp_registry": registry})
    client = FakeClient(
        [
            json.dumps(
                {
                    "action": "call_tool",
                    "capability": "remote",
                    "params": {
                        "tool_name": "github:search_repositories",
                        "tool_args": {"query": "xenon"},
                    },
                }
            ),
            json.dumps({"action": "finish", "answer": "found"}),
        ]
    )
    adapter = RuntimeAdapter(
        workspace=tmp_path,
        model_config=ModelConfig(model_id="test/fake"),
        ledger_dir=tmp_path.parent / f"{tmp_path.name}-adapter-ledger",
        finalize=False,
        client=client,
        approval=AdapterApproval(),
        allow_approval=True,
        catalog_profile="core+mcp",
        gateway_context=context,
        mcp_registry=registry,
    )
    adapter.run("search repositories for Xenon", task_id="adapter-selected")
    events = Ledger(
        tmp_path.parent / f"{tmp_path.name}-adapter-ledger" / "adapter-selected.jsonl",
        "adapter-selected",
    ).read()
    started = next(event for event in events if event.kind.value == "task_started")
    constraints = " ".join(started.payload.get("constraints") or ())
    assert "github:search_repositories" in constraints
    assert "github:create_issue" not in constraints
    assert "weather:forecast" not in constraints


def test_runtime_adapter_mcp_allowlist_overrides_selection(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XENON_MCP_TOOLS", "weather:forecast")
    monkeypatch.setenv("XENON_MCP_TOOL_LIMIT", "16")
    registry = SelectionMCPRegistry()
    context = AgentContext({"_mcp_registry": registry})
    client = FakeClient(
        [
            json.dumps(
                {
                    "action": "call_tool",
                    "capability": "remote",
                    "params": {
                        "tool_name": "weather:forecast",
                        "tool_args": {"city": "Shanghai"},
                    },
                }
            ),
            json.dumps({"action": "finish", "answer": "forecast"}),
        ]
    )
    adapter = RuntimeAdapter(
        workspace=tmp_path,
        model_config=ModelConfig(model_id="test/fake"),
        ledger_dir=tmp_path.parent / f"{tmp_path.name}-adapter-ledger",
        finalize=False,
        client=client,
        approval=AdapterApproval(),
        allow_approval=True,
        catalog_profile="core+mcp",
        gateway_context=context,
        mcp_registry=registry,
    )
    run = adapter.run("search repositories", task_id="adapter-allowlist")
    events = Ledger(run.ledger_path, "adapter-allowlist").read()
    started = next(event for event in events if event.kind.value == "task_started")
    constraints = " ".join(started.payload.get("constraints") or ())
    assert "weather:forecast" in constraints
    assert "github:search_repositories" not in constraints
    assert registry.calls == [("weather:forecast", {"city": "Shanghai"})]


def test_runtime_adapter_runs_plan_execute_with_llm_planner(
    tmp_path: Path,
) -> None:
    _repo(tmp_path)
    client = FakeClient([
        json.dumps(_plan_dict()),
        json.dumps({"action": "finish", "answer": "added add() and its test"}),
    ])
    adapter = RuntimeAdapter(
        workspace=tmp_path,
        model_config=ModelConfig(model_id="test/fake"),
        ledger_dir=tmp_path.parent / f"{tmp_path.name}-adapter-ledger",
        permission=PermissionMode.BYPASS,
        finalize=True,
        client=client,
        strategy_mode="plan-execute",
    )
    run = adapter.run("add a function and test it", task_id="adapter-plan")

    assert run.result.accepted is True, run.result.debt
    assert run.result.answer == "added add() and its test"
    assert run.result.finalize_sha
    events = Ledger(run.ledger_path, "adapter-plan").read()
    assert any(event.kind.value == "plan_accepted" for event in events)
    assert any(event.kind.value == "tool_receipt" for event in events)


class GrantingApproval(AdapterApproval):
    """Approves, and claims the human chose "session" — like the real UI."""

    def __init__(self) -> None:
        super().__init__()
        self.last_scope = "session"
        self.last_reason = "session grant: shell"


def _shell_turn(responses: list[str], tmp_path: Path, name: str) -> tuple[Any, GrantingApproval]:
    client = FakeClient(responses)
    approval = GrantingApproval()
    adapter = RuntimeAdapter(
        workspace=tmp_path,
        model_config=ModelConfig(model_id="test/fake"),
        ledger_dir=tmp_path.parent / f"{name}-ledger",
        finalize=False,
        client=client,
        approval=approval,
        allow_approval=True,
    )
    return adapter, approval


def test_session_grants_flow_across_two_adapter_runs(tmp_path: Path) -> None:
    """#373 ask-once: turn 1 approves and remembers; turn 2 never asks.

    Mirrors what ``run_kernel_turn`` wires: the front-end book records the
    answer when it is given and feeds the next task's ``session_grants``.
    """

    from xenon.kernel.session_grants import GrantBook
    from xenon.ledger.events import EventKind

    book = GrantBook(workspace=str(tmp_path))
    script = [
        json.dumps(
            {
                "action": "call_tool",
                "capability": "shell",
                "params": {"command": "git log"},
            }
        ),
        json.dumps({"action": "finish", "answer": "done"}),
    ]

    # Turn 1: the call needs approval; the human answers "session" — the
    # front-end book records it (the panel is the single owner of memory).
    from xenon.kernel.contracts import Capability
    from xenon.kernel.session_grants import ALLOW, grant_from_choice

    adapter1, approval1 = _shell_turn(script, tmp_path, "t1")
    adapter1.session_grants = tuple(book.active(str(tmp_path)))
    run1 = adapter1.run("do it", task_id="grant-turn-1")
    assert approval1.calls == 1
    planned = grant_from_choice(
        ALLOW,
        capability=Capability.SHELL,
        targets=("git log",),
        workspace=str(tmp_path),
    )
    assert planned is not None
    book.add(planned)

    # Design doc §9: the answer is memory-only.  The approval is on the record,
    # but the grant itself is not, so a restart cannot resurrect it.
    events = [
        event
        for event in Ledger(run1.ledger_path, "grant-turn-1").read()
        if event.kind is EventKind.GRANT_ADDED
    ]
    assert not events, "a session answer must not be written to the ledger"

    # Turn 2: the same call must be answered by the grant, not by a prompt.
    adapter2, approval2 = _shell_turn(script, tmp_path, "t2")
    adapter2.session_grants = tuple(book.active(str(tmp_path)))
    run2 = adapter2.run("do it again", task_id="grant-turn-2")
    assert approval2.calls == 0, "the session grant must answer the second call"

    # And the ledger explains why: a policy_decision with a session-grant reason.
    decisions = [
        event
        for event in Ledger(run2.ledger_path, "grant-turn-2").read()
        if event.kind is EventKind.POLICY_DECISION
    ]
    assert decisions
    assert str(decisions[-1].payload.get("reason") or "").startswith("session grant:")


def test_a_grant_in_another_workspace_does_not_answer(tmp_path: Path) -> None:
    """#373 acceptance: switching repositories invalidates session grants."""

    from xenon.kernel.contracts import Capability
    from xenon.kernel.session_grants import ALLOW, GrantBook, grant_from_choice

    other = tmp_path / "elsewhere"
    other.mkdir()
    book = GrantBook(workspace=str(tmp_path))
    grant = grant_from_choice(
        ALLOW,
        capability=Capability.SHELL,
        targets=("git log",),
        workspace=str(tmp_path),
    )
    assert grant is not None
    book.add(grant)

    adapter, approval = _shell_turn(script := [
        json.dumps(
            {
                "action": "call_tool",
                "capability": "shell",
                "params": {"command": "git log"},
            }
        ),
        json.dumps({"action": "finish", "answer": "done"}),
    ], other, "elsewhere")
    adapter.session_grants = tuple(book.active(str(other)))
    adapter.run("do it", task_id="elsewhere-1")
    assert approval.calls == 1, "the other repo must ask again"


class _GrantingRepl(FakeRepl):
    """A FakeRepl whose confirmation answers "session" — the real UI's a key."""

    def _confirm_tool(self, tool, params, risk, *, from_child=False):
        self.confirm_calls.append((tool, dict(params), risk))
        # Single-owner contract: the front-end book records the "a" answer when
        # it is given (the kernel no longer hands a grant back afterwards).
        from xenon.kernel.session_grants import ALLOW, GrantBook, grant_from_choice
        from xenon.repl.permission_map import capability_for_tool

        book = getattr(self, "_grant_book", None)
        if book is None:
            book = GrantBook(workspace=str(self.project_ctx.root))
            self._grant_book = book
        capability = capability_for_tool(tool)
        target = str((params or {}).get("command") or "").strip()
        if capability is not None and target:
            grant = grant_from_choice(
                ALLOW,
                capability=capability,
                targets=(target,),
                workspace=str(self.project_ctx.root),
            )
            if grant is not None:
                book.add(grant)
        self._last_approval_scope = "session"
        return True, ""


def _run_turn(repl, tmp_path: Path, name: str):
    client = FakeClient(
        [
            json.dumps(
                {
                    "action": "call_tool",
                    "capability": "shell",
                    "params": {"command": "echo hello"},
                }
            ),
            json.dumps({"action": "finish", "answer": "done"}),
        ]
    )
    return run_kernel_turn(
        repl,
        objective="run echo hello",
        user_input="run echo hello",
        model_ids=["test/fake"],
        execution_policy=SimpleNamespace(level=ExecutionLevel.EXECUTE),
        callback=ConsoleCallback(verbose=False),
        client=client,
    )


def test_run_kernel_turn_asks_once_across_two_tasks(tmp_path: Path) -> None:
    """§9 acceptance, product path: two REPL tasks, one prompt.

    Turn 1 answers "a" (tool + path, timed); the runtime hands the grant back
    and ``run_kernel_turn`` stores it in the front-end book.  Turn 2 feeds
    ``session_grants`` from that book, so the same call is answered without a
    prompt.
    """

    registry = ModelRegistry()
    registry.add_model("test/fake", "fake")
    repl = _GrantingRepl(tmp_path, registry)

    run1 = _run_turn(repl, tmp_path, "ask-once-1")
    assert run1 is not None and run1.result.accepted is True
    assert len(repl.confirm_calls) == 1

    # The front-end book now holds the decision (wired inside run_kernel_turn).
    book = getattr(repl, "_grant_book", None)
    assert book is not None and len(book) == 1
    (grant,) = list(book)
    assert grant.targets == ("echo hello",), "the grant covers the command shown"

    run2 = _run_turn(repl, tmp_path, "ask-once-2")
    assert run2 is not None and run2.result.accepted is True
    assert len(repl.confirm_calls) == 1, (
        "the second task must be answered by the session grant, not the TTY"
    )


def test_a_new_repl_asks_again_because_nothing_was_persisted(tmp_path: Path) -> None:
    """Design doc §9: memory only, so a restart re-asks.

    A fresh REPL over the same workspace has no ``_grant_book`` entry, and
    nothing on disk restores one — which is the whole point of "不持久化".
    """

    _repo(tmp_path)
    registry = ModelRegistry()
    registry.add_model("test/fake", "fake")

    first = _GrantingRepl(tmp_path, registry)
    _run_turn(first, tmp_path, "restart-1")
    assert len(first.confirm_calls) == 1
    assert len(getattr(first, "_grant_book", ())) == 1

    second = _GrantingRepl(tmp_path, registry)
    _run_turn(second, tmp_path, "restart-2")

    assert len(second.confirm_calls) == 1, (
        "a new process must ask: the answer was never written down"
    )


def test_kernel_status_bar_snapshot_and_cleanup(tmp_path: Path) -> None:
    """#372 E1: the progress hook pushes runtime facts; the finally clears them."""
    from xenon.repl.runtime_adapter import kernel_progress_reporter

    _repo(tmp_path)
    registry = ModelRegistry()
    registry.add_model("test/fake", "fake")
    repl = FakeRepl(tmp_path, registry)

    runtime = SimpleNamespace(
        status_snapshot=lambda: {
            "phase": "tool_call",
            "steps": 3,
            "tokens": 500,
            "stalled_seconds": 9.0,
            "tool_calls": 2,
        }
    )
    repl._active_runtime = runtime
    reporter = kernel_progress_reporter(repl)
    reporter(2.0, 120)

    assert repl.status_bar.kernel_snapshots[-1]["phase"] == "tool_call"
    assert repl.status_bar.kernel_snapshots[-1]["steps"] == 3

    # The turn's finally clears the snapshot so the bar falls back to /mode.
    client = FakeClient([json.dumps({"action": "finish", "answer": "ok"})])
    run = run_kernel_turn(
        repl,
        objective="do it",
        user_input="do it",
        model_ids=["test/fake"],
        execution_policy=SimpleNamespace(level=ExecutionLevel.EXECUTE),
        callback=ConsoleCallback(verbose=False),
        client=client,
    )
    assert run is not None
    assert repl.status_bar.kernel_snapshots[-1] is None


def test_turn_path_from_the_tree_reaches_the_adapter(tmp_path: Path, monkeypatch) -> None:
    """#372 B3: a rewound tree's branch path flows into the adapter run."""
    from xenon.session.tree import TurnTree

    _repo(tmp_path)
    registry = ModelRegistry()
    registry.add_model("test/fake", "fake")
    repl = FakeRepl(tmp_path, registry)
    tree = TurnTree()
    a = tree.append_turn("objective one")
    tree.finish(a, "passed")
    tree.append_turn("objective two")
    tree.move_to(a)  # rewind -> on a side branch
    repl._turn_tree = tree

    seen: dict = {}

    from xenon.kernel.contracts import TaskPhase, TaskResult

    def capture_turn_path(self, *args, **kwargs):
        seen["turn_path"] = tuple(kwargs.get("turn_path") or ())
        return SimpleNamespace(
            ledger_path=tmp_path / "x.ledger",
            task_id="captured",
            result=TaskResult(
                task_id="captured",
                accepted=False,
                phase=TaskPhase.BLOCKED,
                answer="blocked by test",
            ),
        )

    monkeypatch.setattr(
        "xenon.repl.runtime_adapter.RuntimeAdapter.run",
        capture_turn_path,
    )
    run_kernel_turn(
        repl,
        objective="branch task",
        user_input="branch task",
        model_ids=["test/fake"],
        execution_policy=SimpleNamespace(level=ExecutionLevel.EXECUTE),
        callback=ConsoleCallback(verbose=False),
        client=FakeClient([json.dumps({"action": "finish", "answer": "ok"})]),
    )
    assert seen["turn_path"], "a rewound tree must produce a turn path"
    assert any("objective one" in item for item in seen["turn_path"])


def test_progress_hook_tolerates_a_broken_snapshot(tmp_path: Path, monkeypatch) -> None:
    """#372 E1: progress is cosmetic — a bad snapshot must not break the turn."""
    from xenon.repl.runtime_adapter import kernel_progress_reporter

    _repo(tmp_path)
    registry = ModelRegistry()
    registry.add_model("test/fake", "fake")
    repl = FakeRepl(tmp_path, registry)

    def boom():
        raise RuntimeError("snapshot exploded")

    repl._active_runtime = SimpleNamespace(status_snapshot=boom)
    reporter = kernel_progress_reporter(repl)
    reporter(2.0, 100)  # must not raise

    assert repl.status_bar.kernel_snapshots == []


def test_turn_path_tolerates_a_broken_tree(tmp_path: Path, monkeypatch) -> None:
    """#372 B3: a broken tree must not break the turn (cosmetic)."""
    _repo(tmp_path)
    registry = ModelRegistry()
    registry.add_model("test/fake", "fake")
    repl = FakeRepl(tmp_path, registry)

    class BrokenTree:
        def turn_path(self):
            raise RuntimeError("tree exploded")

    repl._turn_tree = BrokenTree()

    seen: dict = {}

    def capture_turn_path(self, *args, **kwargs):
        seen["turn_path"] = tuple(kwargs.get("turn_path") or ())
        from xenon.kernel.contracts import TaskPhase, TaskResult

        return SimpleNamespace(
            ledger_path=tmp_path / "x.ledger",
            task_id="captured",
            result=TaskResult(
                task_id="captured",
                accepted=False,
                phase=TaskPhase.BLOCKED,
                answer="blocked by test",
            ),
        )

    monkeypatch.setattr(
        "xenon.repl.runtime_adapter.RuntimeAdapter.run",
        capture_turn_path,
    )
    run_kernel_turn(
        repl,
        objective="branch task",
        user_input="branch task",
        model_ids=["test/fake"],
        execution_policy=SimpleNamespace(level=ExecutionLevel.EXECUTE),
        callback=ConsoleCallback(verbose=False),
        client=FakeClient([json.dumps({"action": "finish", "answer": "ok"})]),
    )
    assert seen["turn_path"] == ()


def test_a_blocked_turn_that_asked_a_question_says_so(tmp_path: Path) -> None:
    """#402: the panel said "blocked" and swallowed the question.

    Reproduces the dogfood ledger: the model asked for the P0 context, the
    runtime moved to ``blocked``, and the operator saw "任务未交付" with the
    question nowhere on screen.
    """
    from xenon.kernel.contracts import EvidenceStatus, TaskPhase, TaskResult
    from xenon.repl import runtime_adapter as adapter

    ledger = tmp_path / "q.jsonl"
    store = Ledger(ledger, "q")
    store.append(
        EventKind.USER_QUESTION, EventSource.LLM, {"question": "哪个仓库？"}
    )

    waiting = TaskResult(
        task_id="q",
        accepted=False,
        phase=TaskPhase.BLOCKED,
        answer="",
        evidence_status=EvidenceStatus.UNVERIFIED,
    )
    repl = FakeRepl(tmp_path, ModelRegistry())
    adapter._render_kernel_result(
        repl,
        adapter.HarnessRun(result=waiting, task_id="q", ledger_path=ledger),
        ConsoleCallback(verbose=False),
        model_id="test/fake",
    )

    title, text = repl.rendered[-1]
    assert title == "Kernel · 等待回答"
    assert text.strip() == "等待你的回答：哪个仓库？"


def test_a_stopped_run_is_never_titled_as_finished(tmp_path: Path) -> None:
    """phase and evidence are both labels; neither may overrule the other.

    ``accepted`` is true here on purpose: a run can have accepted delivery and
    still not be ``FINALIZED`` (blocked after the outer rounds were spent).
    The title must say it stopped *and* what the evidence covered.
    """

    import xenon.repl.runtime_adapter as adapter
    from xenon.kernel.contracts import EvidenceStatus, TaskPhase, TaskResult

    ledger = tmp_path / "ledger.jsonl"
    stopped = TaskResult(
        task_id="t",
        accepted=True,
        phase=TaskPhase.BLOCKED,
        answer="half the work",
        evidence_status=EvidenceStatus.VERIFIED,
    )
    repl = FakeRepl(tmp_path, ModelRegistry())
    repl._show_thinking = True

    adapter._render_kernel_result(
        repl,
        adapter.HarnessRun(result=stopped, task_id="t", ledger_path=ledger),
        ConsoleCallback(verbose=False),
        model_id="test/fake",
    )

    title = repl.rendered[-1][0]
    assert "已阻塞" in title
    assert "已验证" in title
