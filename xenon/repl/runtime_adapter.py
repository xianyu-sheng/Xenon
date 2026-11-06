"""Kernel runtime adapter for the interactive REPL (#202 stage 1).

The kernel is the only entry for tool turns.  The REPL builds a TaskSpec, runs
the loop, projects events into the existing ConsoleCallback, and writes the
assistant result back into the REPL's context and turn tree.
"""

from __future__ import annotations

import logging
import os
import select
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
    ToolIntent,
)
from xenon.kernel.policies import PermissionMode
from xenon.kernel.strategies import LoopProfile
from xenon.modes import MODE_CATALOG, loop_profile_for
from xenon.repl.execution_policy import ExecutionLevel

# One explanation for a stopped run, shared by the REPL panel and the replay
# tool; a second copy in this module is how `user_question` stayed invisible
# (#402).
from xenon.replay import blocking_reason

logger = logging.getLogger(__name__)

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


def resolve_max_steps(value: int | None = None) -> int:
    """Resolve the global step cap; ``0`` means no global cap.

    The single-loop design brakes on three things instead of one global step
    count: the per-step call budget (``LoopProfile.step_calls``), the plan-round
    budget, and the stall/wall clock.  A fixed 24-call turn cap made a
    multi-file task die halfway through for no evidence reason (#190).

    ``XENON_MAX_STEPS`` is the operator override; ``0`` (the default) keeps the
    global cap off.  Existing callers that pass a positive value keep it.
    """

    if value is None:
        raw = os.environ.get("XENON_MAX_STEPS", "0").strip()
        try:
            value = int(raw)
        except ValueError:
            value = 0
    return max(0, int(value))


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


def _steering_queue(repl: Any) -> Any:
    queue = getattr(repl, "_steering_queue", None)
    if queue is None:
        import queue as _queue

        queue = _queue.Queue()
        repl._steering_queue = queue
    return queue


class SteeringListener:
    """Read typed lines while the kernel owns the main thread.

    Only active on a real terminal: under tests or piped input there is no
    "typing mid-run" to capture, and a stray reader would steal the next
    prompt's line.  Lines are pushed into the transport queue; the runtime
    turns them into events at the next action boundary.
    """

    def __init__(self, repl: Any, *, stream: Any = None) -> None:
        self._repl = repl
        self._stream = stream
        self._stop = False
        self._thread: Any = None

    def start(self) -> bool:
        import sys
        import threading

        stream = self._stream or sys.stdin
        try:
            interactive = bool(stream) and stream.isatty()
        except Exception:  # noqa: BLE001 — exotic streams are not interactive
            interactive = False
        if not interactive:
            return False
        # On Windows, select() only accepts sockets: polling a console/pipe
        # stream would raise and the thread would die immediately.  Rather
        # than a zombie thread that keeps owning stdin after the turn (which
        # starves the next prompt), refuse to start on platforms where the
        # polling loop cannot work.  Steering mid-run is then unavailable on
        # Windows; the REPL input stays responsive, which is the P0.
        try:
            select.select([stream], [], [], 0)
        except (OSError, ValueError, TypeError):
            return False
        self._stream = stream
        self._thread = threading.Thread(
            target=self._loop, name="xenon-steering", daemon=True
        )
        self._thread.start()
        return True

    def _loop(self) -> None:
        while not self._stop:
            # A blocking readline() would survive stop() forever and keep
            # owning stdin after the kernel turn ends — the main thread's next
            # prompt() then starves.  Poll with a short select timeout so
            # stop() takes effect within ~0.2s and stdin returns to the REPL.
            try:
                ready, _, _ = select.select([self._stream], [], [], 0.2)
            except (OSError, ValueError):  # pragma: no cover - exotic streams
                return
            if not ready:
                continue
            try:
                line = self._stream.readline()
            except Exception:  # noqa: BLE001 — a closed terminal ends the reader
                return
            if not line:
                return
            steer(self._repl, line)

    def stop(self) -> None:
        self._stop = True


