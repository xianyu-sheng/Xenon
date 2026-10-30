"""Opt-in REPL kernel runtime adapter (#224 / #202 first slice)."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

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
from xenon.ledger.events import EventKind, canonical_json
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
    resume_kernel_plan,
    run_kernel_turn,
    should_route_to_kernel,
)


class FakeClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    def __call__(self, model_id, messages, **kwargs):
        return self.responses.pop(0)


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


class FakeStatus:
    def __init__(self) -> None:
        self.tools = 0

    def add_tool_call(self) -> None:
        self.tools += 1


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

    def _confirm_tool(self, tool, params, risk):
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

    def _confirm_tool(self, tool, params, risk):
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


def test_runtime_adapter_approval_only_at_execute_level(tmp_path: Path) -> None:
    registry = ModelRegistry()
    registry.add_model("test/fake", "fake")
    repl = FakeRepl(tmp_path, registry)
    write = RuntimeAdapter.from_repl(
        repl, model_ids=["test/fake"], level=ExecutionLevel.WRITE
    )
    execute = RuntimeAdapter.from_repl(
        repl, model_ids=["test/fake"], level=ExecutionLevel.EXECUTE
    )
    assert write.allow_approval is False
    assert execute.allow_approval is True
    assert execute.approval is not None


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
    assert repl.rendered[-1][0] == "Kernel"
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
    assert repl.rendered[-1][0] == "Kernel · blocked"
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
    assert repl.rendered[-1][0] == "Kernel"
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
            )
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
    assert run.result.accepted is False
    assert run.result.phase.value == "blocked"
    assert repl.confirm_calls == [
        ("bash", {"command": "echo hello"}, "CRITICAL")
    ]
    assert repl.finished == [("degraded", "kernel")]
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
