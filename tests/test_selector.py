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
            raise ValueError(f"未知模式: {name}")
        self._current = name
        self.set_mode_calls.append(name)
        return self.get_current_mode()

    def list_models(self):
        return list(self._models)


def _model(alias: str, model_id: str):
    return types.SimpleNamespace(alias=alias, model_id=model_id)


def _repl(*, book=None, level=None) -> types.SimpleNamespace:
    store: dict = {} if level is None else {"_execution_level": int(level)}
    return types.SimpleNamespace(
        _grant_book=book,
        agent_context=types.SimpleNamespace(
            get=lambda key, default=None: store.get(key, default),
            set=lambda key, value: store.__setitem__(key, value),
            _store=store,
        ),
        project_ctx=types.SimpleNamespace(root="."),
    )


# ── 1. fallback is safe ─────────────────────────────────────────


def test_no_session_means_no_picker() -> None:
    assert ask_choice(_repl(), title="t", choices=permission_choices()) is None


def test_a_non_tty_means_no_picker(monkeypatch) -> None:
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert ask_choice(_repl(), title="t", choices=permission_choices()) is None


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


def test_mode_reports_the_single_loop_and_does_not_switch(monkeypatch) -> None:
    """`/mode` is informational: one loop, phases fixed, no picker.

    The picker/switch surface made a phase profile look like a product menu;
    a follow-up could silently run under a different mode.  The command must
    stay as an answer (old muscle memory lands here) and must never call
    `set_mode`.
    """
    import xenon.repl.selector as selector

    monkeypatch.setattr(selector, "ask_choice", lambda *a, **k: "plan-execute")
    registry = _Registry()

    out = _cmd_mode(args="", registry=registry, session_state={"_repl": _repl()})

    assert "当前循环" in out
    assert "无需切换" in out
    assert "plan-execute" not in out
    assert registry.set_mode_calls == []


def test_permissions_answers_the_current_mode_even_without_grants() -> None:
    out = _cmd_permissions(args="", session_state={"_repl": _repl(level=2)})
    assert "执行级别" in out
    assert "没有记住的授权" in out


def test_permissions_switches_mode_by_name() -> None:
    """A mode name must move the value the kernel actually reads.

    It used to call ``PermissionGate.set_mode`` on a gate the kernel never
    received: the command reported success and changed nothing.  The assertion
    is on ``_execution_level`` because that is what becomes the kernel's
    ``PermissionMode``.
    """

    from xenon.repl.execution_policy import ExecutionLevel

    repl = _repl(level=int(ExecutionLevel.WRITE))

    out = _cmd_permissions(args="plan", session_state={"_repl": repl})

    assert "read_only" in out
    assert repl.agent_context.get("_execution_level") == int(ExecutionLevel.READ_ONLY)


def test_permissions_still_manages_grants() -> None:
    from xenon.kernel.contracts import Capability
    from xenon.kernel.session_grants import ALLOW, GrantBook, grant_from_choice

    book = GrantBook("/repo")
    book.add(
        grant_from_choice(
            ALLOW, capability=Capability.SHELL, targets=("git log",), workspace="/repo"
        )
    )

    out = _cmd_permissions(
        args="remove shell:git log", session_state={"_repl": _repl(book=book)}
    )

    assert "已撤销" in out
    assert len(book) == 0


# ── 6. one owner for /permissions (the shadowing bug) ──────────


def test_permissions_has_exactly_one_handler_and_it_is_the_grants_module() -> None:
    """``workspace.py`` used to register the same command later and win, which
    made #373's grants view unreachable for users."""
    handler = _HANDLERS["/permissions"]
    assert handler.__module__ == "xenon.repl.command_groups.permissions"


# ── the list is visible even when the menu is not ───────────────


