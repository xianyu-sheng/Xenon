"""Kernel runtime adapter for the interactive REPL (#202 stage 1).

The kernel is the only entry for tool turns.  The REPL builds a TaskSpec, runs
the loop, projects events into the existing ConsoleCallback, and writes the
assistant result back into the REPL's context and turn tree.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Mapping

from xenon.catalog.specs import mcp_tool_specs, select_mcp_tool_specs
from xenon.harness.llm import (
    ModelConfig,
    build_llm_loop_strategy,
)
from xenon.harness.runner import HarnessRun, result_json, resume_task, run_task
from xenon.kernel.contracts import (
    CallTool,
    Capability,
    PolicyDecision,
    TaskMode,
    TaskPhase,
    TaskResult,
)
from xenon.kernel.policies import PermissionMode
from xenon.kernel.strategies import LoopProfile
from xenon.modes import MODE_CATALOG, loop_profile_for
from xenon.repl.execution_policy import ExecutionLevel

MCP_TOOL_LIMIT_ENV = "XENON_MCP_TOOL_LIMIT"
MCP_TOOL_ALLOWLIST_ENV = "XENON_MCP_TOOLS"
_DEFAULT_MCP_TOOL_LIMIT = 16


class KernelAdapterError(RuntimeError):
    """The REPL cannot construct or run a kernel task."""


def _mcp_tool_limit() -> int:
    raw = os.environ.get(MCP_TOOL_LIMIT_ENV, "").strip()
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_MCP_TOOL_LIMIT


def _mcp_tool_allowlist() -> tuple[str, ...]:
    raw = os.environ.get(MCP_TOOL_ALLOWLIST_ENV, "").strip()
    if not raw:
        return ()
    return tuple(
        part
        for part in raw.replace(",", " ").replace(";", " ").split()
        if part
    )


def should_route_to_kernel(
    *,
    runtime_mode: str,
    requires_tools: bool,
    skill_name: str | None,
    plan_mode: bool,
    plan_deferred_write: bool = False,
    pending: object | None = None,
    recipe: object | None,
    mode_name: str | None = None,
    external_query: bool = False,
    mcp_available: bool = False,
) -> bool:
    """Route a tool turn through the kernel when the mode declares a loop.

    ``/mode`` is an explicit user choice.  Every catalog mode now declares a
    ``LoopProfile`` (plan / react / reflect phases), so a tool turn runs on the
    kernel; an unknown mode name has no profile and fails closed rather than
    being silently executed as the bare react phase.

    ``/plan`` is a constraint, not a runtime switch.  Both read-only and
    deferred-write plan turns route to the kernel; turn_flow selects
    ``plan_only=True`` for the deferred-write case, so the kernel produces a
    ``PLANNED`` result and never executes before approval.
    """

    requested_mode = (mode_name or "direct").strip().lower()
    mode = MODE_CATALOG.get(requested_mode)
    # A skill turn is just a different prompt, and an external query is just a
    # task shape: both run on the same loop.  Whether the loop can answer is a
    # runtime question (capability binding), not a reason to change runtimes.
    del skill_name, external_query, mcp_available
    return (
        runtime_mode == "kernel"
        and requires_tools
        and pending is None
        and recipe is None
        and mode is not None
        and mode.loop is not None
    )


def permission_mode_for(level: ExecutionLevel | int) -> PermissionMode:
    """Map the REPL's authorization level onto the kernel's conservative modes.

    EXECUTE deliberately maps to AUTO_EDIT, not BYPASS: the kernel policy still
    permits workspace edits and safe test commands, but arbitrary shell needs a
    scoped grant.  Approval UI/scoped grants are a later slice.
    """

    value = int(level)
    if value <= int(ExecutionLevel.READ_ONLY):
        return PermissionMode.READ_ONLY
    return PermissionMode.AUTO_EDIT


def sync_context_window(repl: Any, model_ids: list[str]) -> int:
    """Point the REPL's context meter at the model that is actually running.

    The chat path already does this from the router's aliases; kernel turns
    bypassed it, so a 1M model kept showing 128K-based percentages.
    """

    window = 0
    registry = getattr(repl, "registry", None)
    if registry is not None:
        try:
            window = registry.context_window_for(
                [str(item).rsplit("/", 1)[-1] for item in model_ids]
            )
        except Exception:  # noqa: BLE001 — a meter must never break a turn
            window = 0
    if window <= 0:
        for model_id in model_ids:
            try:
                configured = (
                    registry.get_model_by_id(model_id)
                    if registry is not None
                    else None
                )
                window = int(getattr(configured, "context_window", 0) or 0)
            except Exception:  # noqa: BLE001 — same rule: never break a turn
                window = 0
            if window > 0:
                break
    ctx_mgr = getattr(repl, "ctx_mgr", None)
    if window > 0 and ctx_mgr is not None:
        ctx_mgr.max_tokens = window
    return window


def model_config_for(registry: Any, model_ids: list[str]) -> ModelConfig:
    if not model_ids:
        raise KernelAdapterError("no model selected for kernel runtime")
    model_id = model_ids[0]
    configured = registry.get_model_by_id(model_id) if registry is not None else None
    if configured is None:
        return ModelConfig(model_id=model_id)
    return ModelConfig(
        model_id=configured.model_id,
        base_url=configured.base_url,
        api_key=configured.api_key,
        max_tokens=max(256, int(configured.max_tokens or 8192)),
        # Action selection benefits from determinism more than prose variety.
        temperature=0.0,
        reasoning_effort=configured.reasoning_effort,
        # The agent's budget follows the model it actually runs on: a 1M
        # model should get a 1M-sized working set, a 128K model 128K.
        context_window=int(getattr(configured, "context_window", 0) or 0),
    )


def _receipt_observation(receipt: Mapping[str, Any]) -> str:
    """Short, content-free receipt summary for the existing ThinkingPanel."""

    status = receipt.get("status") or "unknown"
    effects = receipt.get("effects") or []
    kinds = [
        str(item.get("type") or item.get("kind") or "effect")
        for item in effects
        if isinstance(item, Mapping)
    ]
    error = str(receipt.get("error") or "")
    text = f"{status}"
    if kinds:
        text += " · " + ", ".join(kinds)
    if error:
        text += f" · {error[:300]}"
    return text


def _receipt_action_input(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Recover file paths from typed receipt effects for the session panel.

    The legacy panel infers paths from tool params; kernel receipts do not keep
    those params.  Project the typed effects instead, so session working memory
    and turn-node artifacts still see what the kernel actually wrote.
    """

    paths: list[str] = []
    created: list[str] = []
    for effect in receipt.get("effects") or ():
        if not isinstance(effect, Mapping):
            continue
        for key in ("paths", "workspace_delta"):
            value = effect.get(key)
            if isinstance(value, (list, tuple)):
                paths.extend(str(item) for item in value if item)
        value = effect.get("created_paths")
        if isinstance(value, (list, tuple)):
            created.extend(str(item) for item in value if item)

    unique_paths: list[str] = []
    for path in paths:
        if path not in unique_paths:
            unique_paths.append(path)
    unique_created: list[str] = []
    for path in created:
        if path not in unique_created:
            unique_created.append(path)

    params: dict[str, Any] = {}
    if unique_paths:
        params["file_paths"] = unique_paths
    if unique_created:
        params["created_paths"] = unique_created
    return params


