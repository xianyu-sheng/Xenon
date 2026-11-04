"""Unified selector (#183, step 2): one picker, and a safe fallback.

Acceptance this file locks:

1. no interactive session → ``None``, so the caller prints the list it always
   printed (nothing regresses without a terminal);
2. the answer maps from label, row text, value or 1-based number;
3. garbage does not silently pick something;
4. the buffer is never prefilled (the #395 lesson);
5. ``/permissions`` has exactly one owner, and it covers mode + grants.
"""

from __future__ import annotations

import types

import pytest

from xenon.repl.command_groups.model import _cmd_mode
from xenon.repl.command_groups.permissions import MODE_MAP, _cmd_permissions
from xenon.repl.command_registry import _HANDLERS
from xenon.repl.selector import (
    Choice,
    ask_choice,
    looks_like_choice_request,
    mode_choices,
    model_choices,
    permission_choices,
    resolve_answer,
)


class _Registry:
    """A minimal registry: modes, models, role priority."""

    def __init__(self, *, modes=None, models=None, current="direct") -> None:
        self.modes = modes or {
            "direct": types.SimpleNamespace(description="直接回答"),
            "plan-execute": types.SimpleNamespace(description="先计划再执行"),
        }
        self._models = models or []
        self.role_priority = {"planner": ["first"]}
        self._current = current
        self.set_mode_calls: list[str] = []

    def get_current_mode(self):
        mode = self.modes.get(self._current) or types.SimpleNamespace(
            description=""
        )
        return types.SimpleNamespace(name=self._current, description=mode.description)

    def set_mode(self, name: str):
        if name not in self.modes:
            raise ValueError(f"未知范式: {name}")
        self._current = name
        self.set_mode_calls.append(name)
        return self.get_current_mode()

    def list_models(self):
        return list(self._models)


def _model(alias: str, model_id: str):
    return types.SimpleNamespace(alias=alias, model_id=model_id)


def _repl(*, session=None, rules=None, book=None) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        _pt_session=session,
        _permission_rules=rules,
        _grant_book=book,
        _permission_gate=None,
        project_ctx=types.SimpleNamespace(root="."),
    )


# ── 1. fallback is safe ─────────────────────────────────────────


def test_no_session_means_no_picker() -> None:
    assert ask_choice(_repl(), title="t", choices=permission_choices()) is None


def test_a_non_tty_session_means_no_picker(monkeypatch) -> None:
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    session = types.SimpleNamespace(prompt=lambda *a, **k: "plan")
    assert ask_choice(_repl(session=session), title="t", choices=permission_choices()) is None


def test_no_choices_means_no_picker() -> None:
    assert ask_choice(_repl(), title="t", choices=()) is None


# ── 2. answers map to values ────────────────────────────────────


def test_a_label_maps_to_its_value() -> None:
    assert resolve_answer(permission_choices(), "bypass") == "bypass"


def test_a_number_maps_to_the_same_value() -> None:
    choices = permission_choices()
    assert resolve_answer(choices, "3") == choices[2].value


def test_a_row_text_with_the_current_marker_maps() -> None:
    choices = permission_choices("plan")
    row = next(item for item in choices if item.current).row().strip()
    assert resolve_answer(choices, row) == "plan"


def test_a_value_that_differs_from_its_label_maps() -> None:
    choices = (Choice(value="v1", label="友好名字"),)
    assert resolve_answer(choices, "友好名字") == "v1"
    assert resolve_answer(choices, "v1") == "v1"


# ── 3. garbage is not a choice ──────────────────────────────────


@pytest.mark.parametrize("answer", ["", "   ", "nope", "0", "99"])
def test_garbage_does_not_pick_anything(answer: str) -> None:
    assert resolve_answer(permission_choices(), answer) is None


# ── 4. choices are built from live state ────────────────────────


def test_mode_choices_mark_the_current_one() -> None:
    choices = mode_choices(_Registry(current="plan-execute"))
    assert [item.value for item in choices] == ["direct", "plan-execute"]
    assert [item.current for item in choices] == [False, True]
    assert all(item.detail for item in choices)


