"""Numbered selectors for the commands users repeat (#183, step 2).

``/mode``, ``/model`` and ``/permissions`` used to require remembering the exact
token; Claude Code and Codex let you pick from a list.  This module is the one
picker they share, so there is no second menu to keep in sync.

Two rules keep it safe:

* **No TTY, no picker.**  In a pipe or a test ``ask_choice`` returns ``None``
  and the caller keeps printing the list it always printed.  Nothing is lost.
* **The list is part of the question.**  A menu that can be dismissed by a
  stray ESC is worse than no menu, so the numbered rows are always rendered
  next to the prompt.
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


def ask_choice(
    repl: Any,
    *,
    title: str,
    choices: Sequence[Choice],
    attempts: int = 2,
) -> str | None:
    """Show the numbered list and return the chosen value, or ``None``.

    ``repl`` is accepted for call-site compatibility; the picker no longer
    needs the object (the old prompt_toolkit session lived on it).  A protocol
    host replaces this function wholesale for the duration of a command.
    """

    if not choices:
        return None
    try:
        # A pseudo-input used by test runners reports isatty() and then fails
        # on read; requiring a real descriptor keeps the picker from blocking
        # a command that only wanted to print its list.
        interactive = (
            bool(sys.stdin) and sys.stdin.isatty() and sys.stdin.fileno() >= 0
        )
    except Exception:  # noqa: BLE001 — exotic streams are not interactive
        interactive = False
    if not interactive:
        return None

    lines = [f"{title}:"]
    for index, choice in enumerate(choices, start=1):
        current = " ←" if choice.current else ""
        detail = f"  {choice.detail}" if choice.detail else ""
        lines.append(f"  {index}) {choice.label}{detail}{current}")
    default_row = next((item for item in choices if item.current), choices[0])
    question = chr(10).join(lines) + (
        chr(10) + f"输入编号或名称（Enter = {default_row.label}）"
    )

    from rich.prompt import Prompt

    for _ in range(max(1, attempts)):
        try:
            answer = Prompt.ask(question, default="", show_default=False)
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
    offered: set[str] = set()
    for name, spec in known.items():
        if getattr(spec, "alias_of", ""):
            continue  # an alias never gets its own row
        profile = getattr(spec, "loop", None)
        if profile is not None and any(
            repr(getattr(known.get(row), "loop", None)) == repr(profile)
            for row in offered
        ):
            continue  # two names, one loop profile: do not pretend otherwise
        detail = getattr(spec, "description", "") or ""
        aliases = aliases_of(name) if name in MODE_CATALOG else ()
        if aliases:
            detail = f"{detail}（别名：{'/'.join(aliases)}）"
        offered.add(name)
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
        ("bypass", "bypass", "全部放行（危险；破坏性命令与 push 仍需确认，无通道时拒绝）"),
    )
    key = (current or "").strip()
    return tuple(
        Choice(value=value, label=label, detail=detail, current=value == key)
        for value, label, detail in rows
    )


def looks_like_choice_request(args: str) -> bool:
    """No arguments (or only whitespace) means "pick one for me"."""

    return not re.sub(r"\s+", "", args or "")