class ReplEventSink:
    """Project kernel ledger events into the REPL's existing callback/panel."""

    def __init__(self, callback: Any) -> None:
        self.callback = callback

    def emit(self, event: Any) -> None:
        kind = getattr(getattr(event, "kind", None), "value", "")
        payload = getattr(event, "payload", {}) or {}
        if kind == "thought":
            self.callback.on_think(str(payload.get("text") or ""))
        elif kind == "tool_receipt":
            receipt = payload.get("receipt") or {}
            if not isinstance(receipt, Mapping):
                return
            tool = str(receipt.get("tool") or "tool")
            self.callback.on_act(tool, _receipt_action_input(receipt))
            self.callback.on_observe(_receipt_observation(receipt))
        elif kind == "policy_decision":
            decision = str(payload.get("kind") or "")
            reason = str(payload.get("reason") or "")
            if decision == "veto":
                self.callback.on_error(f"policy veto: {reason}")
            else:
                self.callback.on_warning(f"policy: {decision} {reason}")
        elif kind == "debt_opened":
            self.callback.on_warning(
                f"open obligation: {payload.get('kind') or ''} "
                f"{payload.get('target') or ''} — {payload.get('reason') or ''}"
            )
        elif kind == "user_question":
            self.callback.on_warning(
                str(payload.get("question") or "user input required")
            )
        elif kind == "step_verified":
            # Step-level verification is a first-class stage: the panel shows
            # unmet verdicts so the turn tree reflects what reflection saw.
            if str(payload.get("status") or "") != "satisfied":
                unmet = [
                    str(item.get("obligation_id") or "")
                    for item in payload.get("verdicts") or ()
                    if isinstance(item, Mapping)
                    and str(item.get("status") or "") != "satisfied"
                ]
                self.callback.on_warning(
                    f"step {payload.get('step_id')} verification unmet: "
                    + (", ".join(item for item in unmet if item) or "no verdicts")
                )
        elif kind == "step_explored":
            decision = str(payload.get("decision") or "")
            step_id = payload.get("step_id")
            if decision == "budget_exhausted":
                self.callback.on_warning(
                    f"step {step_id} 步内预算用尽（{payload.get('calls')} 次），交给校验决定"
                )
            elif decision == "step_correction":
                self.callback.on_think(
                    f"step {step_id} 审查后补一次修正：{payload.get('capability') or ''}"
                )
        elif kind == "plan_reviewed":
            decision = str(payload.get("decision") or "")
            if decision == "amended":
                self.callback.on_think(
                    f"计划审查补充了 {payload.get('added_steps')} 个步骤"
                )
            elif decision == "rejected":
                self.callback.on_warning(
                    f"计划审查被拒绝：{payload.get('reason') or ''}"
                )
        elif kind == "reflection_proposed":
            decision = str(payload.get("decision") or "")
            if decision == "correction":
                self.callback.on_think(
                    f"反思补一次修正：{payload.get('capability') or ''}"
                )
            elif decision in {"repeated", "declined"}:
                self.callback.on_think("反思结束，交给确定性校验")


