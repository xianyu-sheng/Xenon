"""Regression tests for model health and REPL interruption recovery."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import httpx
import pytest

import xenon.engine.base as base_module
from xenon.engine.react_engine import ReActEngine
from xenon.repl.model_pool import ModelPool


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.test/v1/chat")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(
        f"HTTP {status}", request=request, response=response
    )


def _repl():
    from xenon.repl.model_registry import ModelRegistry
    from xenon.repl.repl import REPL

    registry = ModelRegistry()
    registry.add_model("openai/a", "a")
    registry.add_model("openai/b", "b")
    repl = REPL(registry=registry, streaming=False)
    repl.model_pool.register("openai/a", alias="a")
    repl.model_pool.register("openai/b", alias="b")
    return repl


def test_base_engine_records_elapsed_latency(monkeypatch):
    pool = ModelPool()
    pool.register("openai/a", alias="a")
    engine = ReActEngine(["openai/a"], model_pool=pool)
    ticks = iter([10.0, 10.25])

    monkeypatch.setattr(
        base_module,
        "time",
        SimpleNamespace(monotonic=lambda: next(ticks)),
    )
    monkeypatch.setattr(base_module, "chat_completion", lambda *args, **kwargs: "ok")

    assert engine._call_llm([{"role": "user", "content": "hi"}]) == "ok"
    assert pool.get("a").health.avg_latency == pytest.approx(0.25)


def test_direct_transient_failure_does_not_blacklist_model(monkeypatch):
    repl = _repl()

    def response(model_id, messages):
        if model_id == "openai/a":
            raise httpx.ConnectError("temporary outage")
        return "fallback worked"

    monkeypatch.setattr(repl, "_blocking_response", response)
    repl._run_direct("hello", ["openai/a", "openai/b"])

    assert "openai/a" not in repl._failed_models
    assert repl.model_pool.get("a").health.consecutive_failures == 1
    assert repl.model_pool.get("a").health.circuit_open_until == 0


def test_direct_failed_half_open_probe_increases_backoff(monkeypatch):
    repl = _repl()
    entry = repl.model_pool.get("a")
    entry.health.consecutive_failures = 3
    entry.health.circuit_open_until = 1.0

    def response(model_id, messages):
        if model_id == "openai/a":
            raise httpx.ConnectError("temporary outage")
        return "fallback worked"

    monkeypatch.setattr(repl, "_blocking_response", response)
    repl._run_direct("hello", ["openai/a", "openai/b"])

    assert entry.health.retry_cycle_count == 1
    assert entry.health.circuit_open_until > 1.0


def test_direct_terminal_failure_is_quarantined_for_session(monkeypatch):
    repl = _repl()

    def response(model_id, messages):
        if model_id == "openai/a":
            raise _status_error(401)
        return "fallback worked"

    monkeypatch.setattr(repl, "_blocking_response", response)
    repl._run_direct("hello", ["openai/a", "openai/b"])

    assert "openai/a" in repl._failed_models


def test_direct_captures_provider_logs_away_from_spinner(monkeypatch):
    repl = _repl()
    rendered = []

    def response(model_id, messages):
        logging.getLogger("httpx").warning("HTTP Request: POST example.test 200 OK")
        return "clean answer"

    monkeypatch.setattr(repl, "_blocking_response", response)
    monkeypatch.setattr(
        repl,
        "_render_assistant_text",
        lambda content, **kwargs: rendered.append(content),
    )

    repl._run_direct("hello", ["openai/a"])

    assert rendered == ["clean answer"]
    assert "HTTP Request" in repl._captured_log
    assert repl._last_thinking_panel is None
    assert repl._log_capture_active is False


def test_interrupted_engine_restores_all_logging_state(monkeypatch):
    from xenon.engine import react_engine as react_module

    class InterruptingEngine:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, *args, **kwargs):
            raise KeyboardInterrupt

    monkeypatch.setattr(react_module, "ReActEngine", InterruptingEngine)
    repl = _repl()
    root = logging.getLogger()
    child = logging.getLogger("xenon.test_interrupt_cleanup")
    original_root_handlers = list(root.handlers)
    original_child_handlers = list(child.handlers)
    original_child_propagate = child.propagate

    with pytest.raises(KeyboardInterrupt):
        repl._run_react_engine("interrupt", ["openai/a"])

    assert list(root.handlers) == original_root_handlers
    assert list(child.handlers) == original_child_handlers
    assert child.propagate is original_child_propagate
    assert repl._log_capture_active is False


def test_log_capture_records_propagated_message_once():
    repl = _repl()
    test_logger = logging.getLogger("xenon.test_single_capture")

    repl._start_log_capture()
    test_logger.warning("one unique message")
    captured = repl._stop_log_capture()

    assert captured.count("one unique message") == 1
    assert repl._stop_log_capture() == ""
