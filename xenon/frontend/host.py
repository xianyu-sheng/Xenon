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


def _emit(stream: TextIO, payload: Mapping[str, Any]) -> None:
    """Write one frame.  A single lock keeps concurrent sinks from interleaving."""

    with _EMIT_LOCK:
        stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        stream.flush()


_EMIT_LOCK = threading.Lock()


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


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
        self.turn_started_at = 0.0
        #: Whether the kernel has no credentials/models at all (local facts only).
        self.setup_needed = False

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
        # The protocol owns the real stdout.  Anything the REPL prints through
        # a stdout-backed console (errors, command internals) would corrupt the
        # NDJSON stream, so process stdout becomes a diagnostic sink; frames
        # keep writing to ``self.out``, captured at construction.
        if self.out is sys.stdout:
            sys.stdout = sys.stderr
        self.repl._protocol_callback = ProtocolCallback(self)
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
        self.setup_needed = self._setup_needed()
        self.frame(
            type="ready",
            models=[m.alias for m in registry.list_models()],
            cwd=str(self.cwd),
            setup_needed=self.setup_needed,
        )

    def _setup_needed(self) -> bool:
        """Local-only first-run check (no provider probing, no network).

        The startup hint used to live in the interactive Python loop; with the
        front-end as the only entry it has to come from here, or a user with no
        credentials sees an empty model list and a turn that cannot run.
        """

        check = getattr(self.repl, "_check_first_run", None)
        if not callable(check):
            return False
        try:
            summary = check()
        except Exception:  # noqa: BLE001 — a hint must never block startup
            _log("first-run check failed")
            return False
        return bool((summary or {}).get("needs_setup"))

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
            self.repl._last_approval_scope = {
                "once": "",
                "turn": "",
                "allow": "session",
            }[choice]
            return True, ""
        if choice == "cancel":
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
        """Ask the running kernel loop to stop; False when nothing is running."""

        runtime = getattr(self.repl, "_active_runtime", None)
        cancel = getattr(runtime, "request_cancel", None)
        if not callable(cancel):
            return False
        try:
            cancel()
        except Exception:  # noqa: BLE001 — cancel is best effort
            return False
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
        try:
            # The direct path expects the turn's request message to be in the
            # conversation already (the REPL persists it before dispatching).
            # Without it the provider sees an empty conversation and answers 400.
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
                answer=getattr(outcome, "answer", "") or "",
                model_id=getattr(outcome, "model_id", "") or "",
                error=getattr(outcome, "error", "") or failure,
                delegated=delegated,
            )
        answer = getattr(outcome, "answer", "") or ""
        error = getattr(outcome, "error", "") or failure
        if delegated:
            error = "模型尝试执行工具；请把这一轮改写成工具任务"
        self.frame(
            type="turn.end",
            phase="finalized" if answer else "failed",
            answer=answer,
            accepted=bool(answer),
            error=error,
            evidence={"status": "unverified"},
            ledger=str(path) if path is not None else None,
        )
        self.turn = ""

    def run_turn(self, objective: str, level: str = "auto-edit") -> None:
        """One user turn.  Blocks until the kernel finishes it.

        The level is *rebound from the objective* exactly as the interactive
        REPL does.  Skipping that step made the kernel veto every shell call
        with "shell requires a grant" before it could even ask: the policy only
        offers an approval channel once the turn's level permits execution, and
        a turn whose objective says "run …" is an execution turn.  Measured on a
        real task through the TS front-end — no prompt ever appeared.
        """

        from xenon.repl.runtime_adapter import run_kernel_turn
        from xenon.repl.turn_contract import build_turn_contract

        self.turn = self._next_id("t")
        self.turn_started_at = time.time()
        self.final_answer = ""
        self.frame(type="turn.start", objective=objective)

        contract = build_turn_contract(objective)
        # An explicit --level from the front-end wins; otherwise the objective
        # decides, which is what the interactive path has always done.
        requested = str(level or "").lower()
        if requested and requested not in {"auto", "auto-edit", "default"}:
            level_value = _LEVELS.get(requested, int(contract.level))
        else:
            level_value = int(contract.level)
        contract = _with_level(contract, level_value)
        # The turn's level is the level the session is running at.  Without
        # writing it back, `/permissions` reports "未初始化" and the status bar
        # shows nothing, because both read it from the agent context.
        setter = getattr(self.repl.agent_context, "set", None)
        if callable(setter):
            setter("_execution_level", int(level_value))
            setter("_execution_reason", "protocol turn")

        callback = self.repl._protocol_callback
        if not contract.to_execution_policy().requires_tools:
            # Pure conversation: the kernel loop would plan, propose a tool and
            # get vetoed by the read-only boundary ("policy veto" on a
            # greeting).  Mirror the REPL's direct path, but emit frames and
            # record the turn in the ledger.
            self._run_chat(objective, contract)
            return
        try:
            run = run_kernel_turn(
                self.repl,
                objective=objective,
                user_input=objective,
                model_ids=self._current_models(),
                execution_policy=contract.to_execution_policy(),
                callback=callback,
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
            # a successful, evidence-free run; say it failed.
            self.frame(
                type="turn.end",
                phase="failed",
                answer="",
                error=self.final_answer or "kernel turn failed",
                error_kind="internal",
                retryable=False,
            )
            self.turn = ""
            return

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

        command_id = self._next_id("c")
        session_state = {"_repl": self.repl, "agent_context": self.repl.agent_context}
        captured = io.StringIO()

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
        printed = _plain_text(captured.getvalue()).strip()
        returned = _plain_text("" if output is None else str(output)).strip()
        text = "\n".join(part for part in (printed, returned) if part)
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
                    "paradigm": str(row.get("paradigm") or ""),
                }
                for row in rows
            ],
        )

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
            mode=str(getattr(self.repl.registry, "current_mode", "") or ""),
            level=name,
            cwd=str(self.cwd),
            **self._status_facts(),
        )

    def _status_facts(self) -> dict[str, Any]:
        """Numbers the bar shows, all read from existing counters.

        Deliberately no cache-hit or cost percentage yet: the host cannot read
        those reliably from the session, and a made-up number in a status bar
        is worse than a missing one.
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
        stats = getattr(context, "stats", None)
        if callable(stats):
            try:
                ratio = stats().get("usage_ratio", "")
                facts["context_used_pct"] = round(
                    float(str(ratio).rstrip("%+")) / 100.0, 4
                )
            except (TypeError, ValueError):
                pass
        if self.turn and self.turn_started_at:
            facts["elapsed_seconds"] = round(
                time.time() - self.turn_started_at, 1
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
    fields: dict[str, Any] = {
        "phase": str(getattr(getattr(result, "phase", ""), "value", "") or ""),
        "answer": answer_text,
    }
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
        elif kind == "approval.response":
            if not host.resolve_approval(
                str(message.get("id") or ""), str(message.get("choice") or "deny")
            ):
                host.frame(
                    type="error", error="approval.response for an unknown id"
                )
        elif kind == "shutdown":
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
    own renderer when available (it understands nesting and escaped brackets),
    and falls back to a conservative strip so a missing rich never breaks a
    command result.
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
        console.print(markup, end="\n", soft_wrap=True)
        return buffer.getvalue()
    except Exception:  # noqa: BLE001 — a rendering helper must never fail a command
        import re

        return re.sub(r"\[/?[a-zA-Z][^\]]*\]", "", markup)


def _default_models() -> list[str]:
    from xenon.repl.model_registry import ModelRegistry

    registry = ModelRegistry()
    registry.load_from_credentials()
    models = [m.alias for m in registry.list_models()]
    return models[:1]


if __name__ == "__main__":
    raise SystemExit(main())