def test_model_choices_mark_the_active_one() -> None:
    registry = _Registry(
        models=[_model("first", "provider/a"), _model("second", "provider/b")]
    )
    choices = model_choices(registry)
    assert [item.current for item in choices] == [True, False]
    assert choices[0].detail == "provider/a"


def test_permission_choices_mark_the_current_mode() -> None:
    choices = permission_choices("bypass")
    assert {item.value for item in choices} == set(MODE_MAP)
    assert [item.value for item in choices if item.current] == ["bypass"]


def test_looks_like_a_choice_request() -> None:
    assert looks_like_choice_request("")
    assert looks_like_choice_request("   ")
    assert not looks_like_choice_request("plan")


# ── 5. the commands use the picker and fall back cleanly ────────


def test_mode_with_a_pick_switches_the_mode(monkeypatch) -> None:
    import xenon.repl.selector as selector

    monkeypatch.setattr(selector, "ask_choice", lambda *a, **k: "plan-execute")
    registry = _Registry()
    repl = _repl()

    out = _cmd_mode(args="", registry=registry, session_state={"_repl": repl})

    assert "plan-execute" in out
    assert registry.set_mode_calls == ["plan-execute"]


def test_mode_without_a_pick_still_lists_every_mode(monkeypatch) -> None:
    import xenon.repl.selector as selector

    monkeypatch.setattr(selector, "ask_choice", lambda *a, **k: None)
    registry = _Registry()

    out = _cmd_mode(args="", registry=registry, session_state={"_repl": _repl()})

    assert "当前范式" in out
    assert "plan-execute" in out
    assert registry.set_mode_calls == []


def test_permissions_answers_the_current_mode_even_without_grants() -> None:
    out = _cmd_permissions(args="", session_state={"_repl": _repl()})
    assert "权限模式" in out
    assert "没有记住的授权" in out


def test_permissions_switches_mode_by_name() -> None:
    gate = types.SimpleNamespace(
        mode=types.SimpleNamespace(value="default"),
        set_mode=lambda mode: setattr(gate, "mode", mode),
        reset_session=lambda: None,
    )
    repl = _repl()
    repl._permission_gate = gate

    out = _cmd_permissions(args="bypass", session_state={"_repl": repl})

    assert "bypass" in out
    # PermissionMode.BYPASS, whose .value is the lowercase mode name.
    assert gate.mode.name == "BYPASS"


def test_permissions_still_manages_grants() -> None:
    class _Rules:
        def remove(self, selector):
            return selector == "shell(git log)"

        def rows(self):
            return []

        rules: list = []

    out = _cmd_permissions(
        args="remove shell(git log)", session_state={"_repl": _repl(rules=_Rules())}
    )
    assert "已撤销" in out


# ── 6. one owner for /permissions (the shadowing bug) ──────────


def test_permissions_has_exactly_one_handler_and_it_is_the_grants_module() -> None:
    """``workspace.py`` used to register the same command later and win, which
    made #373's grants view unreachable for users."""
    handler = _HANDLERS["/permissions"]
    assert handler.__module__ == "xenon.repl.command_groups.permissions"


# ── the list is visible even when the menu is not ───────────────


def test_an_empty_answer_picks_the_current_row(monkeypatch) -> None:
    """Enter on a selector means "keep what I have", never "first item"."""
    import sys
    import types as _types

    import xenon.repl.selector as selector

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    seen: dict = {}

    class _Session:
        def prompt(self, question, **kwargs):
            seen["question"] = question
            return ""

    repl = _types.SimpleNamespace(_pt_session=_Session())
    value = selector.ask_choice(
        repl, title="选择权限模式", choices=permission_choices("plan")
    )

    assert value == "plan"
    # the options are in the prompt text, so a dismissed menu still shows them
    assert "1) default" in seen["question"]
    assert "←" in seen["question"]


def test_a_number_answer_picks_by_position(monkeypatch) -> None:
    import sys
    import types as _types

    import xenon.repl.selector as selector

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)

    class _Session:
        def prompt(self, question, **kwargs):
            return "3"

    repl = _types.SimpleNamespace(_pt_session=_Session())
    value = selector.ask_choice(
        repl, title="选择权限模式", choices=permission_choices("default")
    )

    assert value == "plan"