_CAPABILITY_TOOL = {
    Capability.READ: "read",
    Capability.SEARCH: "search",
    Capability.EDIT: "apply_patch",
    Capability.VERIFY: "bash",
    Capability.SHELL: "bash",
    Capability.VCS: "git",
    Capability.REMOTE: "mcp_call",
}
_CRITICAL_CAPABILITIES = frozenset(
    {Capability.VERIFY, Capability.SHELL, Capability.VCS, Capability.REMOTE}
)


class KernelApprovalProvider:
    """Bridge a kernel REQUIRE_APPROVAL decision to the REPL permission UI."""

    def __init__(self, repl: Any) -> None:
        self.repl = repl

    def request(
        self, view: Any, action: Any, decision: PolicyDecision
    ) -> bool:
        del view, decision
        if not isinstance(action, CallTool):
            return False
        confirm = getattr(self.repl, "_confirm_tool", None)
        if not callable(confirm):
            return False
        capability = action.intent.capability
        tool = _CAPABILITY_TOOL.get(capability, capability.value)
        risk = (
            "CRITICAL"
            if capability in _CRITICAL_CAPABILITIES
            else "WRITE"
        )
        try:
            result = confirm(tool, dict(action.intent.params), risk)
        except Exception:  # noqa: BLE001 — UI failure must deny
            return False
        if isinstance(result, tuple):
            return bool(result[0])
        return bool(result)


