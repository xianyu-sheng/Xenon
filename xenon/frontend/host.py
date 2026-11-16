"""NDJSON protocol host: the Python side of the TS front-end boundary.

The design is in ``docs/design/frontend-protocol.md``.  This module implements
step 1 of that document's migration plan: a process that speaks the protocol
and runs real turns, while the existing REPL keeps working untouched.

What it does **not** do: render anything, own any authorization, or compute any
evidence.  It forwards kernel facts as JSON lines and blocks on
``approval.request`` until the front-end answers.  The evidence block in
``turn.end`` is produced by the kernel and passed through unchanged — a
front-end that recomputed it would be inventing the product's only claim.

Usage::

    python -m xenon.frontend.host --model deepseek/deepseek-v4-flash

Reads one JSON object per line on stdin, writes one per line on stdout.
Diagnostics go to stderr so they cannot corrupt the stream.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, TextIO

__all__ = ["PROTOCOL_VERSION", "ProtocolHost", "main"]

PROTOCOL_VERSION = 1

#: What the welcome card and the status bar call the execution model.  There
#: is exactly one loop; a mode is a phase profile inside it, so the UI must not
#: show the internal preset name as if it were something to choose.
LOOP_LABEL = "单循环"


def _emit(stream: TextIO, payload: Mapping[str, Any]) -> None:
    """Write one frame.  A single lock keeps concurrent sinks from interleaving."""

    with _EMIT_LOCK:
        stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        stream.flush()


_EMIT_LOCK = threading.Lock()


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _version() -> str:
    """The running package version — the single source is ``xenon/__init__``."""

    try:
        from xenon import __version__

        return str(__version__)
    except Exception:  # noqa: BLE001 — a welcome card must never fail startup
        return ""


@dataclass
class _ApprovalRequest:
    """One blocked approval, waiting for the front-end's answer."""

    message_id: str
    settled: threading.Event
    choice: str = ""


class ProtocolCallback:
    """Kernel callbacks re-expressed as protocol messages.

    Mirrors ``ConsoleCallback``'s surface, but every method emits JSON.  It is
    deliberately tiny: the front-end decides how to render, and nothing here
    decides anything.
    """

    def __init__(self, host: ProtocolHost) -> None:
        self.host = host

    def on_think(self, thought: str) -> None:
        self.host.frame(type="action", kind="think", text=str(thought))

    def on_act(self, action: str, action_input: dict) -> None:
        self.host.frame(
            type="action",
            kind="tool",
            tool=str(action),
            input=_safe(action_input),
        )

    def on_observe(self, observation: str) -> None:
        self.host.frame(type="action", kind="observe", text=str(observation)[:4000])

    def on_step(self, step_id: int, total: int, task: str) -> None:
        self.host.frame(type="action", kind="step", index=int(step_id), text=str(task))

    def on_step_done(self, step_id: int, success: bool, summary: str) -> None:
        self.host.frame(
            type="action",
            kind="step_done",
            index=int(step_id),
            status="succeeded" if success else "failed",
            text=str(summary)[:2000],
        )

    def on_review(self, score: int, passed: bool, feedback: str) -> None:
        self.host.frame(
            type="action", kind="review", score=int(score), status=str(passed),
            text=str(feedback)[:2000],
        )

    def on_error(self, error: str) -> None:
        self.host.frame(type="action", kind="error", text=str(error)[:4000])

    def on_warning(self, warning: str) -> None:
        self.host.frame(type="action", kind="warning", text=str(warning)[:2000])

    def on_tip(self, tip: str) -> None:
        self.host.frame(type="action", kind="tip", text=str(tip)[:500])

    def on_finish(self, result: str) -> None:
        # The authoritative end-of-turn message is ``turn.end``, which carries
        # evidence; this is only the human-readable answer.
        self.host.final_answer = str(result)

    # ── the three sinks the adapter binds ──────────────────────────

    def progress(self, seconds: float, chars: int) -> None:
        self.host.frame(
            type="progress",
            elapsed_seconds=round(float(seconds), 2),
            chars=int(chars),
        )

    def provider(self, event: dict[str, Any]) -> None:
        # Forwarded verbatim apart from the envelope: only the kernel knows
        # whether this is a first attempt or a retry.
        self.host.frame(type="provider", **{k: _safe(v) for k, v in dict(event).items()})


def _safe(value: Any) -> Any:
    """Make a value JSON-encodable without inventing content."""

    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe(v) for v in value]
    return str(value)