class CancelOnInterrupt:
    """Map Ctrl-C during a kernel turn to ``runtime.request_cancel()``.

    Restoring the previous handler matters: leaving a SIGINT handler installed
    would change what Ctrl-C does at the prompt, which is a different (and
    useful) behaviour.
    """

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime
        self._previous: Any = None
        self._installed = False

    def __enter__(self) -> "CancelOnInterrupt":
        import signal
        import threading

        if threading.current_thread() is not threading.main_thread():
            return self
        try:
            self._previous = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, self._handler)
            self._installed = True
        except (ValueError, OSError):  # pragma: no cover - exotic platforms
            self._installed = False
        return self

    def _handler(self, signum: int, frame: Any) -> None:
        self._runtime.request_cancel()

    def __exit__(self, *exc: Any) -> None:
        import signal

        if self._installed:
            try:
                signal.signal(signal.SIGINT, self._previous)
            except (ValueError, OSError):  # pragma: no cover - exotic platforms
                pass


def kernel_progress_reporter(repl: Any) -> Any:
    """A progress hook that shows "still working" without printing raw JSON.

    Kernel turns emit a protocol payload, so token-level output is noise.  What
    the operator needs is: something is happening, how long, how much.

    Per-second narration was that idea taken too far: a 19-step turn printed
    the line hundreds of times and pushed the real answer off screen (#403).
    The status bar already carries the live counter, so console output here is
    limited to one "still generating" notice per model call, and only once the
    call is slow enough to look stuck.
    """

    console = _console_for(repl)
    slow_notice_after = 15.0
    state: dict[str, float] = {"last_seconds": 0.0, "notified": 0.0}

    def report(seconds: float, chars: int) -> None:
        # #372 E1: push the live kernel snapshot into the status bar on every
        # model call — data comes from the runtime, never guessed by the REPL.
        status_bar = getattr(repl, "status_bar", None)
        runtime = getattr(repl, "_active_runtime", None)
        if status_bar is not None and runtime is not None:
            snapshot = getattr(runtime, "status_snapshot", None)
            if callable(snapshot):
                try:
                    status_bar.set_kernel_status(snapshot())
                except Exception:  # noqa: BLE001 — progress is cosmetic
                    pass
        # #408: the status bar's "context N%" must mean "how full is the
        # window", not "how many tokens did this session bill".  Legacy turns
        # fed this from the chat history, which a kernel turn never grows, so
        # a 20K-character kernel prompt displayed as "0.3%".  Publish the real
        # depth of the last request instead.
        ctx_mgr = getattr(repl, "ctx_mgr", None)
        record = getattr(ctx_mgr, "record_real_usage", None)
        if callable(record) and runtime is not None:
            usage = getattr(runtime, "_usage", None)
            depth = int(getattr(usage, "last_prompt", 0) or 0)
            if depth:
                try:
                    # total = prompt: the window holds the input, and that is
                    # the number the ratio is about.
                    record(depth, 0, depth)
                except Exception:  # noqa: BLE001 — a meter is not a turn
                    pass
        if seconds < state["last_seconds"]:  # a new model call restarts the clock
            state["notified"] = 0.0
        state["last_seconds"] = seconds
        if seconds < slow_notice_after or state["notified"]:
            return
        state["notified"] = 1.0
        try:
            console.print(
                f"[dim]· 仍在生成 {seconds:.0f}s · {chars} 字符（Ctrl-C 可中断）[/dim]",
                highlight=False,
            )
        except Exception:  # noqa: BLE001 — progress is cosmetic
            pass

    return report


def kernel_request_reporter(repl: Any) -> Any:
    """Render provider phase (attempt / retry / timeout) for the operator.

    The progress hook says "still generating"; this says *why it is slow* —
    a retry after a 429 is a different story from a first attempt, and the
    operator cannot tell them apart from a character count.
    """

    console = _console_for(repl)

    def report(event: dict[str, Any]) -> None:
        kind = str(event.get("kind") or "")
        phase = str(event.get("phase") or "model")
        attempt = int(event.get("attempt") or 1)
        attempts = int(event.get("provider_attempts") or 1)
        try:
            if kind == "start":
                if attempts > 1:
                    console.print(
                        f"[dim]· {phase} 请求中（第 {attempt}/{attempts} 次，"
                        f"超时 {float(event.get('timeout') or 0):.0f}s）[/dim]",
                        highlight=False,
                    )
                else:
                    console.print(
                        f"[dim]· {phase} 请求中[/dim]", highlight=False
                    )
            elif kind == "end":
                if event.get("success"):
                    return  # a normal completion needs no extra line
                reason = str(event.get("reason") or "failed")
                if event.get("retrying"):
                    console.print(
                        f"[yellow]· {phase} 失败（{reason}），将重试[/yellow]",
                        highlight=False,
                    )
                else:
                    console.print(
                        f"[red]· {phase} 失败（{reason}），已用尽 {attempt} 次[/red]",
                        highlight=False,
                    )
        except Exception:  # noqa: BLE001 — telemetry is cosmetic
            pass

    return report