class RuntimeAdapter:
    """Build and execute one kernel task with REPL-owned configuration."""

    def __init__(
        self,
        *,
        workspace: str | Path,
        model_config: ModelConfig,
        permission: PermissionMode = PermissionMode.AUTO_EDIT,
        ledger_dir: str | Path | None = None,
        finalize: bool | None = None,
        max_steps: int = 24,
        client: Any = None,
        approval: Any = None,
        allow_approval: bool = False,
        catalog_profile: str = "core",
        gateway_context: Any = None,
        mcp_registry: Any = None,
        strategy_mode: str = "react",
        plan_only: bool = False,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise KernelAdapterError(
                f"kernel workspace is not a directory: {self.workspace}"
            )
        self.model_config = model_config
        self.permission = permission
        self.ledger_dir = (
            Path(ledger_dir).expanduser()
            if ledger_dir is not None
            else Path.home() / ".xenon" / "ledger" / "repl"
        )
        self.finalize = (
            (self.workspace / ".git").exists()
            if finalize is None
            else bool(finalize)
        )
        self.max_steps = max(1, int(max_steps))
        self.client = client
        self.approval = approval
        self.allow_approval = bool(allow_approval)
        self.catalog_profile = catalog_profile
        self.gateway_context = gateway_context
        self.mcp_registry = mcp_registry
        self.strategy_mode = strategy_mode
        self.plan_only = bool(plan_only)

    def strategy_profile(self) -> LoopProfile | None:
        """Loop profile for the selected mode (``None`` fails closed)."""

        return loop_profile_for(self.strategy_mode)

    def run(
        self,
        objective: str,
        *,
        user_objective: str = "",
        task_id: str | None = None,
        sink: Any = None,
    ) -> HarnessRun:
        if self.strategy_mode == "legacy":
            raise KernelAdapterError(
                "selected mode has no kernel loop; it must stay on the legacy path"
            )
        profile = self.strategy_profile()
        if profile is None:
            # Unknown modes must not silently become the bare react phase: a
            # mode has to declare its loop profile explicitly.
            raise KernelAdapterError(
                f"unknown kernel strategy: {self.strategy_mode}"
            )
        if self.plan_only:
            # Plan-only never executes a step; the profile only needs the plan
            # phase.
            profile = LoopProfile(plan=True)
        strategy = build_llm_loop_strategy(
            self.model_config, profile, client=self.client
        )
        constraints = [
            "runtime:kernel",
            f"model:{self.model_config.model_id}",
        ]
        extra_specs = ()
        if self.mcp_registry is not None:
            # MCP servers may be lazy.  Discover once for a real MCP task so
            # the model sees actual tool names instead of a generic mcp_call
            # description.  Discovery failures must not break core tasks.
            if not getattr(self.mcp_registry, "tool_map", None):
                discover = getattr(self.mcp_registry, "discover_tools", None)
                if callable(discover):
                    try:
                        discover()
                    except Exception:
                        pass
            tool_map = getattr(self.mcp_registry, "tool_map", {}) or {}
            extra_specs = select_mcp_tool_specs(
                mcp_tool_specs(tool_map),
                objective,
                limit=_mcp_tool_limit(),
                allowed=_mcp_tool_allowlist(),
            )
            names = [spec.name for spec in extra_specs]
            if names:
                constraints.append("mcp_tools: " + ", ".join(names))
        run = run_task(
            objective,
            user_objective=user_objective,
            strategy=strategy,
            workspace=self.workspace,
            ledger_dir=self.ledger_dir,
            task_id=task_id or f"repl-{uuid.uuid4().hex[:12]}",
            permission=self.permission,
            finalize=self.finalize,
            max_steps=self.max_steps,
            mode=TaskMode.PLAN if self.plan_only else TaskMode.AUTO,
            constraints=tuple(constraints),
            sink=sink,
            approval=self.approval if self.allow_approval else None,
            allow_approval=self.allow_approval,
            catalog_profile=self.catalog_profile,
            gateway_context=self.gateway_context,
            extra_specs=extra_specs,
        )
        try:
            result_path = run.ledger_path.with_suffix(".result.json")
            result_path.write_text(
                result_json(run.result) + "\n", encoding="utf-8"
            )
        except OSError:
            pass
        return run

    def resume(
        self,
        *,
        task_id: str,
        plan: Any,
        plan_digest: str,
        approval_id: str,
        objective: str = "",
        user_objective: str = "",
        sink: Any = None,
    ) -> HarnessRun:
        """Execute an approved plan in its existing kernel ledger."""

        if self.plan_only:
            raise KernelAdapterError("plan-only adapters cannot resume execution")
        # An approved plan resumes in its own ledger: follow the plan's intents
        # deterministically instead of asking the model what to do next.
        strategy = build_llm_loop_strategy(
            self.model_config,
            LoopProfile(plan=True, step_scope=True, follow_plan=True),
            client=self.client,
            resumed=True,
        )
        constraints = [
            "runtime:kernel",
            f"model:{self.model_config.model_id}",
        ]
        run = resume_task(
            task_id=task_id,
            plan=plan,
            plan_digest=plan_digest,
            approval_id=approval_id,
            strategy=strategy,
            objective=objective,
            user_objective=user_objective,
            workspace=self.workspace,
            ledger_dir=self.ledger_dir,
            permission=self.permission,
            finalize=self.finalize,
            max_steps=self.max_steps,
            constraints=tuple(constraints),
            sink=sink,
            approval=self.approval if self.allow_approval else None,
            allow_approval=self.allow_approval,
            catalog_profile=self.catalog_profile,
            gateway_context=self.gateway_context,
        )
        try:
            result_path = run.ledger_path.with_suffix(".result.json")
            result_path.write_text(
                result_json(run.result) + chr(10), encoding="utf-8"
            )
        except OSError:
            pass
        return run

    @classmethod
    def from_repl(
        cls,
        repl: Any,
        *,
        model_ids: list[str],
        level: ExecutionLevel | int,
        client: Any = None,
        plan_only: bool = False,
        ledger_dir: str | Path | None = None,
    ) -> RuntimeAdapter:
        project_ctx = getattr(repl, "project_ctx", None)
        workspace = (
            getattr(project_ctx, "root", None)
            or getattr(project_ctx, "working_dir", None)
            or Path.cwd()
        )
        gateway_context = getattr(repl, "agent_context", None)
        if not hasattr(gateway_context, "get"):
            # Test doubles / minimal hosts may not provide the full
            # AgentContext surface.  MCP needs the real context; core tools do
            # not, so fall back to the gateway's default context.
            gateway_context = None
        mcp_registry = getattr(repl, "_mcp_registry", None)
        has_mcp = bool(
            mcp_registry is not None
            and (
                getattr(mcp_registry, "clients", None)
                or (
                    hasattr(mcp_registry, "has_pending_servers")
                    and mcp_registry.has_pending_servers()
                )
            )
        )
        return cls(
            workspace=workspace,
            model_config=model_config_for(repl.registry, model_ids),
            permission=permission_mode_for(level),
            ledger_dir=(
                Path(ledger_dir)
                if ledger_dir is not None
                else Path.home() / ".xenon" / "ledger" / "repl"
            ),
            client=client,
            approval=KernelApprovalProvider(repl),
            allow_approval=(
                int(level) >= int(ExecutionLevel.EXECUTE)
                or (
                    has_mcp
                    and int(level) == int(ExecutionLevel.READ_ONLY)
                )
            ),
            catalog_profile="core+mcp" if has_mcp else "core",
            gateway_context=gateway_context,
            mcp_registry=mcp_registry if has_mcp else None,
            # The adapter stores the mode name; the loop profile is resolved
            # from the catalog at run time, so a mode cannot carry a hard-coded
            # strategy.
            strategy_mode=getattr(repl.registry, "current_mode", "react")
            or "react",
            plan_only=plan_only,
        )


def _console_for(repl: Any) -> Any:
    console = getattr(repl, "console", None)
    if console is not None:
        return console
    from xenon.repl.repl import console as repl_console

    return repl_console


def _clear_legacy_task_state(repl: Any) -> None:
    """Drop the pre-kernel task-state bag; the ledger is authoritative."""

    agent_context = getattr(repl, "agent_context", None)
    setter = getattr(agent_context, "set", None)
    if callable(setter):
        setter("_task_state", None)


def _result_summary(result: TaskResult) -> str:
    if result.answer.strip():
        return result.answer
    if result.accepted:
        return "任务已完成并交付。"
    if result.debt:
        reasons = "; ".join(item.reason for item in result.debt[:3])
        return f"任务被阻断，仍有未满足的证据：{reasons}"
    return f"任务未交付（phase={result.phase.value}）。"


def _render_kernel_result(
    repl: Any,
    run: HarnessRun,
    callback: Any,
    *,
    model_id: str,
) -> None:
    result = run.result
    repl._last_kernel_ledger = run.ledger_path
    repl._last_kernel_task_id = run.task_id
    console = _console_for(repl)
    if hasattr(callback, "finish_activity"):
        callback.finish_activity()
    panel = (
        callback.get_thinking_panel()
        if hasattr(callback, "get_thinking_panel")
        else None
    )
    if panel is not None:
        repl._last_thinking_panel = panel
        tool_count = int(getattr(panel, "tool_call_count", 0) or 0)
        for _ in range(tool_count):
            repl.status_bar.add_tool_call()
        track = getattr(repl, "_track_session_files", None)
        if callable(track):
            track(panel)

    answer = _result_summary(result)
    accepted = bool(result.accepted)
    planned = result.phase is TaskPhase.PLANNED
    if accepted and result.finalize_sha:
        answer = (
            answer.rstrip()
            + "\n\n交付: commit "
            + result.finalize_sha[:12]
            + " · evidence: "
            + result.evidence_status.value
        )
    status = (
        "pending-approval"
        if planned
        else ("passed" if accepted else "degraded")
    )
    reasons = [] if planned else [item.reason for item in result.debt]
    finish_node = getattr(repl, "_finish_turn_node", None)
    if callable(finish_node):
        finish_node(
            status,
            engine="kernel",
            reasons=reasons,
            panel=panel,
            metrics=dict(result.metrics),
        )

    repl._last_mode_line = "· Kernel runtime"
    if getattr(repl, "_show_thinking", False):
        captured = getattr(repl, "_captured_log", "")
        if captured:
            console.print(captured.rstrip(), style="dim")
        if panel is not None:
            console.print(panel)
    else:
        metrics = result.metrics
        console.print(
            "[dim]  💭 kernel · "
            f"{int(metrics.get('steps', 0))} steps · "
            f"{int(metrics.get('tool_calls', 0))} tools · "
            f"{result.evidence_status.value}"
            "[/dim]"
        )
    render = repl._render_assistant_text
    if planned:
        title = "Kernel · plan"
    else:
        title = "Kernel" if accepted else "Kernel · blocked"
    render(answer, title=title)
    if planned:
        console.print(
            "[dim]· 计划已接受，执行等待确认；本轮未写盘。[/dim]"
        )
        console.print(f"[dim]ledger: {run.ledger_path}[/dim]")
    elif not accepted:
        for item in result.debt[:5]:
            console.print(
                f"[yellow]· {item.obligation.kind.value}: "
                f"{item.reason or item.obligation.id}[/yellow]"
            )
        console.print(f"[dim]ledger: {run.ledger_path}[/dim]")

    add_message = getattr(repl.ctx_mgr, "add_assistant_message", None)
    if callable(add_message):
        add_message(answer, model_used=model_id)
    # The kernel ledger is the source of truth for this turn, even when it
    # blocks.  The legacy task-state bag must not survive and drive the next
    # legacy/continuation turn with a second, stale explanation of the task.
    _clear_legacy_task_state(repl)
    record = getattr(repl, "_record_event", None)
    if callable(record):
        record(
            "runtime/kernel",
            task_id=run.task_id,
            accepted=accepted,
            evidence=result.evidence_status.value,
            ledger=str(run.ledger_path),
        )


def resume_kernel_plan(
    repl: Any,
    *,
    task_id: str,
    plan: Any,
    plan_digest: str,
    approval_id: str,
    objective: str = "",
    user_objective: str = "",
    callback: Any = None,
    client: Any = None,
    ledger_dir: str | Path | None = None,
) -> HarnessRun | None:
    """Resume an approved kernel plan and render its execution result."""

    if callback is None:
        callback = repl._make_callback()
    model_ids = list(repl.registry.get_role_priority("planner"))
    adapter = RuntimeAdapter.from_repl(
        repl,
        model_ids=model_ids,
        level=ExecutionLevel.EXECUTE,
        client=client,
        ledger_dir=ledger_dir,
    )
    sink = ReplEventSink(callback)
    agent_context = getattr(repl, "agent_context", None)
    setter = getattr(agent_context, "set", None)
    getter = getattr(agent_context, "get", None)
    previous_level = (
        getter("_execution_level") if callable(getter) else None
    )
    previous_reason = (
        getter("_execution_reason") if callable(getter) else None
    )
    if callable(setter):
        # The approved plan is executed at EXECUTE, not at the plan-only
        # READ_ONLY level that the legacy ToolExecutor still reads.
        setter("_execution_level", int(ExecutionLevel.EXECUTE))
        setter("_execution_reason", "approved plan execution")
    start_capture = getattr(repl, "_start_log_capture", None)
    if callable(start_capture):
        start_capture()
    try:
        try:
            run = adapter.resume(
                task_id=task_id,
                plan=plan,
                plan_digest=plan_digest,
                approval_id=approval_id,
                objective=objective,
                user_objective=user_objective,
                sink=sink,
            )
        finally:
            stop_capture = getattr(repl, "_stop_log_capture", None)
            if callable(stop_capture):
                repl._captured_log = stop_capture()
    except Exception as exc:  # noqa: BLE001 — approval command must report failure
        _clear_legacy_task_state(repl)
        _console_for(repl).print(f"[red]Kernel plan execution failed: {exc}[/red]")
        add_message = getattr(repl.ctx_mgr, "add_assistant_message", None)
        if callable(add_message):
            add_message(f"[Kernel plan execution error] {exc}")
        return None
    finally:
        if callable(setter):
            setter(
                "_execution_level",
                previous_level
                if previous_level is not None
                else int(ExecutionLevel.READ_ONLY),
            )
            setter("_execution_reason", previous_reason or "")
    _render_kernel_result(
        repl,
        run,
        callback,
        model_id=adapter.model_config.model_id,
    )
    return run


def run_kernel_turn(
    repl: Any,
    *,
    objective: str,
    user_input: str,
    model_ids: list[str],
    execution_policy: Any,
    callback: Any = None,
    client: Any = None,
    plan_only: bool = False,
) -> HarnessRun | None:
    """Run one REPL task turn through the kernel and render its result."""

    if callback is None:
        callback = repl._make_callback()
    sync_context_window(repl, model_ids)
    try:
        adapter = RuntimeAdapter.from_repl(
            repl,
            model_ids=model_ids,
            level=execution_policy.level,
            client=client,
            plan_only=plan_only,
        )
        sink = ReplEventSink(callback)
        start_capture = getattr(repl, "_start_log_capture", None)
        if callable(start_capture):
            start_capture()
        try:
            run = adapter.run(objective, user_objective=user_input, sink=sink)
        finally:
            stop_capture = getattr(repl, "_stop_log_capture", None)
            if callable(stop_capture):
                repl._captured_log = stop_capture()
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # noqa: BLE001 — REPL must show an actionable error
        # A failed kernel turn must not leave legacy task state behind either.
        _clear_legacy_task_state(repl)
        finish_node = getattr(repl, "_finish_turn_node", None)
        if callable(finish_node):
            finish_node("interrupted", engine="kernel", reasons=[str(exc)[:200]])
        _console_for(repl).print(f"[red]Kernel runtime error: {exc}[/red]")
        add_message = getattr(repl.ctx_mgr, "add_assistant_message", None)
        if callable(add_message):
            add_message(f"[Kernel runtime error] {exc}")
        return None

    _render_kernel_result(
        repl,
        run,
        callback,
        model_id=adapter.model_config.model_id,
    )
    return run


__all__ = [
    "KernelAdapterError",
    "KernelApprovalProvider",
    "ReplEventSink",
    "RuntimeAdapter",
    "model_config_for",
    "permission_mode_for",
    "resume_kernel_plan",
    "run_kernel_turn",
    "should_route_to_kernel",
]
