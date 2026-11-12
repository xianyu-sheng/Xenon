"""A command exists only if it has *both* a handler and a catalog entry.

This guard exists because the same defect shipped three times:

* ``/trust-hooks`` had a handler, was never registered — and two other modules
  told the user to run it, so the advice pointed at a command that could not be
  found;
* ``/permissions`` was registered in two modules and the later import silently
  won;
* ``/lessons`` and ``/auto-route`` had handlers but no catalog entry: typing the
  name worked, while completion and ``/help`` never showed them.

The mechanism is two decorators that do not know about each other, and getting
one of them wrong fails silently.  These tests are the cross-check.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import xenon.repl.commands  # noqa: F401 — importing registers every command group
from xenon.repl.command_registry import _HANDLERS, COMMANDS

COMMAND_GROUPS = Path(__file__).resolve().parents[1] / "xenon" / "repl" / "command_groups"
COMMANDS_MODULE = Path(__file__).resolve().parents[1] / "xenon" / "repl" / "commands.py"


def _decorated_names(path: Path, decorator: str) -> set[str]:
    """Command names passed to ``@<decorator>("/name")`` in one file."""

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for deco in node.decorator_list:
            target = deco.func if isinstance(deco, ast.Call) else deco
            label = getattr(target, "id", None) or getattr(target, "attr", None)
            if label != decorator:
                continue
            if isinstance(deco, ast.Call) and deco.args:
                arg = deco.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    names.add(arg.value)
    return names


def _all_decorated(decorator: str) -> set[str]:
    files = [COMMANDS_MODULE, *sorted(COMMAND_GROUPS.glob("*.py"))]
    found: set[str] = set()
    for path in files:
        found |= _decorated_names(path, decorator)
    return found


# ── the two directions ──────────────────────────────────────────────


def test_every_handler_has_a_catalog_entry() -> None:
    """A handler nobody can discover is a command that does not exist.

    Typing ``/lessons`` worked while completion and ``/help`` never mentioned
    it — the user has to already know the name to find it.
    """

    missing = sorted(set(_HANDLERS) - set(COMMANDS))
    assert not missing, (
        "these handlers are not in COMMANDS, so completion and /help hide them: "
        f"{missing}"
    )


def test_every_catalog_entry_has_a_handler() -> None:
    """A catalog entry without a handler advertises something that cannot run."""

    orphaned = sorted(set(COMMANDS) - set(_HANDLERS))
    assert not orphaned, f"these are advertised but cannot be dispatched: {orphaned}"


def test_the_source_agrees_with_the_runtime() -> None:
    """Catch a decorator that is written but never imported.

    ``_HANDLERS`` only knows what was imported, so a module that is never
    imported looks perfect at runtime.  Scanning the source catches it.
    """

    declared = _all_decorated("command_handler")
    assert declared, "no @command_handler decorators found — the scan is broken"
    missing = sorted(declared - set(_HANDLERS))
    assert not missing, (
        "these handlers are declared in source but never imported, so they "
        f"cannot run: {missing}"
    )


def test_no_command_is_registered_twice_under_different_modules() -> None:
    """The /permissions shadowing bug: two modules, one name, later import wins."""

    registrations: dict[str, set[str]] = {}
    files = [COMMANDS_MODULE, *sorted(COMMAND_GROUPS.glob("*.py"))]
    for path in files:
        for name in _decorated_names(path, "command_handler"):
            registrations.setdefault(name, set()).add(path.name)
    duplicated = {
        name: files for name, files in registrations.items() if len(files) > 1
    }
    assert not duplicated, f"one command declared in several modules: {duplicated}"


def test_a_handler_used_for_several_names_is_registered_for_each() -> None:
    """``/exit`` used to be three names for one function; keep the tables equal.

    Collapsing aliases is fine — what must not happen is a name that dispatches
    but never appears in the list.
    """

    for name in _HANDLERS:
        assert name in COMMANDS, name


@pytest.mark.parametrize("name", sorted(COMMANDS))
def test_every_catalogued_command_has_a_description(name: str) -> None:
    """``/help`` renders this string; an empty one is a blank line."""

    description = str((COMMANDS[name] or {}).get("description") or "").strip()
    assert description, f"{name} has no description"
