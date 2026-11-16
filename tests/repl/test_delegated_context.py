"""A chat turn that escalates to the kernel keeps its conversation."""

from __future__ import annotations

from typing import Any

import pytest

import xenon.repl.runtime_adapter as adapter_mod
from xenon.repl.kernel_host import KernelHostMixin


class _Repl(KernelHostMixin):
    """Only the surface ``_run_loop_turn`` touches."""

    def __init__(self, ctx_mgr: Any, callback: Any = None) -> None:
        self.agent_context = _AgentContext()
        self.ctx_mgr = ctx_mgr
        if callback is not None:
            self._protocol_callback = callback


class _AgentContext:
    def get(self, _key: str) -> None:
        return None

    def update(self, _values: dict[str, Any]) -> None:
        pass


class _CtxManager:
    def __init__(self, get_prompt_messages: Any = None) -> None:
        if get_prompt_messages is not None:
            self.get_prompt_messages = get_prompt_messages

    def add_assistant_message(self, _text: str, **_kwargs: Any) -> None:
        pass

    def trim_last_user(self) -> None:
        pass


def test_a_delegated_turn_carries_the_conversation_it_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(_repl: Any, **kwargs: Any) -> str:
        captured.update(kwargs)
        return "RUN"

    monkeypatch.setattr(adapter_mod, "run_kernel_turn", fake_run)

    def get_prompt_messages(**kwargs: Any) -> list[dict[str, str]]:
        assert kwargs["intent"] == "chat"
        return [
            {"role": "user", "content": "请记住这个数字：42"},
            {"role": "assistant", "content": "42"},
        ]

    repl = _Repl(
        _CtxManager(get_prompt_messages=get_prompt_messages), callback="callback"
    )

    outcome = repl._run_loop_turn(
        "我刚才让你记住的数字是多少？", ["test/model"]
    )

    assert outcome == "RUN"
    assert captured["callback"] == "callback"
    blocks = captured["extra_blocks"]
    assert blocks and blocks[0][0] == "conversation"
    assert "42" in blocks[0][1]
    assert "请记住这个数字" in blocks[0][1]


def test_a_missing_history_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        adapter_mod,
        "run_kernel_turn",
        lambda _repl, **kwargs: captured.update(kwargs) or "RUN",
    )
    repl = _Repl(_CtxManager())

    assert repl._run_loop_turn("继续", ["test/model"]) == "RUN"
    assert captured["extra_blocks"] == ()