def drain_steering(repl: Any) -> Any:
    """Pop one pending operator line, or ``None`` (never blocks).

    This is a *transport*: whatever it yields becomes a ``user_input`` event
    written by the runtime itself, so typing mid-run cannot change behaviour
    without a trace.
    """

    queue = _steering_queue(repl)
    try:
        return queue.get_nowait()
    except Exception:  # noqa: BLE001 — empty queue (or closed UI) means "nothing"
        return None


def steer(repl: Any, text: str, *, kind: str = "constraint", scope: str = "task") -> bool:
    """Queue one operator line for the running turn (or the next one)."""

    body = str(text or "").strip()
    if not body:
        return False
    _steering_queue(repl).put({"text": body, "kind": kind, "scope": scope})
    return True


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
        # Why the last request ended the way it did.  The protocol only
        # carries a bool, but a blocked run that cannot say *who* denied and
        # why is undebuggable — so the bridge keeps the reason for the ledger.
        self.last_reason = ""
        self.last_scope = ""
        # True while answering a *child's* brokered request (#373).  A child may
        # only ask; it must never create a grant, a TTL rule or an allowed-once
        # entry from its own request, because then a delegated model could
        # accumulate authority one prompt at a time.
        self.from_child = False

    def request(
        self, view: Any, action: Any, decision: PolicyDecision
    ) -> bool:
        del view, decision
        self.last_reason = ""
        if not isinstance(action, CallTool):
            self.last_reason = "approval only covers tool calls"
            return False
        confirm = getattr(self.repl, "_confirm_tool", None)
        if not callable(confirm):
            self.last_reason = "front-end has no confirmation UI"
            return False
        capability = action.intent.capability
        tool = _CAPABILITY_TOOL.get(capability, capability.value)
        risk = (
            "CRITICAL"
            if capability in _CRITICAL_CAPABILITIES
            else "WRITE"
        )
        try:
            result = confirm(tool, dict(action.intent.params), risk, from_child=self.from_child)
        except Exception as exc:  # noqa: BLE001 — UI failure must deny
            self.last_reason = f"confirmation UI failed: {exc}"
            return False
        if isinstance(result, tuple):
            approved = bool(result[0])
            self.last_reason = str(result[1] or "")
        else:
            approved = bool(result)
        # What the human chose to remember: "" (once) / "session" / "always" /
        # "scoped".  Read from the front-end so the closed 2-tuple result
        # vocabulary of ``_confirm_tool`` stays intact.  A child's answer is
        # one-shot by construction, so the scope is always empty for it.
        self.last_scope = (
            ""
            if self.from_child
            else str(getattr(self.repl, "_last_approval_scope", "") or "")
        )
        if not approved and not self.last_reason:
            self.last_reason = "user rejected the request"
        return approved

    def approve_request(self, request: dict[str, Any]) -> tuple[bool, str]:
        """Answer a *child's* brokered request with the same confirmation UI.

        A child process has no terminal, so it asks its parent; the parent asks
        the human through exactly the path a kernel turn would use.  The child
        therefore never gets authority the parent could not obtain itself.
        """

        raw = request.get("capability")
        try:
            capability = Capability(str(raw))
        except ValueError:
            return False, f"unknown capability {raw!r} in the child request"
        action = CallTool(
            ToolIntent(
                capability=capability,
                params=dict(request.get("params") or {}),
            )
        )
        self.from_child = True
        try:
            approved = self.request(None, action, None)
        finally:
            self.from_child = False
        return approved, self.last_reason


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
        max_steps: int | None = None,
        client: Any = None,
        approval: Any = None,
        allow_approval: bool = False,
        catalog_profile: str = "core",
        gateway_context: Any = None,
        mcp_registry: Any = None,
        strategy_mode: str = "react",
        plan_only: bool = False,
        progress_sink: Any = None,
        request_sink: Any = None,
        audit_provider: Any = None,
        permission_rules: Any = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise KernelAdapterError(
                f"kernel workspace is not a directory: {self.workspace}"
            )
        self.model_config = model_config
        self.progress_sink = progress_sink
        self.request_sink = request_sink
        self.permission_rules = permission_rules
        # Session grants are remembered decisions (#373); the front-end owns the
        # book, so the adapter only forwards what is currently active.
        self.session_grants: tuple[Any, ...] = ()
        self.audit_provider = audit_provider
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
        self.max_steps = resolve_max_steps(max_steps)
        self.client = client
        self.approval = approval
        self.allow_approval = bool(allow_approval)
        self.catalog_profile = catalog_profile
        self.gateway_context = gateway_context
        self.mcp_registry = mcp_registry
        self.strategy_mode = strategy_mode
        self.plan_only = bool(plan_only)

    def _rules_of(self, decision: str) -> tuple[Any, ...]:
        """Rules of one decision from the layered settings, if any."""

        rules = self.permission_rules
        if rules is None:
            return ()
        rows = getattr(rules, "rules", ()) or ()
        return tuple(
            rule
            for rule in rows
            if getattr(rule, "decision", "") == decision
        )

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
        on_runtime: Any = None,
        context_blocks: Any = (),
        turn_path: Any = (),
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
        if self.progress_sink is not None:
            from xenon.harness.llm import bind_progress_sink

            bind_progress_sink(strategy, self.progress_sink)
        if self.request_sink is not None:
            from xenon.harness.llm import bind_request_sink

            bind_request_sink(strategy, self.request_sink)
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
            on_runtime=on_runtime,
            workspace=self.workspace,
            ledger_dir=self.ledger_dir,
            task_id=task_id or f"repl-{uuid.uuid4().hex[:12]}",
            permission=self.permission,
            finalize=self.finalize,
            max_steps=self.max_steps,
            mode=TaskMode.PLAN if self.plan_only else TaskMode.AUTO,
            constraints=tuple(constraints),
            sink=sink,
            context_blocks=tuple(context_blocks or ()),
            turn_path=tuple(turn_path or ()),
            audit_provider=self.audit_provider,
            # #392: the approver reaches the runtime whenever the front-end can
            # ask.  ``allow_approval`` still decides whether the *policy* may
            # auto-prompt; an explicit ``request_escalation`` is a separate,
            # human-driven path that only exists because this bridge is wired.
            approval=self.approval,
            allow_approval=self.allow_approval,
            catalog_profile=self.catalog_profile,
            gateway_context=self.gateway_context,
            extra_specs=extra_specs,
            session_grants=self.session_grants,
            deny_rules=self._rules_of("deny"),
            ask_rules=self._rules_of("ask"),
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
        context_blocks: Any = (),
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
            # #392: the approver reaches the runtime whenever the front-end can
            # ask.  ``allow_approval`` still decides whether the *policy* may
            # auto-prompt; an explicit ``request_escalation`` is a separate,
            # human-driven path that only exists because this bridge is wired.
            approval=self.approval,
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
            # #392: the bridge is wired whenever the front-end *can* ask — it
            # re-checks isatty and fails closed with a readable reason.  It is
            # what ``request_escalation`` talks to.  ``allow_approval`` still
            # gates the policy's own auto-prompt path, so a read-level turn's
            # default permissions are unchanged: only an explicit escalation
            # (a human decision, recorded) can widen them.
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
            # /max-steps sets the per-session override; None falls back to the
            # environment/default (no global cap).
            max_steps=getattr(repl, "_max_steps", None),
        )