# ── the catalog tells the truth about "single loop" ─────────────


def test_react_is_an_alias_not_a_second_paradigm() -> None:
    """The seven engines are gone; ``react`` now declares *exactly* what
    ``direct`` declares, so offering it as a separate paradigm would claim a
    difference that no longer exists."""
    from xenon.modes import MODE_CATALOG, aliases_of, distinct_modes

    assert MODE_CATALOG["react"].alias_of == "direct"
    assert MODE_CATALOG["react"].loop == MODE_CATALOG["direct"].loop
    assert "react" in aliases_of("direct")
    assert {spec.name for spec in distinct_modes()} == {
        "direct",
        "plan-execute",
        "reflection",
        "plan-react",
        "plan-reflection",
        "react-reflection",
    }


def test_every_distinct_mode_has_a_distinct_profile() -> None:
    from xenon.modes import distinct_modes

    profiles = [repr(spec.loop) for spec in distinct_modes()]
    assert len(profiles) == len(set(profiles))


def test_the_picker_offers_one_row_per_profile() -> None:
    from xenon.repl.model_registry import ModelRegistry

    choices = mode_choices(ModelRegistry())
    values = [item.value for item in choices]

    assert "react" not in values, "an alias must not get its own row"
    assert len(values) == len(set(values))
    direct = next(item for item in choices if item.value == "direct")
    assert "react" in direct.detail, "the alias stays discoverable"


def test_the_current_mode_is_always_shown() -> None:
    """Even when the current mode is an alias, "what am I on" is answerable."""
    registry = _Registry(
        modes={
            "direct": types.SimpleNamespace(description="直接", alias_of="", loop=None),
            "react": types.SimpleNamespace(
                description="别名", alias_of="direct", loop=None
            ),
        },
        current="react",
    )

    choices = mode_choices(registry)

    assert [item.value for item in choices if item.current] == ["react"]


# ── selector internals (menu, pre_run, retry) ───────────────────


def test_the_choice_menu_lists_and_filters(monkeypatch) -> None:
    import sys
    import types as _types

    import xenon.repl.selector as selector

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    seen: dict = {}

    class _Session:
        def prompt(self, question, **kwargs):
            seen["kwargs"] = kwargs
            from prompt_toolkit.document import Document

            items = list(kwargs["completer"].get_completions(Document(""), None))
            filtered = list(
                kwargs["completer"].get_completions(Document("byp"), None)
            )
            seen["all"] = [(item.text, item.display_meta_text) for item in items]
            seen["filtered"] = [item.text for item in filtered]
            return "bypass"

    repl = _types.SimpleNamespace(_pt_session=_Session())
    value = selector.ask_choice(
        repl, title="t", choices=permission_choices("default")
    )

    assert value == "bypass"
    assert len(seen["all"]) == 4
    assert all(meta for _, meta in seen["all"])
    assert seen["filtered"] == ["bypass"]
    assert callable(seen["kwargs"]["pre_run"])


def test_pre_run_opens_the_menu() -> None:
    import types as _types

    from xenon.repl.selector import _open_menu

    calls: list[str] = []
    buffer = _types.SimpleNamespace(start_completion=lambda: calls.append("start"))
    session = _types.SimpleNamespace(default_buffer=buffer)

    _open_menu(session)()

    assert calls == ["start"]


def test_pre_run_tolerates_a_session_without_a_buffer() -> None:
    import types as _types

    from xenon.repl.selector import _open_menu

    _open_menu(_types.SimpleNamespace())()  # must not raise


class _SelBuffer:
    def __init__(self, text: str = "", completions=None, index=None) -> None:
        self.document = type("D", (), {"text_before_cursor": text})()
        self.complete_state = (
            None
            if completions is None
            else type(
                "S",
                (),
                {
                    "completions": [type("C", (), {"text": c})() for c in completions],
                    "complete_index": index,
                },
            )()
        )
        self.text = text
        self.cursor_position = len(text)
        self.submitted = False

    def validate_and_handle(self) -> None:
        self.submitted = True


