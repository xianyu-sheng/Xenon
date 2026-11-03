"""REPL turns must hand project rules and memories to the kernel (#340)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from xenon.repl.runtime_adapter import session_context_blocks


@pytest.fixture(autouse=True)
def _no_real_skills(monkeypatch):
    """These tests pin the *context-layer* behaviour; the skill index (#370)
    is covered by its own test file, so the real skill directory is stubbed
    empty here to keep the assertions exact.
    """

    monkeypatch.setattr(
        "xenon.repl.runtime_adapter._skill_index", lambda repl, limit=40: ""
    )


class _Ctx:
    def __init__(self, layers):
        self._layers = layers

    def context_layers(self):
        return list(self._layers)


def _repl(**kwargs):
    base = dict(ctx_mgr=_Ctx([("project", "规则：不要修改 tests/", True)]))
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_blocks_come_from_the_context_layers():
    blocks = session_context_blocks(_repl(), "review the repo")

    assert blocks == (("project", "规则：不要修改 tests/"),)


def test_injection_happens_before_the_blocks_are_read():
    seen: list[str] = []

    def _inject_project():
        seen.append("project")

    def _inject_memories(query):
        seen.append(f"memory:{query}")

    repl = SimpleNamespace(
        ctx_mgr=_Ctx([("project", "rules", True), ("memory", "prefs", False)]),
        _inject_project_context=_inject_project,
        _inject_memories=_inject_memories,
    )

    blocks = session_context_blocks(repl, "fix the bug")

    assert seen == ["project", "memory:fix the bug"]
    assert [label for label, _ in blocks] == ["project", "memory"]


def test_blank_layers_are_dropped():
    repl = _repl(ctx_mgr=_Ctx([("project", "   ", True), ("memory", "keep", False)]))

    assert session_context_blocks(repl, "x") == (("memory", "keep"),)


def test_a_broken_injection_does_not_break_the_turn():
    def _boom(*args, **kwargs):
        raise RuntimeError("memory store corrupt")

    repl = SimpleNamespace(
        ctx_mgr=_Ctx([("project", "rules", True)]),
        _inject_project_context=_boom,
        _inject_memories=_boom,
    )

    assert session_context_blocks(repl, "x") == (("project", "rules"),)


def test_a_repl_without_a_context_manager_yields_nothing():
    assert session_context_blocks(SimpleNamespace(), "x") == ()


def test_a_broken_layer_reader_yields_nothing():
    class _Bad:
        def context_layers(self):
            raise RuntimeError("nope")

    repl = SimpleNamespace(ctx_mgr=_Bad())

    assert session_context_blocks(repl, "x") == ()