def session_context_blocks(repl: Any, objective: str) -> tuple[tuple[str, str], ...]:
    """Reference material for a kernel turn: project rules + retrieved memory.

    The chat path has always injected these; kernel turns ignored them, so a
    rule like "don't touch tests/" silently stopped applying the moment the
    turn needed tools.  Labels come from the context manager's keys, and the
    insertion order is preserved so the prompt (and its hash) is reproducible.

    #370 adds two progressive-loading layers:
    - the skill index (name — one-liner) so every task knows what skills exist
      without paying their bodies in tokens;
    - the active skill's body, only when this turn actually runs that skill.
    """

    inject_project = getattr(repl, "_inject_project_context", None)
    if callable(inject_project):
        try:
            inject_project()
        except Exception:  # noqa: BLE001 — context is advisory, never fatal
            logger.debug("project context injection failed", exc_info=True)
    inject_memory = getattr(repl, "_inject_memories", None)
    if callable(inject_memory):
        try:
            inject_memory(objective)
        except Exception:  # noqa: BLE001 — memory is advisory, never fatal
            logger.debug("memory injection failed", exc_info=True)
    blocks: list[tuple[str, str]] = []
    ctx_mgr = getattr(repl, "ctx_mgr", None)
    layers = getattr(ctx_mgr, "context_layers", None)
    if callable(layers):
        try:
            blocks.extend(
                (str(key), str(content))
                for key, content, _stable in layers()
                if str(content or "").strip()
            )
        except Exception:  # noqa: BLE001 — fall back to no context
            pass
    skill_index = _skill_index(repl)
    if skill_index:
        blocks.append(("skills", skill_index))
    active = getattr(repl, "_active_skill_body", None)
    if active:
        blocks.append(("active_skill", active))
    return tuple(blocks)


