"""A failed kernel turn must keep its cause for non-console front-ends."""

from __future__ import annotations

import types

import pytest

import xenon.repl.runtime_adapter as adapter_mod
from xenon.repl.runtime_adapter import run_kernel_turn


class _Console:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def print(self, *parts, **_kwargs) -> None:
        self.lines.append(" ".join(str(part) for part in parts))


def _repl() -> types.SimpleNamespace:
    console = _Console()
    return types.SimpleNamespace(
        registry=None,
        ctx_mgr=types.SimpleNamespace(),
        agent_context=types.SimpleNamespace(set=lambda *_: None),
        console=console,
        _make_callback=lambda: object(),
    )


def test_run_kernel_turn_stores_the_exception_for_the_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repl = _repl()

    def boom(*_args, **_kwargs):
        raise ConnectionError("[SSL] EOF occurred in violation of protocol")

    monkeypatch.setattr(
        adapter_mod.RuntimeAdapter, "from_repl", staticmethod(boom)
    )

    assert run_kernel_turn(
        repl,
        objective="查车次",
        user_input="查车次",
        model_ids=["test/fake"],
        execution_policy=types.SimpleNamespace(level=0),
        callback=lambda *_args, **_kwargs: None,
    ) is None
    assert isinstance(repl._last_kernel_error, ConnectionError)
    assert "EOF" in str(repl._last_kernel_error)


def test_a_successful_call_clears_a_previous_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repl = _repl()
    repl._last_kernel_error = ConnectionError("stale")

    def boom(*_args, **_kwargs):
        raise RuntimeError("fresh cause")

    monkeypatch.setattr(
        adapter_mod.RuntimeAdapter, "from_repl", staticmethod(boom)
    )

    run_kernel_turn(
        repl,
        objective="o",
        user_input="o",
        model_ids=["test/fake"],
        execution_policy=types.SimpleNamespace(level=0),
        callback=lambda *_args, **_kwargs: None,
    )

    assert isinstance(repl._last_kernel_error, RuntimeError)
    assert "fresh cause" in str(repl._last_kernel_error)