class ProtocolHost:
    """Run turns for a front-end that speaks the protocol.

    The host owns exactly one ``REPL`` instance (so model config, session
    bookkeeping and the hook registry behave as they do interactively) and
    swaps its three UI-facing hooks for protocol output.
    """

    def __init__(
        self,
        model_ids: list[str],
        *,
        out: TextIO | None = None,
        cwd: str | Path | None = None,
    ) -> None:
        self.model_ids = list(model_ids)
        self.out = out or sys.stdout
        self.cwd = Path(cwd) if cwd else Path.cwd()
        self.final_answer = ""
        self.turn = ""
        #: The worker running the current turn; None when no turn has started.
        self.turn_thread: threading.Thread | None = None
        self._pending: dict[str, _ApprovalRequest] = {}
        self._pending_lock = threading.Lock()
        self._counter = 0
        self.repl: Any = None
        #: A command runs off the stdin loop for the same reason a turn does:
        #: its handlers may ask a question, and a blocked reader could never
        #: deliver the answer.
        self.command_thread: threading.Thread | None = None
        self._prompt_waiters: dict[str, threading.Event] = {}
        self._prompt_answers: dict[str, str] = {}
        #: Set by ``turn.cancel``.  A turn runs on a worker thread, so Ctrl+C
        #: arrives as a protocol frame, not as SIGINT; this flag is the one
        #: cancel signal every path can read (direct chat and kernel alike).
        self._turn_cancel = threading.Event()
        self.turn_started_at = 0.0
        #: Session wall-clock start.  The interactive status bar reports how
        #: long the session has been open, not just the current turn.
        self.session_started_at = time.time()
        #: Whether the kernel has no credentials/models at all (local facts only).
        self.setup_needed = False
        #: The objective of the turn in flight, for the dashboard snapshot.
        self.turn_objective = ""
        #: The last finished kernel run and its ledger.  Read-only panels
        #: (diff, evidence, plan) are views of these facts; storing them here
        #: is what keeps a query from re-running anything.
        self.last_run: Any = None
        self.last_ledger: Path | None = None

    def _sync_kernel_view(self, previous_run: Any = None) -> None:
        """Adopt the kernel run the REPL most recently published.

        A kernel run can start from two places: a turn (``run_turn`` returns
        it here) and a command (``/plan approve`` resumes the approved plan on
        the command worker).  Only the turn path used to update ``last_run``,
        so after an approved plan the panels kept describing the *plan-only*
        run — the evidence panel said ``unverified`` while the ledger said the
        plan had delivered a verified commit.  Every kernel run now lands on
        ``repl._last_kernel_run``; reading that one pointer keeps the panels in
        agreement with the ledger no matter which entry produced the run.
        """

        run = getattr(self.repl, "_last_kernel_run", None)
        if run is not None and run is not previous_run:
            self.last_run = run
            ledger = str(getattr(run, "ledger_path", "") or "")
            if ledger:
                self.last_ledger = Path(ledger)
            return
        # A third producer installs evidence without a run: ``/resume`` (also
        # the startup ``--resume``) restores the REPL's ledger pointer so
        # ``/evidence`` works, but the host kept its old view and the dashboard
        # reported "没有可读的账本" for a session the terminal could inspect.
        # The pointer is the same fact the terminal reads, so adopt it — and
        # drop a run that belongs to a different ledger, or the status would
        # come from the previous session's result.
        ledger = getattr(self.repl, "_last_kernel_ledger", None)
        if not ledger:
            return
        path = Path(str(ledger))
        if path == self.last_ledger or not path.exists():
            return
        self.last_ledger = path
        run_path = getattr(self.last_run, "ledger_path", None)
        if run_path is None or Path(str(run_path)) != path:
            self.last_run = None

    # ── frames ─────────────────────────────────────────────────────

    def frame(self, **fields: Any) -> None:
        payload = {"v": PROTOCOL_VERSION}
        if self.turn:
            payload["turn"] = self.turn
        payload.update(fields)
        _emit(self.out, payload)

    def _next_id(self, prefix: str) -> str:
        with self._pending_lock:
            self._counter += 1
            return f"{prefix}-{self._counter}"

    # ── lifecycle ──────────────────────────────────────────────────

    def start(self) -> None:
        """Build the REPL the host will drive.  Mirrors ``start_repl`` wiring."""

        from xenon.repl.model_registry import ModelRegistry
        from xenon.repl.repl import REPL

        registry = ModelRegistry()
        # The interactive REPL loads models.yaml first and then derives any
        # missing entries from credentials.  Loading only credentials here made
        # the same model id resolve to a different endpoint (and fail 401) in
        # the front-end path.
        models_file = Path.home() / ".xenon" / "models.yaml"
        if models_file.exists():
            registry.load_from_file(models_file)
        registry.load_from_credentials()
        self.repl = REPL(registry=registry, streaming=False)
        # ``start_repl`` feeds the pool from the registry before any turn runs;
        # the host must do the same.  Without it ``model_pool`` stays empty: the
        # welcome summary reports "0 loaded" while the registry knows ten
        # models, and AutoRouter loses its only data source.
        pool = getattr(self.repl, "model_pool", None)
        from_config = getattr(pool, "from_config", None)
        export_config = getattr(registry, "export_config", None)
        if callable(from_config) and callable(export_config):
            try:
                from_config(export_config().get("models", {}))
            except Exception:  # noqa: BLE001 — pool wiring must never block boot
                _log("model pool wiring failed")
        if self.model_ids:
            # Same precedence as ``start_repl --model``: an explicit choice
            # outranks whatever the router would pick.
            self.repl._preferred_model_ids = list(self.model_ids)
        # The protocol owns the real stdout.  Anything the REPL prints through
        # a stdout-backed console (errors, command internals) would corrupt the
        # NDJSON stream, so process stdout becomes a diagnostic sink; frames
        # keep writing to ``self.out``, captured at construction.
        if self.out is sys.stdout:
            sys.stdout = sys.stderr
        self.repl._protocol_callback = ProtocolCallback(self)
        # Direct chat and the kernel loop both poll this probe while a turn is
        # in flight (see ``turn.cancel``).  It stays installed for the life of
        # the session; the event behind it is cleared when a turn starts.
        self.repl._turn_cancel_probe = self._turn_cancel.is_set
        # The three hooks the adapter binds today; same signatures, new sink.
        self.repl._start_log_capture = lambda: None
        self.repl._stop_log_capture = lambda: ""
        # The adapter asks the front-end through ``_confirm_tool``; rebinding it
        # is the whole seam — the kernel and the adapter stay unchanged.
        self.repl._confirm_tool = self._confirm_tool
        # Plan approval is the second blocking exchange; without this the
        # plan-only turn would call Prompt.ask directly on the protocol stdin.
        self.repl.agent_context.set_plan_callback(self._confirm_plan)
        # The default progress/request reporters print to the console, which
        # here *is* the protocol stream: a stray "· 请求中" line made the
        # front-end drop everything after it as unparseable.  These sinks emit
        # frames instead.
        callback = self.repl._protocol_callback
        self.repl._progress_sink_override = callback.progress
        self.repl._request_sink_override = callback.provider
        self._bootstrap_extensions()
        summary = self._first_run_summary()
        self.setup_needed = bool(summary.get("needs_setup"))
        # The front-end renders the welcome card (logo, version, mode, model
        # count, first-run hint).  Every one of those is a kernel fact, so the
        # startup frame carries them; a front-end that guessed a version or a
        # mode would be inventing product copy.  Startup stays local-only —
        # ``probed`` is False because provider discovery is lazy (v0.9.0).
        from xenon.modes import loop_phases

        mode = None
        get_mode = getattr(registry, "get_current_mode", None)
        if callable(get_mode):
            try:
                mode = get_mode()
            except Exception:  # noqa: BLE001 — the card can omit the loop line
                mode = None
        profile = getattr(mode, "loop", None)
        self.frame(
            type="ready",
            models=[m.alias for m in registry.list_models()],
            cwd=str(self.cwd),
            setup_needed=self.setup_needed,
            version=_version(),
            # The card shows the loop, never the internal preset name: a mode
            # is a phase profile of the one loop, so the phases are the only
            # difference worth displaying (and the only one that is true).
            mode=LOOP_LABEL,
            mode_description=loop_phases(profile),
            model_count=len(self._model_pool_ids()),
            loaded_models=int(summary.get("loaded_models") or 0),
            probed=bool(summary.get("probed")),
            available_providers=int(summary.get("available_providers") or 0),
            failures=[
                {"name": str(name), "reason": str(reason)}
                for name, reason in (summary.get("failures") or [])
            ],
        )

    def _bootstrap_extensions(self) -> None:
        """Load dynamic commands and persisted MCP servers, like ``REPL.run``.

        The host is the only interactive entry now, so it must run the same
        extension bootstrap the Python loop does; skipping it left ``/mcp
        list`` empty after a restart and kept saved shortcut/skill commands
        invisible.  Console writes are captured and dropped: stdout carries
        NDJSON here and stderr is the Ink surface.
        """

        import contextlib
        import io

        bootstrap = getattr(self.repl, "bootstrap_extensions", None)
        if not callable(bootstrap):
            return
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                bootstrap()
            except Exception:  # noqa: BLE001 — an extension must not block boot
                _log("extension bootstrap failed")

    def _model_pool_ids(self) -> list[str]:
        """Configured model ids from the pool (local facts, no probing)."""

        pool = getattr(self.repl, "model_pool", None)
        list_all = getattr(pool, "list_all", None)
        if not callable(list_all):
            return []
        try:
            return list(list_all())
        except Exception:  # noqa: BLE001 — a count must never block startup
            return []

    def _first_run_summary(self) -> dict[str, Any]:
        """Local-only first-run check (no provider probing, no network).

        The startup hint used to live in the interactive Python loop; with the
        front-end as the only entry it has to come from here, or a user with no
        credentials sees an empty model list and a turn that cannot run.
        """

        check = getattr(self.repl, "_check_first_run", None)
        if not callable(check):
            return {}
        try:
            return dict(check() or {})
        except Exception:  # noqa: BLE001 — a hint must never block startup
            _log("first-run check failed")
            return {}

    def _setup_needed(self) -> bool:
        """Whether the session has no credentials and no configured models."""

        return bool(self._first_run_summary().get("needs_setup"))

    def _confirm_tool(
        self, tool_name: str, params: dict, risk: str, *, from_child: bool = False
    ) -> tuple[bool, str]:
        """Answer the kernel's REQUIRE_APPROVAL through the protocol.

        Same closed result contract as the interactive panel: a tuple of
        ``(approved, reason)``, and ``_last_approval_scope`` carries what the
        human chose to remember so the runtime can record it.
        """

        from xenon.repl.permission_map import capability_for_tool

        self.repl._last_approval_scope = ""
        capability = capability_for_tool(tool_name)
        targets = _targets_for(tool_name, params)
        choice = self.request_approval(
            capability=capability.value if capability is not None else str(tool_name),
            tool=tool_name,
            targets=targets,
            reason=f"风险等级 {risk}",
            risk=str(risk),
        )
        if choice in {"once", "turn", "allow"}:
            if choice == "turn":
                # The runtime only knows "session"/"scoped"; a turn-scoped
                # answer is recorded here, against this turn's id, and pushed
                # to the running policy so it applies immediately.
                self._remember_turn(capability, targets)
            elif choice == "allow":
                # The durable half (§9: tool + path, 30 min) lives in the
                # session GrantBook.  Only the interactive bridge used to
                # write it, so an "allow" given here was forgotten the moment
                # the run ended: the next turn asked again and /permissions
                # listed nothing.
                from xenon.repl.approval_ux import remember_allow_answer

                remember_allow_answer(self.repl, tool_name, params)
            self.repl._last_approval_scope = {
                "once": "",
                "turn": "",
                "allow": "session",
            }[choice]
            return True, ""
        if choice == "cancel":
            # "Cancel the whole task" must stop the run, not just this call:
            # the kernel sees a refusal as one failed tool and keeps planning.
            # Same contract as the interactive panel.
            from xenon.repl.approval_ux import cancel_running_task

            cancel_running_task(self.repl)
            return False, "用户取消任务"
        return False, "用户拒绝"

    def _remember_turn(self, capability: Any, targets: list[str]) -> None:
        from xenon.kernel.session_grants import TURN_ALLOW, grant_from_choice

        book = getattr(self.repl, "_grant_book", None)
        if book is None or capability is None or not self.turn:
            return
        grant = grant_from_choice(
            TURN_ALLOW,
            capability=capability,
            targets=targets or ["*"],
            workspace=str(getattr(book, "workspace", "") or self.cwd),
            reason=f"this turn: {capability.value}",
            turn=self.turn,
        )
        if grant is None:
            return
        book.add(grant)
        runtime = getattr(self.repl, "_active_runtime", None)
        for policy in getattr(runtime, "_policies", ()) if runtime else ():
            add = getattr(policy, "grant", None)
            if callable(add):
                try:
                    add(grant.as_scoped())
                except Exception:  # noqa: BLE001 — a grant must not break a turn
                    _log("policy rejected the turn grant")

    def _confirm_plan(self, plan_text: str) -> dict:
        """Plan approval over the protocol (the direct ``Prompt.ask`` blocks)."""

        choice = self.request_approval(
            capability="plan",
            tool="plan",
            targets=[],
            reason="计划需要批准后才能执行",
            risk="PLAN",
            diff_preview=str(plan_text)[:4000],
            kind="plan",
        )
        if choice in {"once", "turn", "allow"}:
            return {"approved": True, "feedback": ""}
        if choice == "cancel":
            return {"approved": False, "feedback": "用户取消"}
        return {"approved": False, "feedback": "用户驳回，请修改计划后重新提交"}

    @contextlib.contextmanager
    def _protocol_prompts(self, flush: Any = None) -> Any:
        """Route ``rich.prompt``, the selector and the setup wizard to frames.

        ``flush`` is called right before a question is sent.  Commands print a
        table and *then* ask ("which model?"); without the flush the table
        stayed in the capture buffer until the command ended, so the user saw
        the question first and the list afterwards.

        The patch is scoped: making it process-global leaked into later tests
        (and into any embedded host), leaving a dead waiter behind.  Commands
        are the only place that asks questions, and only one runs at a time.
        """

        import rich.prompt as rich_prompt

        import xenon.repl.selector as selector_mod
        import xenon.repl.setup_wizard as setup_wizard_mod

        originals = (
            rich_prompt.Prompt.ask,
            rich_prompt.Confirm.ask,
            rich_prompt.IntPrompt.ask,
            selector_mod.ask_choice,
            setup_wizard_mod._masked_input,
        )

        def masked_input(prompt_text: str = "") -> str:
            # The wizard's key entry reads the terminal byte-by-byte for its
            # ``*`` mask.  Under the front-end the terminal belongs to Node, so
            # that read would swallow nothing and block forever; ask the UI.
            return self._protocol_prompt(
                str(prompt_text), default="", choices=[], secret=True
            )

        def ask(prompt: str = "", **kwargs: Any) -> str:
            default = kwargs.get("default", "")
            choices = kwargs.get("choices") or []
            value = self._protocol_prompt(
                str(prompt),
                default=str(default or ""),
                choices=[str(item) for item in choices],
                secret=bool(kwargs.get("password", False)),
            )
            return value if value != "" else str(default or "")

        def confirm(prompt: str = "", **kwargs: Any) -> bool:
            default = bool(kwargs.get("default", True))
            value = self._protocol_prompt(
                str(prompt),
                default="y" if default else "n",
                choices=["y", "n"],
                secret=False,
            ).strip().lower()
            return value in {"y", "yes", "true", "1"}

        def int_ask(prompt: str = "", **kwargs: Any) -> int:
            default = kwargs.get("default", 0)
            value = self._protocol_prompt(
                str(prompt), default=str(default or ""), choices=[]
            )
            try:
                return int(str(value).strip())
            except (TypeError, ValueError):
                return int(default or 0)

        def ask_choice(
            repl: Any, *, title: str, choices: Any, attempts: int = 2
        ) -> str | None:
            rows = list(choices)
            if not rows:
                return None
            labels = [
                f"{index + 1}. {getattr(row, 'label', row)}"
                + (f" — {row.detail}" if getattr(row, "detail", "") else "")
                for index, row in enumerate(rows)
            ]
            answer = self._protocol_prompt(
                str(title), default="", choices=labels
            ).strip()
            if answer.isdigit():
                index = int(answer) - 1
                if 0 <= index < len(rows):
                    return str(getattr(rows[index], "value", rows[index]))
            for row in rows:
                value = str(getattr(row, "value", row))
                label = str(getattr(row, "label", row))
                if answer in {value, label}:
                    return value
            return None

        rich_prompt.Prompt.ask = ask
        rich_prompt.Confirm.ask = confirm
        rich_prompt.IntPrompt.ask = int_ask
        selector_mod.ask_choice = ask_choice
        setup_wizard_mod._masked_input = masked_input
        previous_flush = getattr(self, "_prompt_flush", None)
        self._prompt_flush = flush
        try:
            yield
        finally:
            self._prompt_flush = previous_flush
            (
                rich_prompt.Prompt.ask,
                rich_prompt.Confirm.ask,
                rich_prompt.IntPrompt.ask,
                selector_mod.ask_choice,
                setup_wizard_mod._masked_input,
            ) = originals

    def _protocol_prompt(
        self,
        message: str,
        *,
        default: str = "",
        choices: list[str] | None = None,
        secret: bool = False,
    ) -> str:
        message_id = self._next_id("p")
        event = threading.Event()
        # Anything the command printed before asking belongs on screen first.
        flush = getattr(self, "_prompt_flush", None)
        if callable(flush):
            flush()
        with self._pending_lock:
            self._prompt_waiters[message_id] = event
        self.frame(
            type="prompt.request",
            id=message_id,
            message=str(message),
            default=default,
            choices=list(choices or []),
            secret=secret,
        )
        if not event.wait(timeout=_approval_timeout()):
            with self._pending_lock:
                self._prompt_waiters.pop(message_id, None)
                self._prompt_answers.pop(message_id, None)
            _log(f"prompt {message_id} timed out; using the default")
            return default
        with self._pending_lock:
            self._prompt_waiters.pop(message_id, None)
            return self._prompt_answers.pop(message_id, default)

    def resolve_prompt(self, message_id: str, value: str) -> bool:
        with self._pending_lock:
            event = self._prompt_waiters.get(message_id)
            if event is None:
                return False
            self._prompt_answers[message_id] = str(value)
        event.set()
        return True

    def cancel_turn(self) -> bool:
        """Stop the work in flight; False when there is nothing to stop.

        Two paths run here: a kernel loop (which has a runtime) and direct
        chat (which has none).  Binding the flag to the runtime alone made
        Ctrl+C a no-op for a plain conversation -- the essay it was writing
        arrived in full.  The flag is therefore owned by the host for the
        duration of the turn, and the runtime (when present) is told as well
        so an in-flight tool step is interrupted instead of finishing first.
        """

        runtime = getattr(self.repl, "_active_runtime", None)
        busy = runtime is not None or any(
            thread is not None and thread.is_alive()
            for thread in (self.turn_thread, self.command_thread)
        )
        if not busy:
            return False
        self._turn_cancel.set()
        cancel = getattr(runtime, "request_cancel", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:  # noqa: BLE001 — cancel is best effort
                _log("runtime cancel failed")
        # A turn parked on an approval panel cannot see the flag: the kernel
        # waits on this event, not in its loop.  Answering "cancel" is the same
        # closed choice the panel offers, so the runtime unwinds instead of
        # holding Ctrl+C hostage until the 300 s timeout.
        with self._pending_lock:
            waiting = list(self._pending.values())
        for request in waiting:
            request.choice = "cancel"
            request.settled.set()
        return True

    def select_session(self, name: str) -> None:
        role = str(name or "").strip()
        if not role:
            self.frame(type="error", error="session.select without a name")
            return
        if self.turn_thread is not None and self.turn_thread.is_alive():
            self.frame(type="error", error="session.select: a turn is in progress")
            return
        worker = threading.Thread(
            target=self.run_command,
            args=("/resume", role),
            name="xenon-session-select",
            daemon=True,
        )
        self.command_thread = worker
        worker.start()

    def _current_models(self) -> list[str]:
        """Models the *next* turn should use.

        ``/set_model`` and ``/model`` update ``repl._preferred_model_ids``; the
        constructor list is only the startup default.  Reading it at turn time
        is what makes a switch take effect without restarting the session.
        """

        preferred = getattr(self.repl, "_preferred_model_ids", None)
        if preferred:
            rows = [str(item) for item in preferred if str(item).strip()]
            if rows:
                return rows
        return self.model_ids

    def _run_chat(self, objective: str, contract: Any) -> None:
        """One conversational turn: answer, record, close the turn."""

        from xenon.ledger.conversation import (
            default_ledger_dir,
            finish_conversation_turn,
            open_conversation_turn,
        )
        from xenon.repl.runtime_adapter import _session_id_of

        chat_turn = open_conversation_turn(
            default_ledger_dir(),
            objective=objective,
            user_objective=objective,
            workspace=str(self.cwd),
            session_id=_session_id_of(self.repl, None),
            model_ids=self._current_models(),
        )
        outcome = None
        failure = ""
        prompt = objective
        memory_answer = ""
        try:
            # The direct path expects the turn's request message to be in the
            # conversation already (the REPL persists it before dispatching).
            # Without it the provider sees an empty conversation and answers
            # 400 — and, for a memory-gated turn, nothing was recorded at all,
            # so ``/save`` wrote an empty session that ``--resume`` then
            # reported as "不存在".
            context = getattr(self.repl, "ctx_mgr", None)
            add_request = getattr(context, "add_request_message", None)
            if callable(add_request):
                prompt = add_request(
                    objective,
                    metadata={
                        "intent": "",
                        "original_user_input": objective,
                        "intent_source": "",
                        "contextual_followup": False,
                        "continuation": False,
                    },
                )
            # "记住……" is an instruction, not a conversation: the interactive
            # REPL persists it inside ``turn_flow`` before any model call.  The
            # host dispatched straight to ``_run_direct``, so the model used to
            # answer "已记住" while the memory service was never touched.
            # Running the same gate here keeps both entries honest.  Its
            # receipt is the turn's answer, so the conversation records the
            # pair instead of an unanswered user message.
            memory_answer = self._explicit_memory_answer(objective)
            if memory_answer:
                add_answer = getattr(context, "add_assistant_message", None)
                if callable(add_answer):
                    add_answer(memory_answer)
            else:
                outcome = self.repl._run_direct(
                    prompt,
                    self._current_models(),
                    intent=None,
                    execution_policy=contract.to_execution_policy(),
                )
        except Exception as exc:  # noqa: BLE001 — a frame, not a crash
            failure = f"{type(exc).__name__}: {exc}"
        finally:
            delegated = bool(getattr(outcome, "delegated", False))
            path = finish_conversation_turn(
                chat_turn,
                answer=memory_answer or (getattr(outcome, "answer", "") or ""),
                model_id=getattr(outcome, "model_id", "") or "",
                error=getattr(outcome, "error", "") or failure,
                delegated=delegated,
            )
        answer = memory_answer or (getattr(outcome, "answer", "") or "")
        error = "" if memory_answer else (getattr(outcome, "error", "") or failure)
        cancelled = bool(getattr(outcome, "cancelled", False))
        run = getattr(outcome, "run", None)
        # A chat turn still has a ledger (the conversation one); the panels
        # open it even when there is no kernel run to project.
        self.last_ledger = Path(path) if path is not None else None
        if run is not None:
            self.last_run = run
        if delegated and run is not None:
            # The direct path found the model reaching for a tool and handed
            # the turn to the kernel.  That kernel run *is* the turn: report its
            # answer, evidence, ledger and counters.  The host used to replace
            # all of it with "模型尝试执行工具；请把这一轮改写成工具任务" while the
            # work had already happened in a ledger the user never saw.
            self.frame(type="turn.end", **_turn_end_fields(run, self.final_answer or answer))
        elif delegated:
            from xenon.utils.errors import envelope

            cause = getattr(self.repl, "_last_kernel_error", None)
            problem = (
                envelope(cause)
                if isinstance(cause, BaseException)
                else envelope(answer or "kernel turn failed")
            )
            self.frame(
                type="turn.end",
                phase="failed",
                answer="",
                error=problem.message,
                error_kind=problem.kind,
                retryable=problem.retryable,
                ledger=str(path) if path is not None else None,
            )
        elif cancelled:
            # Not a failure: the operator said stop.  The closed ledger path is
            # still reported, so the aborted turn is auditable like any other.
            self.frame(
                type="turn.end",
                phase="aborted",
                answer="",
                accepted=False,
                cancelled=True,
                ledger=str(path) if path is not None else None,
            )
        else:
            self.frame(
                type="turn.end",
                phase="finalized" if answer else "failed",
                answer=answer,
                accepted=bool(answer),
                error=error,
                # A memory receipt is produced by a deterministic write, so the
                # claim on screen is backed by the service, not by the model.
                evidence={"status": "verified" if memory_answer else "unverified"},
                ledger=str(path) if path is not None else None,
            )
        self.turn = ""

    def _explicit_memory_answer(self, objective: str) -> str:
        """Persist an explicit "记住……" request and return its visible receipt.

        Empty means "not a memory request"; the turn proceeds normally.  The
        handler renders through the REPL's rich console, which in protocol mode
        points at a diagnostic sink, so stdout is captured here and the text
        becomes this turn's answer instead of vanishing.
        """

        handler = getattr(self.repl, "_handle_explicit_memory_request", None)
        if not callable(handler):
            return ""
        # Scope selection reads the detected project; the adapter injects it
        # for kernel turns, but a chat turn reaches the host first.
        inject = getattr(self.repl, "_inject_project_context", None)
        if callable(inject):
            try:
                inject()
            except Exception:  # noqa: BLE001 — context is advisory
                _log("project context injection failed before memory gate")
        captured = io.StringIO()
        try:
            with contextlib.redirect_stdout(captured):
                handled = handler(objective)
        except Exception:  # noqa: BLE001 — a chat turn must never fail on this
            _log("explicit memory gate failed")
            return ""
        if not handled:
            return ""
        text = _plain_text(captured.getvalue()).strip()
        return text or "· 记忆请求已处理（/memory list 查看）"

    def _resolve_contract(self, objective: str) -> tuple[Any, Any, str, str]:
        """The turn contract, resolved the same way the interactive REPL does.

        Falls back to the regex-only classifier for a bare REPL (embedded
        hosts, tests) that has no resolver.  A resolver failure must not kill
        the turn: an over-conservative contract still answers, it just may
        refuse a write, and that refusal is visible.
        """

        from xenon.repl.turn_contract import build_turn_contract

        resolve = getattr(self.repl, "_resolve_turn_contract", None)
        if callable(resolve):
            try:
                contract, policy, intent, intent_source = resolve(objective)
                return contract, policy, str(intent or ""), str(intent_source or "")
            except Exception as exc:  # noqa: BLE001 — classification is not the task
                _log(f"turn contract resolver failed ({exc}); using regex signals")
        contract = build_turn_contract(objective)
        return contract, contract.to_execution_policy(), str(contract.intent or ""), ""

    def _apply_permission_ceiling(self, contract: Any) -> Any:
        """Clamp one turn to the session's ``/permissions`` mode.

        The mode is a durable ceiling over every turn, not only the interactive
        path.  ``run_turn`` recomputes the contract from the objective each
        turn, so a mode stored only as ``_execution_level`` was overwritten
        before it could constrain anything: through the front-end
        ``/permissions plan`` still let the turn write/execute.
        """

        ctx_get = getattr(getattr(self.repl, "agent_context", None), "get", None)
        if not callable(ctx_get):
            return contract
        try:
            mode_name = str(ctx_get("_permission_mode") or "")
        except Exception:  # noqa: BLE001 — a context read must not kill a turn
            return contract
        if not mode_name:
            return contract

        from xenon.repl.turn_contract import apply_permission_ceiling

        return apply_permission_ceiling(contract, mode_name)

    def run_turn(self, objective: str, level: str = "auto-edit") -> None:
        """One user turn.  Blocks until the kernel finishes it.

        The level is *rebound from the objective* exactly as the interactive
        REPL does.  Skipping that step made the kernel veto every shell call
        with "shell requires a grant" before it could even ask: the policy only
        offers an approval channel once the turn's level permits execution, and
        a turn whose objective says "run …" is an execution turn.  Measured on a
        real task through the TS front-end — no prompt ever appeared.
        """

        from xenon.repl.execution_policy import ExecutionLevel
        from xenon.repl.runtime_adapter import run_kernel_turn

        # A cancel belongs to the turn that asked for it, never to the next one.
        self._turn_cancel.clear()
        self.turn = self._next_id("t")
        self.turn_objective = objective
        self.turn_started_at = time.time()
        self.final_answer = ""
        self.last_run = None
        self.frame(type="turn.start", objective=objective)

        # One contract resolver for both entries.  The interactive REPL
        # resolves a turn through ``_resolve_turn_contract`` (regex signals +
        # then the LLM classifier, with the pending-action and task-state
        # context).  Calling ``build_turn_contract`` here instead made the
        # protocol a second, weaker classifier: a write task whose verb came
        # *after* the path ("在 x.txt 写入一行") had no structural signal, so
        # the front-end turn ran read-only and every write was vetoed, while
        # the same text in the Python TUI was authorised.
        contract, _policy, intent, intent_source = self._resolve_contract(objective)

        # An explicit --level from the front-end wins; otherwise the resolved
        # contract decides, which is what the interactive path has always done.
        requested = str(level or "").lower()
        if requested and requested not in {"auto", "auto-edit", "default"}:
            contract = _with_level(
                contract, _LEVELS.get(requested, int(contract.level))
            )

        # Plan mode is a constraint on the *turn*, applied after resolution so
        # the plan itself keeps its write/execute steps (``proposed_level``).
        # Only the interactive path used to do this; through the front-end
        # `/plan on` had no effect at all — the kernel started in TaskMode.AUTO
        # with every tool available.
        plan_active = bool(getattr(self.repl, "_plan_mode_active", False))
        plan_convert = getattr(self.repl, "_plan_mode_contract", None)
        if (
            plan_active
            and contract.level >= ExecutionLevel.WRITE
            and callable(plan_convert)
        ):
            contract = plan_convert(contract)

        contract = self._apply_permission_ceiling(contract)
        policy = contract.to_execution_policy()
        # Publish the write scope (named protected paths / relative "don't touch
        # the rest") into the agent context.  The interactive path did this in
        # ``turn_flow``; without it here the protocol host silently dropped the
        # user's constraint and let the kernel write any path.
        from xenon.repl.turn_contract import store_write_scope

        store_write_scope(getattr(self.repl, "agent_context", None), contract)
        plan_only = bool(plan_active and contract.deferred_write)
        # The session reports the level the turn is *authorised* at, not the
        # temporary read-only window a plan turn executes in.
        session_level = (
            int(contract.proposed_level) if plan_only else int(contract.level)
        )
        # The turn's level is the level the session is running at.  Without
        # writing it back, `/permissions` reports "未初始化" and the status bar
        # shows nothing, because both read it from the agent context.
        setter = getattr(self.repl.agent_context, "set", None)
        if callable(setter):
            setter("_execution_level", session_level)
            setter("_execution_reason", "protocol turn")

        callback = self.repl._protocol_callback
        if not policy.requires_tools:
            # Pure conversation: the kernel loop would plan, propose a tool and
            # get vetoed by the read-only boundary ("policy veto" on a
            # greeting).  Mirror the REPL's direct path, but emit frames and
            # record the turn in the ledger.
            self._run_chat(objective, contract)
            return
        # A plan-only turn may *plan* writes even though the runtime is
        # read-only; say so in the prompt exactly as the interactive path does,
        # or the model trims the write steps out of its own plan.
        turn_prompt = objective
        if plan_only:
            turn_prompt = (
                f"{objective}\n\n## 计划模式（kernel plan-only）\n"
                "计划可以包含写/执行步骤；本轮只提交计划，不执行任何工具，"
                "批准后才执行。不要因为执行边界是只读就删掉计划中的写步骤。"
            )
        # The interactive REPL records the request in the conversation before
        # every turn (``turn_flow``), and ``run_kernel_turn`` records the
        # answer when it renders the result.  The protocol host called the
        # adapter directly and skipped the first half: after a real tool task
        # ``ctx_mgr.history`` held only an assistant message, so ``/save``
        # persisted a conversation ``/load`` could not replay, ``/context``
        # undercounted, and ``--resume`` reported the saved session as "不存在"
        # because ``list_sessions`` drops empty-history files.
        context = getattr(self.repl, "ctx_mgr", None)
        add_request = getattr(context, "add_request_message", None)
        if callable(add_request):
            turn_prompt = add_request(
                turn_prompt,
                metadata={
                    "intent": str(intent or ""),
                    "original_user_input": objective,
                    "intent_source": str(intent_source or ""),
                    "contextual_followup": False,
                    "continuation": False,
                },
            )
        try:
            run = run_kernel_turn(
                self.repl,
                objective=turn_prompt,
                user_input=objective,
                model_ids=self._current_models(),
                execution_policy=policy,
                callback=callback,
                plan_only=plan_only,
                intent=str(intent or ""),
                intent_source=str(intent_source or ""),
            )
        except Exception as exc:  # noqa: BLE001 — a failed turn is a frame, not a crash
            from xenon.utils.errors import envelope

            problem = envelope(exc)
            # Additive fields: a client can retry on ``retryable`` without
            # parsing provider prose.  The message stays where it always was.
            self.frame(
                type="turn.end",
                phase="failed",
                answer="",
                error=problem.message,
                error_kind=problem.kind,
                retryable=problem.retryable,
            )
            self.turn = ""
            return

        if run is None:
            # ``run_kernel_turn`` reports a failed turn by returning None after
            # printing the reason.  A frame with an empty phase would look like
            # a successful, evidence-free run; say it failed — and say *why*.
            # The console print never reaches this stream, so the cause is
            # read back from the REPL and classified like any other boundary
            # error (network failures are retryable; an unknown cause is not).
            from xenon.utils.errors import envelope

            cause = getattr(self.repl, "_last_kernel_error", None)
            problem = (
                envelope(cause)
                if isinstance(cause, BaseException)
                else envelope(self.final_answer or "kernel turn failed")
            )
            self.frame(
                type="turn.end",
                phase="failed",
                answer="",
                error=problem.message,
                error_kind=problem.kind,
                retryable=problem.retryable,
            )
            self.turn = ""
            return

        # Keep the run for the read-only panels.  A query must be a view of
        # this run, never a second execution of the task.
        self.last_run = run
        self.last_ledger = Path(getattr(run, "ledger_path", "") or "") or None
        self.frame(type="turn.end", **_turn_end_fields(run, self.final_answer))
        self.turn = ""

    # ── slash commands ─────────────────────────────────────────────

    def run_command(self, name: str, args: str = "") -> None:
        """Run one ``/command`` and return what it printed.

        The handlers already return display text, so the front-end renders that
        instead of reimplementing 60 commands.  A command that wants to leave
        the session (``/exit``) is reported, not obeyed here — terminating the
        host is the front-end's decision.
        """

        from xenon.repl.command_registry import ExitSignal, dispatch_command

        # Some commands execute a turn of their own (/plan approve resumes the
        # approved plan).  That work is cancellable too, and a cancel from the
        # previous command must not leak into it.
        self._turn_cancel.clear()
        command_id = self._next_id("c")
        # One canonical session_state, shared with the REPL.  The host used to
        # build a two-key shadow dict, so any handler reading
        # ``session_state["model_pool"]`` (/pool, /set_profile, /import_models…)
        # saw nothing while the pool was in fact loaded — and the same hole
        # applied to the routers, terminal activity and memory service.
        session_state = getattr(self.repl, "_session_state", None)
        if not isinstance(session_state, dict):
            session_state = {}
        session_state["_repl"] = self.repl
        session_state["agent_context"] = self.repl.agent_context
        captured = io.StringIO()
        # A command may execute a kernel run (``/plan approve``).  Remember
        # which run the panels were showing so only a *new* run replaces it —
        # a plain command must not resurrect a run whose turn already failed.
        previous_run = getattr(self.repl, "_last_kernel_run", None)

        def flush_output() -> None:
            """Send what the command printed so far, before it asks a question."""

            text = _plain_text(captured.getvalue()).strip()
            if not text:
                return
            captured.seek(0)
            captured.truncate(0)
            self.frame(type="command.output", id=command_id, name=name, output=text)

        try:
            # Handlers are inconsistent: some return their text, others render
            # straight to a rich console.  Capture stdout for the duration so
            # both shapes reach the front-end instead of vanishing into stderr.
            # The prompt bridge is scoped to this call and restored in
            # ``finally``: a process-global patch would leak into later code.
            with self._protocol_prompts(flush_output), contextlib.redirect_stdout(
                captured
            ):
                output = dispatch_command(
                    name,
                    args,
                    registry=self.repl.registry,
                    ctx_mgr=self.repl.ctx_mgr,
                    session_state=session_state,
                    repl=self.repl,
                )
        except ExitSignal:
            self.frame(type="command.result", id=command_id, name=name, exit=True)
            return
        except Exception as exc:  # noqa: BLE001 — a command is never fatal
            self.frame(
                type="command.result", id=command_id, name=name, error=str(exc)[:2000]
            )
            return
        self._sync_kernel_view(previous_run)
        printed = _plain_text(captured.getvalue()).strip()
        returned = _plain_text("" if output is None else str(output)).strip()
        text = "\n".join(part for part in (printed, returned) if part)

        # ``/restart`` only *requests* a restart; the interactive REPL's main
        # loop performs it.  The protocol host has no such loop, so the
        # command answered "正在准备重启" and nothing ever happened.  The
        # manager performs an in-process reload; its outcome belongs in the
        # same command result the user is looking at.
        manager = getattr(self.repl, "_restart_manager", None)
        coordinator = getattr(manager, "coordinator", None)
        if coordinator is not None and hasattr(manager, "perform_restart"):
            should_restart, preserve = coordinator.should_restart()
            if should_restart:
                coordinator.clear()
                try:
                    outcome = manager.perform_restart(preserve)
                    text = "\n".join(
                        part
                        for part in (text, str(getattr(outcome, "message", "")))
                        if part
                    )
                except Exception as exc:  # noqa: BLE001 — a failed restart is not fatal
                    _log(f"restart failed: {exc}")
                    text = "\n".join(part for part in (text, f"⚠️ 重启失败: {exc}") if part)
        self.frame(
            type="command.result",
            id=command_id,
            name=name,
            # Command handlers return rich markup ("[dim]…[/dim]") because the
            # interactive REPL hands the string straight to rich.  Across the
            # protocol there is no rich, so the markup would arrive as literal
            # brackets in the terminal.  Strip it here, once, at the boundary.
            output=text,
        )

    def command_catalog(self) -> list[dict[str, str]]:
        """Every registered command, for the front-end's completer."""

        from xenon.repl.command_registry import COMMANDS

        rows: list[dict[str, str]] = []
        for name, meta in COMMANDS.items():
            rows.append(
                {
                    "name": name,
                    "summary": str(meta.get("description") or meta.get("help") or ""),
                    "usage": str(meta.get("usage") or ""),
                }
            )
        return rows

    #: Directories that never belong in a path completer.
    _FILE_SKIP_DIRS = frozenset(
        {
            ".git",
            ".hg",
            ".svn",
            "node_modules",
            "__pycache__",
            ".venv",
            "venv",
            "env",
            ".tox",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            "dist",
            "build",
            "target",
            ".idea",
            ".vscode",
            ".eggs",
            "coverage",
        }
    )

    def list_files(self, query: str, *, limit: int = 50) -> list[str]:
        """Workspace-relative paths for the ``@`` completer, bounded.

        Read-only and local: it lists names under the workspace root, never
        contents, never outside it.  Symlinked directories are not followed.
        """

        root = Path(self.cwd)
        needle = str(query or "").strip().lower()
        found: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            relative_dir = Path(dirpath).relative_to(root)
            dirnames[:] = [
                name
                for name in dirnames
                if name not in self._FILE_SKIP_DIRS and not name.endswith(".egg-info")
            ]
            if len(relative_dir.parts) >= 6:
                dirnames[:] = []
            for name in filenames:
                relative = (relative_dir / name).as_posix().lstrip("./")
                if needle and needle not in relative.lower():
                    continue
                found.append(relative)
                if len(found) >= limit:
                    return sorted(found)
        return sorted(found)

    def list_sessions(self) -> None:
        """Saved sessions, newest/most-used first.

        The front-end needs these to offer a picker; the REPL's own startup
        picker is not used here because the front-end owns the screen.
        """

        from xenon.repl.session import list_sessions

        try:
            rows = list_sessions()
        except Exception as exc:  # noqa: BLE001 — a broken session dir is not fatal
            self.frame(type="sessions", sessions=[], error=str(exc)[:500])
            return
        self.frame(
            type="sessions",
            sessions=[
                {
                    "name": str(row.get("name") or ""),
                    "saved_at": str(row.get("saved_at") or ""),
                    "messages": int(row.get("messages") or 0),
                }
                for row in rows
            ],
        )

    def session_report(self) -> None:
        """Savings report facts, emitted once when the session ends.

        Mirrors ``REPL._print_exit_report`` but ships numbers instead of a Rich
        panel: the front-end draws the panel, the kernel owns the accounting.
        Nothing is sent when no model call was made.
        """

        payload = self._report_payload()
        if payload is None:
            return
        self.frame(type="session.report", **payload)

    def _report_payload(self) -> dict[str, Any] | None:
        """The report numbers, or None when no model call was made.

        Shared by the exit frame and the dashboard's ``report.get`` so the two
        can never disagree about what the session cost.
        """

        tracker = getattr(self.repl, "_cache_tracker", None)
        if tracker is None:
            return None
        try:
            model_ids = list(tracker.all_models)
        except Exception:  # noqa: BLE001 — a report must never block exit
            return None
        rows: list[dict[str, Any]] = []
        total_tokens = total_cost = 0.0
        calls = 0
        for model_id in model_ids:
            try:
                snap = dict(tracker.model_snapshot(model_id) or {})
            except Exception:  # noqa: BLE001
                continue
            model_calls = int(snap.get("calls", 0) or 0)
            if model_calls == 0:
                continue
            prompt_t = int(snap.get("prompt_tokens", 0) or 0)
            comp_t = int(snap.get("completion_tokens", 0) or 0)
            reported = int(snap.get("cache_reported_calls", 0) or 0)
            cost = float(snap.get("cost_yuan", 0.0) or 0.0)
            calls += model_calls
            total_tokens += prompt_t + comp_t
            total_cost += cost
            rows.append(
                {
                    "model": str(model_id),
                    "calls": model_calls,
                    "tokens": prompt_t + comp_t,
                    "cache_hit_pct": (
                        round(float(snap.get("cache_hit_rate", 0.0) or 0.0) * 100, 1)
                        if reported
                        else None
                    ),
                    "cost_cny": round(cost, 4),
                }
            )
        if calls == 0:
            return None
        try:
            rate = (
                round(float(tracker.cache_hit_rate) * 100, 1)
                if int(getattr(tracker, "cache_reported_calls", 0) or 0)
                else None
            )
            savings = int(tracker.savings_pct)
            saved = float(getattr(tracker, "savings_yuan", 0.0) or 0.0)
        except Exception:  # noqa: BLE001
            rate, savings, saved = None, 0, 0.0
        return {
            "models": rows,
            "totals": {
                "calls": calls,
                "tokens": int(total_tokens),
                "cost_cny": round(total_cost, 4),
                "saved_cny": round(saved, 4),
                "savings_pct": savings,
                "cache_hit_pct": rate,
            },
        }

    #: Read-only query frames the dashboard may send.  They answer from facts
    #: the session already produced; none of them starts a turn or a tool.
    QUERY_KINDS = frozenset(
        {
            "dashboard.snapshot",
            "workspace.status",
            "diff.get",
            "ledger.events",
            "evidence.chain",
            "plan.get",
            "memory.list",
            "mcp.status",
            "report.get",
            "sessions.list",
            "sessions.tree",
        }
    )

    def handle_query(self, message: Mapping[str, Any]) -> None:
        """Answer one read-only query with ``<kind>.result``.

        A query must never take the session down: a broken ledger or a git
        failure becomes an ``error`` field in its own result frame.
        """

        from xenon.frontend.queries import DashboardQueries

        kind = str(message.get("type") or "")
        queries = DashboardQueries(self)
        try:
            if kind == "dashboard.snapshot":
                payload = queries.snapshot()
            elif kind == "workspace.status":
                payload = queries.workspace()
            elif kind == "diff.get":
                payload = queries.diff()
            elif kind == "ledger.events":
                payload = queries.ledger_events(
                    kinds=list(message.get("kinds") or []),
                    limit=int(message.get("limit") or 200),
                )
            elif kind == "evidence.chain":
                payload = queries.evidence_chain()
            elif kind == "plan.get":
                payload = queries.plan()
            elif kind == "memory.list":
                payload = queries.memory()
            elif kind == "mcp.status":
                payload = queries.mcp()
            elif kind == "report.get":
                payload = queries.report()
            elif kind == "sessions.list":
                payload = queries.sessions()
            elif kind == "sessions.tree":
                payload = queries.sessions_tree()
            else:  # pragma: no cover - main() filters before dispatching
                payload = {"error": f"unknown query: {kind!r}"}
        except Exception as exc:  # noqa: BLE001 — a panel must not kill the session
            payload = {"error": f"{type(exc).__name__}: {exc}"}
        payload = {str(key): value for key, value in payload.items()}
        self.frame(type=f"{kind}.result", id=str(message.get("id") or ""), **payload)

    def status(self) -> None:
        """Current model / mode / execution level, for the status bar."""

        from xenon.repl.execution_policy import ExecutionLevel

        level = None
        getter = getattr(self.repl.agent_context, "get", None)
        if callable(getter):
            level = getter("_execution_level")
        name = ""
        if level is not None:
            try:
                name = ExecutionLevel(int(level)).name.lower()
            except (TypeError, ValueError):
                name = str(level)
        self.frame(
            type="status",
            models=self._current_models(),
            mode=LOOP_LABEL,
            level=name,
            cwd=str(self.cwd),
            **self._status_facts(),
        )

    def _status_facts(self) -> dict[str, Any]:
        """Numbers the bar shows, all read from existing counters.

        Every value is a kernel fact: token counts and the window from
        ``ContextManager.stats()``, live phase/steps/tools from the runtime
        snapshot, cache/cost from the session's ``CacheTracker``.  Anything a
        source cannot answer is omitted — a made-up number in a status bar is
        worse than a missing one.
        """

        facts: dict[str, Any] = {}
        runtime = getattr(self.repl, "_active_runtime", None)
        snapshot = getattr(runtime, "status_snapshot", None)
        if callable(snapshot):
            try:
                facts.update(snapshot())
            except Exception:  # noqa: BLE001 — status must never fail
                pass
        context = getattr(self.repl, "ctx_mgr", None)
        stats_fn = getattr(context, "stats", None)
        stats: dict[str, Any] = {}
        if callable(stats_fn):
            try:
                stats = dict(stats_fn() or {})
            except Exception:  # noqa: BLE001 — status must never fail
                stats = {}
        if stats:
            try:
                ratio = str(stats.get("usage_ratio", "")).rstrip("%+")
                facts["context_used_pct"] = round(float(ratio) / 100.0, 4)
            except (TypeError, ValueError):
                pass
            facts["tokens"] = int(stats.get("estimated_tokens") or 0)
            facts["max_tokens"] = int(stats.get("max_tokens") or 0)
            facts["messages"] = int(stats.get("total_messages") or 0)
            facts["needs_compact"] = bool(stats.get("needs_compact"))
        facts["streaming"] = bool(getattr(self.repl, "streaming", False))

        tracker = getattr(self.repl, "_cache_tracker", None)
        if tracker is not None:
            # Only report a hit rate once the provider actually reported cache
            # usage; otherwise 0% would read as "caching is broken".
            try:
                if int(getattr(tracker, "cache_reported_calls", 0) or 0) > 0:
                    facts["cache_hit_pct"] = round(
                        float(tracker.cache_hit_rate) * 100.0, 1
                    )
                cost = float(tracker.estimated_cost_yuan)
                if cost > 0:
                    facts["cost_cny"] = round(cost, 4)
                savings = int(tracker.savings_pct)
                if savings > 0:
                    facts["savings_pct"] = savings
            except Exception:  # noqa: BLE001 — status must never fail
                pass
        if self.turn and self.turn_started_at:
            facts["elapsed_seconds"] = round(
                time.time() - self.turn_started_at, 1
            )
        if self.session_started_at:
            facts["session_elapsed_seconds"] = round(
                time.time() - self.session_started_at, 1
            )
        return facts

    # ── approval: the one blocking exchange ────────────────────────

    def request_approval(
        self,
        capability: str,
        tool: str,
        targets: list[str],
        reason: str,
        risk: str = "CRITICAL",
        diff_preview: str | None = None,
        *,
        kind: str = "tool",
    ) -> str:
        """Ask the front-end; block until it answers.

        Fail-closed: no answer (EOF, timeout, garbage) means ``deny``, because
        an unattended process must not acquire permission by being unattended.
        """

        message_id = self._next_id("a")
        pending = _ApprovalRequest(message_id=message_id, settled=threading.Event())
        with self._pending_lock:
            self._pending[message_id] = pending
        advisories: list[dict[str, Any]] = []
        context = getattr(self.repl, "agent_context", None)
        getter = getattr(context, "get_security_advisories", None)
        if callable(getter):
            try:
                advisories = [dict(item) for item in getter()]
            except Exception:  # noqa: BLE001 — advisory context is optional
                advisories = []
        self.frame(
            type="approval.request",
            id=message_id,
            kind=kind,
            capability=capability,
            tool=tool,
            targets=list(targets),
            reason=reason,
            risk=risk,
            diff_preview=diff_preview,
            advisories=advisories,
            options=["once", "turn", "allow", "deny", "cancel"],
        )
        if not pending.settled.wait(timeout=_approval_timeout()):
            _log(f"approval {message_id} timed out; denying")
            return "deny"
        with self._pending_lock:
            self._pending.pop(message_id, None)
        return pending.choice or "deny"

    def resolve_approval(self, message_id: str, choice: str) -> bool:
        with self._pending_lock:
            pending = self._pending.get(message_id)
        if pending is None:
            return False
        pending.choice = str(choice or "deny")
        pending.settled.set()
        return True


_LEVELS = {
    "read-only": 1,
    "read_only": 1,
    "auto-edit": 2,
    "auto_edit": 2,
    "bypass": 3,
    "execute": 3,
}


def _with_level(contract: Any, level: int) -> Any:
    from dataclasses import replace

    from xenon.repl.execution_policy import ExecutionLevel

    try:
        return replace(
            contract,
            level=ExecutionLevel(int(level)),
            proposed_level=ExecutionLevel(int(level)),
        )
    except Exception:  # noqa: BLE001 — a contract shape change must not crash
        return contract


def _targets_for(tool_name: str, params: Mapping[str, Any]) -> list[str]:
    """The execution paths a call touches, as the human would see them.

    Reuses the REPL's own extraction so the protocol cannot disagree with the
    interactive panel about what "this command" means.
    """

    from xenon.nodes.approval_policy import write_targets_for

    try:
        targets = [str(p) for p in (write_targets_for(tool_name, dict(params)) or [])]
    except Exception:  # noqa: BLE001 — an unparsable target is simply not shown
        targets = []
    if not targets:
        command = str(dict(params).get("command") or "").strip()
        if command:
            targets = [command]
    return targets


def _approval_timeout() -> float:
    raw = os.environ.get("XENON_PROTOCOL_APPROVAL_TIMEOUT", "").strip()
    try:
        return float(raw) if raw else 300.0
    except ValueError:
        return 300.0


def _turn_end_fields(run: Any, answer: str) -> dict[str, Any]:
    """Everything ``turn.end`` carries, taken from the kernel's own result."""

    result = getattr(run, "result", None)
    # ``final_answer`` is only set by the callback's ``on_finish``; the kernel
    # path puts the answer on the result itself.  Without this fallback the
    # transcript block lost the answer entirely (in the ledger, absent on screen).
    answer_text = str(answer or "")
    if not answer_text:
        answer_text = str(getattr(result, "answer", "") or "")
    # A stopped turn must still *say* something.  A measured run hit the
    # policy-refusal cap and delivered an empty ``answer``: the screen showed a
    # turn that ended with nothing, and the ledger could not be read from the
    # terminal.  The REPL's own summary already knows how to explain a blocked
    # or failed result (debt, the ledger's blocking reason, the phase), so the
    # protocol path reuses it instead of inventing a second wording.  A
    # cancelled run is the operator's own doing and keeps its silence.
    phase = str(getattr(getattr(result, "phase", ""), "value", "") or "")
    cancelled = bool(getattr(result, "cancelled", False)) or (
        str(getattr(result, "stop_signal", "") or "") == "cancelled"
    )
    if not answer_text and phase in ("blocked", "failed") and not cancelled:
        from xenon.repl.runtime_adapter import _result_summary

        try:
            answer_text = _result_summary(
                result, ledger_path=getattr(run, "ledger_path", None)
            )
        except Exception:  # noqa: BLE001 — a frame must not fail on a partial result
            answer_text = f"任务未交付（phase={phase}）。"
    fields: dict[str, Any] = {
        "phase": str(getattr(getattr(result, "phase", ""), "value", "") or ""),
        "answer": answer_text,
    }
    # A cancelled run must read as "the operator stopped it", not as a failed
    # or evidence-free answer.  The kernel already separates the three in its
    # own ledger; this carries the distinction to the front-end.
    if fields["phase"] == "aborted" or str(getattr(result, "stop_signal", "") or "") == "cancelled":
        fields["cancelled"] = True
    accepted = getattr(result, "accepted", None)
    if accepted is not None:
        fields["accepted"] = bool(accepted)
    # The field is ``evidence_status``; asking for ``evidence`` always returned
    # None, so the front-end showed "证据 未知" for a run that had a real status.
    evidence = getattr(result, "evidence_status", None)
    if evidence is not None:
        fields["evidence"] = {
            "status": str(getattr(evidence, "value", evidence)),
        }
    ledger = getattr(run, "ledger_path", None)
    if ledger is not None:
        fields["ledger"] = str(ledger)
    # The run's own counters, so the front-end can show the collapsed summary
    # the Python TUI printed ("💭 N steps · N tools · evidence").  Derived from
    # the result object, never counted by the front-end.
    metrics = getattr(result, "metrics", None)
    if isinstance(metrics, dict):
        fields["metrics"] = {
            "steps": int(metrics.get("steps", 0) or 0),
            "tool_calls": int(metrics.get("tool_calls", 0) or 0),
        }
    # Disclosure, not a gate: obligations the ledger could not discharge.  The
    # answer is still delivered; the reader is told what was not proven.
    debt: list[dict[str, str]] = []
    for item in list(getattr(result, "debt", None) or [])[:5]:
        obligation = getattr(item, "obligation", None)
        kind = getattr(getattr(obligation, "kind", ""), "value", "") or ""
        reason = str(getattr(item, "reason", "") or getattr(obligation, "id", ""))
        debt.append({"kind": str(kind), "reason": reason})
    if debt:
        fields["debt"] = debt
    fields["planned"] = str(fields.get("phase", "")) == "planned"
    return fields


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Xenon protocol host (NDJSON)")
    parser.add_argument("--model", "-m", action="append", default=[])
    parser.add_argument("--cwd", default="")
    # ``xenon --resume <序号|名称>`` arrives here now that the front-end is the
    # only interactive entry; it is applied as a normal ``/resume`` command.
    parser.add_argument("--resume", default="")
    args = parser.parse_args(argv)

    # The protocol is UTF-8 on both ends.  Windows starts Python with the ANSI
    # code page (GBK here), so a frame containing a Chinese reason string would
    # be encoded as GBK while the reader expects UTF-8 — the front-end then sees
    # a decode error exactly when an approval arrives, which is the worst moment.
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

    model_ids = args.model or _default_models()
    host = ProtocolHost(model_ids, cwd=args.cwd or None)
    host.start()
    if args.resume:
        host.select_session(str(args.resume))

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            host.frame(type="error", error=f"bad frame: {exc}")
            continue
        kind = str(message.get("type") or "")
        if kind == "turn.start":
            objective = str(message.get("objective") or "").strip()
            if not objective:
                host.frame(type="error", error="turn.start without objective")
                continue
            if host.turn_thread is not None and host.turn_thread.is_alive():
                # The REPL is not safe for two concurrent kernel runs, and the
                # front-end never sends one; fail loudly instead of interleaving.
                host.frame(
                    type="error",
                    error="turn.start: a turn is already in progress",
                )
                continue
            if host.command_thread is not None and host.command_thread.is_alive():
                # A command blocked on a question is still using the REPL.  The
                # dashboard used to start a turn on top of it — ``/set_model``'s
                # picker stayed open while the typed "11" ran as a task against
                # the old model.  One exchange at a time, in both directions.
                host.frame(
                    type="error",
                    error=(
                        "turn.start: a command is in progress; "
                        "answer its question or wait for it to finish"
                    ),
                )
                continue
            # The turn MUST run off the stdin loop.  A turn that blocks for an
            # approval would otherwise freeze this loop so the front-end's
            # approval.response could never be read: the panel would sit there
            # until the 300 s timeout and fail closed on every approving turn.
            worker = threading.Thread(
                target=host.run_turn,
                args=(objective, str(message.get("level") or "auto-edit")),
                name="xenon-turn",
                daemon=True,
            )
            host.turn_thread = worker
            worker.start()
        elif kind == "command":
            if host.turn_thread is not None and host.turn_thread.is_alive():
                host.frame(
                    type="error",
                    error="command: a turn is in progress; wait for it to end",
                )
                continue
            if host.command_thread is not None and host.command_thread.is_alive():
                host.frame(type="error", error="command: one command at a time")
                continue
            # A command may ask a question (``prompt.request``); running it on
            # the stdin loop would block the answer forever.
            worker = threading.Thread(
                target=host.run_command,
                args=(
                    str(message.get("name") or ""),
                    str(message.get("args") or ""),
                ),
                name="xenon-command",
                daemon=True,
            )
            host.command_thread = worker
            worker.start()
        elif kind == "turn.cancel":
            accepted = host.cancel_turn()
            host.frame(type="turn.cancel.result", accepted=accepted)
        elif kind == "session.select":
            host.select_session(str(message.get("name") or ""))
        elif kind == "prompt.response":
            if not host.resolve_prompt(
                str(message.get("id") or ""), str(message.get("value") or "")
            ):
                host.frame(
                    type="error", error="prompt.response for an unknown id"
                )
        elif kind == "catalog":
            host.frame(type="catalog", commands=host.command_catalog())
        elif kind == "files.request":
            # Listing is local and cheap; it must not queue behind a turn.
            host.frame(
                type="files.result",
                id=str(message.get("id") or ""),
                paths=host.list_files(str(message.get("query") or "")),
            )
        elif kind == "sessions":
            host.list_sessions()
        elif kind == "status":
            host.status()
        elif kind in ProtocolHost.QUERY_KINDS:
            # ``diff.get`` shells out to git; running it on the stdin loop
            # would delay an approval answer behind a large diff.
            worker = threading.Thread(
                target=host.handle_query,
                args=(message,),
                name="xenon-query",
                daemon=True,
            )
            worker.start()
        elif kind == "approval.response":
            if not host.resolve_approval(
                str(message.get("id") or ""), str(message.get("choice") or "deny")
            ):
                host.frame(
                    type="error", error="approval.response for an unknown id"
                )
        elif kind == "shutdown":
            # The savings panel is the last thing a session shows; emit its
            # facts before leaving so the front-end can render it.
            host.session_report()
            return 0
        else:
            host.frame(type="error", error=f"unknown message type: {kind!r}")

    if host.turn_thread is not None and host.turn_thread.is_alive():
        # Front-end closed stdin mid-turn; give the kernel a moment to finish
        # so its final frame is not lost.  A daemon thread never delays exit.
        host.turn_thread.join(timeout=5)
    return 0


def _plain_text(markup: str) -> str:
    """Drop rich markup, keeping the text a person is supposed to read.

    Command handlers build rich strings.  The interactive REPL renders them;
    the protocol cannot, so it must not ship the brackets either.  Uses rich's
    own renderer (it understands nesting), with literals escaped first so an
    identifier in brackets is not mistaken for a style.  The fallback returns
    the text unchanged: a missing rich must never delete what the command
    said.
    """

    try:
        from io import StringIO

        from rich.console import Console

        buffer = StringIO()
        console = Console(
            file=buffer,
            width=100,
            force_terminal=False,
            color_system=None,
            legacy_windows=False,
            emoji=False,
            highlight=False,
            markup=True,
        )
        from xenon.repl.rich_markup import escape_literal_tags

        console.print(escape_literal_tags(markup), end="\n", soft_wrap=True)
        return buffer.getvalue()
    except Exception:  # noqa: BLE001 — a rendering helper must never fail a command
        return markup


def _default_models() -> list[str]:
    from xenon.repl.model_registry import ModelRegistry

    registry = ModelRegistry()
    registry.load_from_credentials()
    models = [m.alias for m in registry.list_models()]
    return models[:1]


if __name__ == "__main__":
    raise SystemExit(main())