def test_enter_takes_the_highlighted_row_when_empty() -> None:
    from xenon.repl.selector import accept_choice

    buffer = _SelBuffer("", ["direct", "react"], 1)
    accept_choice(buffer)

    assert buffer.text == "react"
    assert buffer.submitted is True


def test_enter_keeps_typed_text() -> None:
    from xenon.repl.selector import accept_choice

    buffer = _SelBuffer("byp", ["bypass"], 0)
    accept_choice(buffer)

    assert buffer.text == "byp"
    assert buffer.submitted is True


def test_enter_without_a_menu_still_submits() -> None:
    from xenon.repl.selector import accept_choice

    buffer = _SelBuffer("2")
    accept_choice(buffer)

    assert buffer.text == "2"
    assert buffer.submitted is True


def test_a_bad_answer_gives_up_after_the_attempts(monkeypatch) -> None:
    import sys
    import types as _types

    import xenon.repl.selector as selector

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    answers = ["nope", "still-nope"]

    class _Session:
        def prompt(self, question, **kwargs):
            return answers.pop(0)

    repl = _types.SimpleNamespace(_pt_session=_Session())
    assert selector.ask_choice(
        repl, title="t", choices=permission_choices(), attempts=2
    ) is None


def test_ctrl_c_means_cancel(monkeypatch) -> None:
    import sys
    import types as _types

    import xenon.repl.selector as selector

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)

    class _Session:
        def prompt(self, question, **kwargs):
            raise KeyboardInterrupt

    repl = _types.SimpleNamespace(_pt_session=_Session())
    assert selector.ask_choice(
        repl, title="t", choices=permission_choices()
    ) is None


def test_model_command_uses_the_picker(monkeypatch) -> None:
    import xenon.repl.selector as selector
    from xenon.repl.command_groups.model import _cmd_model

    monkeypatch.setattr(selector, "ask_choice", lambda *a, **k: "second")
    registry = _Registry(
        models=[_model("first", "provider/a"), _model("second", "provider/b")]
    )
    seen: list[str] = []
    repl = _repl()
    repl._preferred_model_ids = []
    repl.status_bar = types.SimpleNamespace(set_last_model=lambda m: None)
    repl.auto_router = None
    repl._failed_models = set()
    repl._activate = lambda model_id: seen.append(model_id)

    out = _cmd_model(
        session_state={"_repl": repl}, registry=registry, console=types.SimpleNamespace(print=lambda *a, **k: None)
    )

    assert "second" in out
    assert registry.role_priority["planner"] == ["second"]


def test_an_uppercase_label_still_matches() -> None:
    assert resolve_answer(permission_choices(), "BYPASS") == "bypass"


def test_a_raising_isatty_means_no_picker(monkeypatch) -> None:
    import sys
    import types as _types

    import xenon.repl.selector as selector

    def boom():
        raise OSError("no terminal here")

    monkeypatch.setattr(sys.stdin, "isatty", boom, raising=False)
    session = _types.SimpleNamespace(prompt=lambda *a, **k: "plan")
    assert (
        selector.ask_choice(
            _repl(session=session), title="t", choices=permission_choices()
        )
        is None
    )


def test_an_alias_of_a_listed_mode_gets_no_row() -> None:
    registry = _Registry(
        modes={
            "direct": types.SimpleNamespace(description="直接", alias_of="", loop="L1"),
            "react": types.SimpleNamespace(
                description="别名", alias_of="direct", loop="L1"
            ),
        }
    )
    assert [item.value for item in mode_choices(registry)] == ["direct"]


def test_switching_to_an_unknown_mode_name_is_not_a_switch() -> None:
    from xenon.repl.command_groups.permissions import _switch_mode

    assert _switch_mode(_repl(), "nonsense") is None


def test_switching_without_a_gate_reports_it() -> None:
    from xenon.repl.command_groups.permissions import _switch_mode

    assert _switch_mode(_repl(), "plan") == "权限系统未初始化"


def test_a_pick_that_cannot_be_applied_reports_a_warning(monkeypatch) -> None:
    import xenon.repl.selector as selector

    monkeypatch.setattr(selector, "ask_choice", lambda *a, **k: "not-a-mode")

    out = _cmd_permissions(args="", session_state={"_repl": _repl()})

    assert "未能切换" in out
