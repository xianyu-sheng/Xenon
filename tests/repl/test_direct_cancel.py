"""Ctrl+C must stop a direct (no-tool) turn, not just a kernel one.

A protocol host runs turns on a worker thread, so Ctrl+C arrives as a
``turn.cancel`` frame and the host exposes ``_turn_cancel_probe``.  The direct
path used to ignore it entirely: the reply streamed to the end and the turn
finished as an ordinary answer.
"""

from __future__ import annotations

import types

import pytest

import xenon.utils.llm_client as client
from xenon.kernel.cancel import ModelCancelled
from xenon.repl.direct_chat import DirectChatMixin


class _Repl(DirectChatMixin):
    """Just enough REPL for the direct path; nothing touches a real model."""

    def __init__(self, probe: object = None) -> None:
        self.registry = types.SimpleNamespace(get_model_by_id=lambda model_id: None)
        self.ctx_mgr = types.SimpleNamespace(
            prompt_lanes=object(), cache_epoch=0, event_cursor=0
        )
        if probe is not None:
            self._turn_cancel_probe = probe


def test_without_a_probe_a_turn_is_never_cancelled() -> None:
    assert _Repl()._turn_cancel_requested() is False


def test_the_probe_is_the_only_cancel_signal() -> None:
    assert _Repl(lambda: True)._turn_cancel_requested() is True
    assert _Repl(lambda: False)._turn_cancel_requested() is False


def test_a_broken_probe_does_not_kill_the_turn() -> None:
    def broken() -> bool:
        raise RuntimeError("probe exploded")

    assert _Repl(broken)._turn_cancel_requested() is False


def test_streaming_stops_between_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """The chunk loop is where a streaming reply becomes interruptible."""

    state = {"cancelled": False}

    def fake_stream(*_args: object, **_kwargs: object):
        for chunk in ("第一段", "第二段", "第三段"):
            yield chunk
            state["cancelled"] = True

    # The method imports the symbol lazily, so patch the module it resolves from.
    monkeypatch.setattr(client, "chat_completion_stream", fake_stream)

    repl = _Repl(lambda: state["cancelled"])

    with pytest.raises(ModelCancelled):
        repl._stream_response("test/fake", [{"role": "user", "content": "写一段散文"}])
