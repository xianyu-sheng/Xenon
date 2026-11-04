"""Arrow-key selectors for the commands users repeat (#183, step 2).

``/mode``, ``/model`` and ``/permissions`` used to require remembering the exact
token; Claude Code and Codex let you pick from a list.  This module is the one
picker they share, so there is no second menu to keep in sync.

Two rules keep it safe:

* **No session, no picker.**  Without an interactive prompt_toolkit session (a
  pipe, ``XENON_NO_PT=1``, a test) ``ask_choice`` returns ``None`` and the
  caller keeps printing the list it always printed.  Nothing is lost.
* **Never prefill the buffer.**  ``prompt_toolkit``'s ``default=`` writes into
  the edit buffer — the #395 bug, where every answer became invalid.  The
  current value is shown as a ``*`` marker in the row instead.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True, slots=True)
class Choice:
    """One selectable row: the value to apply, the label to show."""

    value: str
    label: str
    detail: str = ""
    current: bool = False

    def row(self) -> str:
        marker = "* " if self.current else "  "
        return f"{marker}{self.label}"

    def meta(self) -> str:
        parts = []
        if self.current:
            parts.append("当前")
        if self.detail:
            parts.append(self.detail)
        return " · ".join(parts)


def resolve_answer(choices: Sequence[Choice], answer: str) -> str | None:
    """Map a submitted line to a value.

    Accepts the label, the row text (with the current-value marker) or a 1-based
    number — the number keeps the muscle memory of the old ``输入编号`` prompt.
    Anything else is ``None`` (caller reports and re-prompts or falls back).
    """

    text = (answer or "").strip()
    if not text:
        return None
    if text.isdigit():
        index = int(text) - 1
        if 0 <= index < len(choices):
            return choices[index].value
        return None
    for choice in choices:
        if text in {choice.label, choice.row().strip(), choice.value}:
            return choice.value
    # a case-insensitive label match is still unambiguous enough
    lowered = text.casefold()
    for choice in choices:
        if choice.label.casefold() == lowered or choice.value.casefold() == lowered:
            return choice.value
    return None


def _key_bindings():
    """Enter accepts the highlighted row instead of submitting the typed text."""

    from prompt_toolkit.key_binding import KeyBindings

    bindings = KeyBindings()

    @bindings.add("enter", eager=True)
    def _(event):
        accept_choice(event.current_buffer)

    return bindings


def accept_choice(buffer: Any) -> None:
    """Enter: take the highlighted row when the buffer is still empty."""

    state = getattr(buffer, "complete_state", None)
    if state is not None and getattr(state, "completions", None):
        index = getattr(state, "complete_index", None)
        chosen = (
            state.completions[index]
            if index is not None and 0 <= index < len(state.completions)
            else state.completions[0]
        )
        if not buffer.document.text_before_cursor.strip():
            buffer.text = chosen.text
            buffer.cursor_position = len(chosen.text)
    buffer.validate_and_handle()


def _open_menu(session: Any):
    """Open the completion menu before the user types anything."""

    def run() -> None:
        buffer = getattr(session, "default_buffer", None)
        start = getattr(buffer, "start_completion", None)
        if callable(start):
            start()

    return run


def _completer(choices: Sequence[Choice]):
    from prompt_toolkit.completion import Completer, Completion

    class _ChoiceCompleter(Completer):
        def get_completions(self, document, complete_event):
            typed = document.text_before_cursor.strip().casefold()
            for choice in choices:
                haystack = f"{choice.label} {choice.detail}".casefold()
                if typed and typed not in haystack:
                    continue
                yield Completion(
                    choice.label,
                    start_position=-len(document.text_before_cursor),
                    display=choice.row(),
                    display_meta=choice.meta(),
                )

    return _ChoiceCompleter()


def ask_choice(
    repl: Any,
    *,
    title: str,
    choices: Sequence[Choice],
    attempts: int = 2,
) -> str | None:
    """Show a selector and return the chosen value, or ``None`` to fall back."""

    if not choices:
        return None
    session = getattr(repl, "_pt_session", None)
    if session is None:
        return None
    try:
        interactive = bool(sys.stdin) and sys.stdin.isatty()
    except Exception:  # noqa: BLE001 — exotic streams are not interactive
        interactive = False
    if not interactive:
        return None

    # The list is part of the question, not only the completion menu: a menu can
    # be dismissed by a stray ESC (winpty splits escape sequences), and a picker
    # whose options are invisible is worse than no picker.  The menu is still
    # there for search; the numbered list is the guaranteed surface.
    lines = [f"{title}:"]
    for index, choice in enumerate(choices, start=1):
        current = " ←" if choice.current else ""
        detail = f"  {choice.detail}" if choice.detail else ""
        lines.append(f"  {index}) {choice.label}{detail}{current}")
    default_row = next(
        (item for item in choices if item.current), choices[0]
    )
    lines.append(
        f"输入编号或名称（Enter = {default_row.label}）: "
    )
    question = chr(10).join(lines)
    for _ in range(max(1, attempts)):
        try:
            answer = session.prompt(
                question,
                completer=_completer(choices),
                key_bindings=_key_bindings(),
                complete_while_typing=True,
                # The list is in the question, but an open menu is still the
                # shortest path for someone who wants to arrow down and type
                # nothing.
                pre_run=_open_menu(session),
            )
        except (KeyboardInterrupt, EOFError):
            return None
        if not (answer or "").strip():
            return default_row.value
        value = resolve_answer(choices, answer)
        if value is not None:
            return value
    return None


# ── the three pickers ────────────────────────────────────────────


def mode_choices(registry: Any) -> tuple[Choice, ...]:
    """One row per *distinct* loop profile.

    The catalog still resolves 7 names, but ``react`` declares exactly the same
    phases as ``direct`` since the engines were retired — offering both as
    "paradigms" would claim a difference that no longer exists.  Aliases are
    named in the detail column instead, so they stay discoverable.
    """

    from xenon.modes import MODE_CATALOG, aliases_of

    current = registry.get_current_mode()
    # Walk the *registry's* catalog so an injected set (tests, plugins) is
    # respected, and drop aliases: they resolve to an identical loop profile.
    known = getattr(registry, "modes", {}) or {}
    rows: list[Choice] = []
    seen: set[str] = set()
    for name, spec in known.items():
        if name in seen:
            continue
        canonical = getattr(spec, "alias_of", "") or name
        profile = getattr(spec, "loop", None)
        if profile is not None and canonical in {row.value for row in rows}:
            continue  # same profile as a row we already offer
        if canonical != name:
            continue  # the alias itself never gets its own row
        detail = getattr(spec, "description", "") or ""
        aliases = aliases_of(name) if name in MODE_CATALOG else ()
        if aliases:
            detail = f"{detail}（别名：{'/'.join(aliases)}）"
        seen.add(name)
        rows.append(
            Choice(
                value=name,
                label=name,
                detail=detail,
                current=name == current.name,
            )
        )
    if current.name not in {row.value for row in rows}:
        # The current mode is an alias (or an unknown injected by a test):
        # still show it, marked, so "what am I on" is always answerable.
        spec = registry.modes.get(current.name)
        rows.append(
            Choice(
                value=current.name,
                label=current.name,
                detail=getattr(spec, "description", "") or "",
                current=True,
            )
        )
    return tuple(rows)


def model_choices(registry: Any) -> tuple[Choice, ...]:
    active = set(registry.role_priority.get("planner", []))
    return tuple(
        Choice(
            value=model.alias,
            label=model.alias,
            detail=model.model_id,
            current=model.alias in active,
        )
        for model in registry.list_models()
    )


def permission_choices(current: str = "") -> tuple[Choice, ...]:
    """The four permission modes the panel understands."""

    rows = (
        ("default", "default", "写盘/命令需要确认"),
        ("accept_edits", "accept_edits", "工作区内写盘直接执行"),
        ("plan", "plan", "只读：只产出方案"),
        ("bypass", "bypass", "全部放行（危险）"),
    )
    key = (current or "").strip()
    return tuple(
        Choice(value=value, label=label, detail=detail, current=value == key)
        for value, label, detail in rows
    )


def looks_like_choice_request(args: str) -> bool:
    """No arguments (or only whitespace) means "pick one for me"."""

    return not re.sub(r"\s+", "", args or "")