def _skill_index(repl: Any, limit: int = 40) -> str:
    """One-line index of installed skills, truncated with a visible count.

    ``SkillManager.list_all`` never loads bodies, so this is cheap.  The path
    is included so a model that wants a skill's full text can ``read`` its
    SKILL.md — the progressive channel (#370) instead of a new tool.
    """

    try:
        from xenon.repl.skill_manager import SkillManager

        skills = SkillManager().list_all()
    except Exception:  # noqa: BLE001 — no skills is not an error
        return ""
    if not skills:
        return ""
    lines = []
    for skill in skills[:limit]:
        root = getattr(skill, "root", None)
        path = (str(Path(root) / "SKILL.md") if root else "")
        description = " ".join(str(skill.description or "").split())[:120]
        lines.append(f"- {skill.name}: {description or 'no description'}")
        if path:
            lines[-1] += f"  (read {path} for the full text)"
    if len(skills) > limit:
        lines.append(
            f"… {len(skills) - limit} more skills; list them with /skills"
        )
    return "\n".join(lines)


def _audit_proposer(config: Any) -> Any:
    """Build the V2 audit proposer for a kernel turn (fail-closed on error)."""

    try:
        from xenon.harness.llm import LLMAuditProposer

        return LLMAuditProposer(config)
    except Exception:  # noqa: BLE001 — no proposer means "abstain", not "pass"
        return None


def _console_for(repl: Any) -> Any:
    console = getattr(repl, "console", None)
    if console is not None:
        return console
    from xenon.repl.repl import console as repl_console

    return repl_console


_EVIDENCE_LABELS = {
    "verified": "已验证",
    "partial": "部分验证",
    "unverified": "未验证",
    "abstained": "无法验证",
    "contradicted": "与事实冲突",
}


def _evidence_label(status: str, accepted: bool) -> str:
    """The short label shown next to the kernel answer."""

    if not accepted:
        # Only execution brakes leave accepted=False now.
        return "已停止"
    return _EVIDENCE_LABELS.get(status, status or "未验证")


def _clear_legacy_task_state(repl: Any) -> None:
    """Drop the pre-kernel task-state bag; the ledger is authoritative."""

    agent_context = getattr(repl, "agent_context", None)
    setter = getattr(agent_context, "set", None)
    if callable(setter):
        setter("_task_state", None)