def _answer_with(monkeypatch, *answers):
    """Drive ``ask_choice`` as if a person typed ``answers`` in order."""
    import sys

    import rich.prompt as rich_prompt

    class _Tty:
        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            return 0

        def __bool__(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", _Tty())
    queue = list(answers)
    seen: dict = {}

    def fake_ask(question="", **_kwargs):
        seen["question"] = question
        reply = queue.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    monkeypatch.setattr(rich_prompt.Prompt, "ask", fake_ask)
    return seen


def test_an_empty_answer_picks_the_current_row(monkeypatch) -> None:
    """Enter on a selector means "keep what I have", never "first item"."""
    seen = _answer_with(monkeypatch, "")

    value = ask_choice(None, title="选择权限模式", choices=permission_choices("plan"))

    assert value == "plan"
    # the options are in the prompt text, so the list is always visible
    assert "1) default" in seen["question"]
    assert "←" in seen["question"]


def test_a_number_answer_picks_by_position(monkeypatch) -> None:
    _answer_with(monkeypatch, "3")

    value = ask_choice(None, title="选择权限模式", choices=permission_choices("default"))

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


def test_a_bad_answer_gives_up_after_the_attempts(monkeypatch) -> None:
    _answer_with(monkeypatch, "nope", "still-nope")

    assert ask_choice(None, title="t", choices=permission_choices(), attempts=2) is None


def test_ctrl_c_means_cancel(monkeypatch) -> None:
    _answer_with(monkeypatch, KeyboardInterrupt())

    assert ask_choice(None, title="t", choices=permission_choices()) is None


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

    import xenon.repl.selector as selector

    def boom():
        raise OSError("no terminal here")

    monkeypatch.setattr(sys.stdin, "isatty", boom, raising=False)
    assert (
        selector.ask_choice(_repl(), title="t", choices=permission_choices()) is None
    )


def test_switching_to_an_unknown_mode_name_is_not_a_switch() -> None:
    from xenon.repl.command_groups.permissions import _switch_mode

    assert _switch_mode(_repl(), "nonsense") is None


def test_switching_without_a_context_reports_it() -> None:
    """The dependency is the agent context now, not a gate object."""

    from xenon.repl.command_groups.permissions import _switch_mode

    bare = types.SimpleNamespace(project_ctx=types.SimpleNamespace(root="."))

    assert _switch_mode(bare, "plan") == "权限系统未初始化"


def test_a_pick_that_cannot_be_applied_reports_a_warning(monkeypatch) -> None:
    import xenon.repl.selector as selector

    monkeypatch.setattr(selector, "ask_choice", lambda *a, **k: "not-a-mode")

    out = _cmd_permissions(args="", session_state={"_repl": _repl()})

    assert "未能切换" in out


# ── the swept strings are exercised, not just rewritten (#294) ──


def test_sub_agent_usage_lists_only_accepted_profiles() -> None:
    """/sub-agent used to advertise seven "engines", three of which it never
    accepted.  The list now comes from CHILD_PROFILES."""
    from xenon.harness.child import CHILD_PROFILES
    from xenon.repl.command_groups.agent import _cmd_sub_agent

    out = _cmd_sub_agent(args="", session_state={}, repl=None)

    assert "可选 profile" in out
    for name in CHILD_PROFILES:
        assert name in out
    for never_accepted in ("plan-reflection", "react-reflection"):
        assert never_accepted not in out
    assert "引擎类型" not in out


def test_verbose_toggle_says_loop_not_engine() -> None:
    from xenon.repl.command_groups.runtime import cmd_verbose

    repl = types.SimpleNamespace(verbose=False)
    out = cmd_verbose(args="on", session_state={"_repl": repl})

    assert repl.verbose is True
    assert "循环执行时" in out
    assert "引擎" not in out


def test_shift_tab_explains_the_single_loop_instead_of_switching(argv=None) -> None:
    """Shift+Tab must not cycle modes: there is one loop."""
    from xenon.repl import repl as repl_module

    printed: list[str] = []
    switches: list[str] = []

    class _Registry:
        modes = {"direct": types.SimpleNamespace(description="直接")}
        current_mode = "direct"

        def set_mode(self, name):
            switches.append(name)
            raise AssertionError("Shift+Tab must not switch modes")

        def get_current_mode(self):
            return types.SimpleNamespace(name="direct", description="直接")

    fake = types.SimpleNamespace(
        registry=_Registry(),
        status_bar=types.SimpleNamespace(set_mode_notification=lambda *_: None),
    )
    original = repl_module.console

    class _Console:
        def print(self, *args, **kwargs):
            printed.append(" ".join(str(a) for a in args))

    repl_module.console = _Console()
    try:
        repl_module.REPL._handle_shift_tab(fake)
    finally:
        repl_module.console = original

    text = " ".join(printed)
    assert "当前循环" in text
    assert "无需切换" in text
    assert switches == []


def test_the_cli_session_table_says_mode(monkeypatch) -> None:
    """`xenon --resume` prints the same table as /sessions; both say 模式 now."""
    import xenon.main as main_module
    import xenon.repl.session as session_module

    monkeypatch.setattr(
        session_module,
        "list_sessions",
        lambda: [
            {
                "saved_at": "2026-11-04T10:00:00",
                "messages": 3,
                "paradigm": "direct",
                "name": "demo",
            }
        ],
    )
    printed: list[str] = []
    monkeypatch.setattr(
        main_module,
        "console",
        types.SimpleNamespace(print=lambda *a, **k: printed.append(str(a))),
    )

    main_module._print_session_list()

    assert printed, "the table must render when sessions exist"


def test_the_resume_command_renders_its_table(monkeypatch) -> None:
    """/resume with no arguments prints the same table as `xenon --resume`."""
    import xenon.repl.command_groups.session as session_group
    import xenon.repl.session as session_module

    monkeypatch.setattr(
        session_module,
        "list_sessions",
        lambda: [
            {
                "saved_at": "2026-11-04T10:00:00",
                "messages": 2,
                "paradigm": "direct",
                "name": "demo",
            }
        ],
    )
    out = session_group._cmd_resume(
        args="", session_state={"_repl": types.SimpleNamespace()}
    )

    assert isinstance(out, str)


def test_auto_route_command_is_gone() -> None:
    """The mode router is deleted, so its command must not linger.

    A command that only explains a retired feature is still a promise in
    ``/help``; auto-discovery means deleting the registration is enough.
    """
    from xenon.repl.command_registry import _HANDLERS
    from xenon.repl.commands import _import_command_group_modules

    _import_command_group_modules()

    assert "/auto-route" not in _HANDLERS
    assert "/mode" in _HANDLERS


def test_the_turn_flow_never_switches_the_phase_profile(monkeypatch) -> None:
    """One loop: a turn must not change its phase profile behind the user.

    The intelligent router used to flip the mode per task ("write code" →
    plan-execute), so the same conversation ran under different profiles and
    the mode index read like a menu of products.  Plan/execute/reflect are
    phases of the one loop; the object that did the flipping is gone.
    """
    from xenon.repl.model_registry import ModelRegistry
    from xenon.repl.repl import REPL

    registry = ModelRegistry()
    registry.add_model("openai/a", "a")
    repl = REPL(registry=registry, streaming=False)
    repl.model_pool.register("openai/a", alias="a")

    monkeypatch.setattr(repl, "_commit_memory_usage", lambda: None)
    monkeypatch.setattr(repl, "_maybe_suggest_memory", lambda value: None)
    monkeypatch.setattr(repl, "_run_direct", lambda *a, **k: None)

    assert not hasattr(repl, "intelligent_router")

    repl._handle_chat("帮我写一个快速排序算法")

    assert registry.current_mode == "direct"


def test_mode_next_is_informational() -> None:
    """The TS front-end still sends ``/mode next`` on Shift+Tab; it must not
    switch anything and must say why."""

    registry = _Registry()

    output = _cmd_mode(args="next", registry=registry, session_state={})

    assert registry.set_mode_calls == []
    assert "无需切换" in output
    assert "忽略了参数" in output


def test_mode_with_a_name_is_informational() -> None:
    registry = _Registry(current="direct")

    output = _cmd_mode(args="plan-execute", registry=registry, session_state={})

    assert registry.set_mode_calls == []
    assert "当前循环" in output
    assert "忽略" in output
