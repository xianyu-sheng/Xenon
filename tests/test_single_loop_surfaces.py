"""One loop, one description: every surface must say the same thing.

The seven engines are gone (README: "the seven isomorphic engines are gone; a
mode is just a phase profile of the same loop", architecture doc §2.2/§4.1).
What survived the refactor was the *surface* still implying otherwise: ``/mode``
leading with an internal preset name, ``/auto-route`` offering to switch
profiles no turn consults, chat copy announcing a switch to "ReAct 模式".

These tests pin two things: one formatter (``describe_loop``) is the source of
the line every surface prints, and no module still pretends a mode is chosen
per turn.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from xenon.kernel.strategies import LoopProfile
from xenon.modes import describe_loop, loop_phases, loop_profile_for

ROOT = Path(__file__).resolve().parents[1]

#: The design document's phase names, in the order the loop runs them (§4.2).
DEFAULT_LINE = "单循环 · Plan → React（逐步） → Reflect（每步校验）"


def _source(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _registry():
    from xenon.repl.model_registry import ModelRegistry

    return ModelRegistry()


# ── the formatter ───────────────────────────────────────────────────


def test_the_default_loop_reads_like_the_design_document():
    assert describe_loop(loop_profile_for("direct")) == DEFAULT_LINE


def test_an_alias_describes_the_same_loop_as_its_target():
    # ``react`` is kept so ``--mode react`` still starts; it declares exactly
    # what ``direct`` declares, so it must not read as a different product.
    assert loop_profile_for("react") == loop_profile_for("direct")
    assert describe_loop(loop_profile_for("react")) == DEFAULT_LINE


def test_the_line_is_derived_from_the_profile_not_its_name():
    # A finish-reflection-only preset really has no Plan phase: the line says
    # so, because it reads the fields rather than a name→description table.
    profile = LoopProfile(reflect_rounds=2, reflect_calls=1)
    assert loop_phases(profile) == "React → Reflect（收尾×2）"
    assert loop_phases(None) == "未绑定阶段"


# ── the command surfaces ────────────────────────────────────────────


def test_mode_prints_the_loop_and_the_rules_not_a_preset_name():
    from xenon.repl.command_groups.model import _cmd_mode

    out = _cmd_mode(args="", registry=_registry())

    assert DEFAULT_LINE in out
    # The internal preset name is what made the user ask "am I still in
    # direct mode?" — it is configuration, not a choice.
    assert "direct" not in out
    # And the answer says what the loop actually does, with numbers imported
    # from the code that enforces them.
    assert "运行规则" in out
    assert "最多 3 轮" in out


def test_mode_ignores_a_switch_argument_instead_of_switching():
    from xenon.repl.command_groups.model import _cmd_mode

    out = _cmd_mode(args="next", registry=_registry())

    assert DEFAULT_LINE in out
    assert "忽略" in out


def test_status_prints_the_same_loop_line():
    from xenon.repl.command_groups.resources import _cmd_status
    from xenon.repl.context_manager import ContextManager

    out = _cmd_status(ctx_mgr=ContextManager(), registry=_registry(), session_state={})

    assert f"循环: {DEFAULT_LINE}" in out
    assert "模式: direct" not in out


def test_the_dashboard_snapshot_uses_the_same_label():
    from xenon.frontend.queries import _loop_label

    repl = SimpleNamespace(registry=_registry())
    assert _loop_label(repl) == DEFAULT_LINE
    # A broken registry must degrade to the label, never invent phases.
    assert _loop_label(SimpleNamespace()) == "单循环 · 未绑定阶段"


# ── nothing still switches a profile per turn ───────────────────────


def test_the_mode_router_is_gone_not_just_disabled():
    # Keeping a disabled router would leave a reader believing some turn still
    # consults it.  The modules, the object and the command are all deleted.
    import importlib.util

    assert importlib.util.find_spec("xenon.repl.intelligent_router") is None
    assert importlib.util.find_spec("xenon.repl.paradigm_selector") is None
    assert importlib.util.find_spec("xenon.repl.feature_extractor") is None
    assert "intelligent_router" not in _source("xenon/repl/repl.py")
    assert "auto_route" not in _source("xenon/repl/system_config.py")


def test_chat_copy_never_announces_a_switch_to_another_mode():
    source = _source("xenon/repl/direct_chat.py")
    assert "自动切换到 ReAct 模式" not in source
    assert "ReAct 模式重试" not in source


def test_auto_route_command_is_unregistered():
    from xenon.repl.command_registry import _HANDLERS

    assert "/auto-route" not in _HANDLERS