def _result_summary(result: TaskResult, *, ledger_path: Any = None) -> str:
    if result.answer.strip():
        return result.answer
    if result.accepted:
        return "任务已完成并交付。"
    if result.debt:
        reasons = "; ".join(item.reason for item in result.debt[:3])
        return f"任务被阻断，仍有未满足的证据：{reasons}"
    detail = (
        blocking_reason(ledger_path, task_id=result.task_id)
        if ledger_path is not None
        else ""
    )
    if detail.startswith("等待你的回答："):
        # The run did not fail — it is waiting on the operator.  Saying
        # The run did not fail — it is waiting on the operator.  Saying
        # "未交付" first made a clarifying question look like a crash (#402).
        return detail
    if detail:
        return f"任务未交付（phase={result.phase.value}）：{detail}"
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

    answer = _result_summary(result, ledger_path=run.ledger_path)
    # #413: remember that the floor is now the user's.  The next message is an
    # answer to the question this turn just asked, and must reach the model
    # verbatim — a mis-classified "here is the path" became a research brief
    # once, and the real request never happened.
    repl._awaiting_user_answer = answer.startswith("等待你的回答：")
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
    elif answer.startswith("等待你的回答："):
        title = "Kernel · 等待回答"
    else:
        # Evidence is a label, not a verdict on the run.  The answer is shown
        # with how much of it the ledger can stand behind; only an execution
        # brake (loop, timeout, cancel) still reads as "stopped".
        title = f"Kernel · {_evidence_label(result.evidence_status.value, accepted)}"
    render(answer, title=title)
    if planned:
        console.print(
            "[dim]· 计划已接受，执行等待确认；本轮未写盘。[/dim]"
        )
        console.print(f"[dim]ledger: {run.ledger_path}[/dim]")
    elif result.debt:
        for item in result.debt[:5]:
            console.print(
                f"[yellow]· 未完成 {item.obligation.kind.value}: "
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
    listener = SteeringListener(repl)
    listener.start()
    guard = CancelOnInterrupt(None)
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
            guard.__exit__()
            listener.stop()
            repl._active_runtime = None
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
        # #373: the front-end owns the session's remembered decisions.  A grant
        # made in an earlier task must apply to this one, and this task's answer
        # must be remembered for the next.  Without this the kernel records
        # GRANT_ADDED and still asks again on the next task — the earlier probe
        # wired the book by hand; the product must not.
        from xenon.kernel.session_grants import GrantBook

        book = getattr(repl, "_grant_book", None)
        if book is None:
            book = GrantBook(workspace=str(adapter.workspace))
            repl._grant_book = book
        adapter.session_grants = tuple(book.active(str(adapter.workspace)))
        sink = ReplEventSink(callback)
        start_capture = getattr(repl, "_start_log_capture", None)
        if callable(start_capture):
            start_capture()
        adapter.progress_sink = kernel_progress_reporter(repl)
        adapter.request_sink = kernel_request_reporter(repl)
        # V2 coverage needs a proposer: without one, every semantic obligation
        # can only ever abstain (#286).  The proposer only points at receipts;
        # the binder still decides, and proposal-backed verdicts stay PARTIAL.
        adapter.audit_provider = _audit_proposer(adapter.model_config)
        # The status bar shows the loop that is actually executing; ``/mode``
        # is chat configuration and would claim a mode the agent is not using.
        status_bar = getattr(repl, "status_bar", None)
        loop_setter = getattr(status_bar, "set_active_loop", None)
        if callable(loop_setter):
            loop_setter(adapter.strategy_mode)
        guard = CancelOnInterrupt(None)

        def _attach(runtime: Any) -> None:
            repl._active_runtime = runtime
            runtime.attach_steering_source(lambda: drain_steering(repl))
            guard._runtime = runtime
            guard.__enter__()

        listener = SteeringListener(repl)
        listener.start()
        blocks = session_context_blocks(repl, objective)
        # #372 B3: a task reached through a rewind/fork carries the branch path
        # into the kernel view (Turn path: line).  Linear chat passes (); the
        # tree decides, never the REPL guessing.
        tree = getattr(repl, "_turn_tree", None)
        turn_path = ()
        if tree is not None:
            branch = getattr(tree, "turn_path", None)
            if callable(branch):
                try:
                    turn_path = branch()
                except Exception:  # noqa: BLE001 — cosmetic
                    turn_path = ()

        try:
            run = adapter.run(
                objective,
                user_objective=user_input,
                sink=sink,
                on_runtime=_attach,
                context_blocks=blocks,
                turn_path=turn_path,
            )
            # Remember the human's decision so the next task in this session
            # does not ask again.  The kernel already wrote GRANT_ADDED; this is
            # where the grant outlives the task it was made in.
            runtime = getattr(repl, "_active_runtime", None)
            grant = getattr(runtime, "last_grant", None)
            if grant is not None:
                book.add(grant)
        finally:
            if callable(loop_setter):
                loop_setter(None)
            status_setter = getattr(status_bar, "set_kernel_status", None)
            if callable(status_setter):
                # #372 E1: no kernel turn is running; the bar must fall back
                # to /mode config instead of showing a stale "last loop".
                status_setter(None)
            guard.__exit__()
            listener.stop()
            repl._active_runtime = None
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
    "kernel_progress_reporter",
    "kernel_request_reporter",
    "model_config_for",
    "permission_mode_for",
    "resume_kernel_plan",
    "run_kernel_turn",
    "should_route_to_kernel",
]
