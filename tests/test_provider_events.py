"""Provider-phase events on the kernel path (#320).

Before this, `provider_request_start/end` were declared on `EngineCallback` and
emitted only by the engine layer.  When that layer was deleted the hooks became
dead code: the REPL could say "still generating", never "retrying after a 429".

These tests pin three things: the events actually fire on the kernel path, the
attempt numbers tell the truth, and a broken sink cannot fail a run.
"""

from __future__ import annotations

import time
import types
from pathlib import Path

import httpx
import pytest

import xenon.kernel  # noqa: F401  — ledger<->kernel import cycle
from xenon.harness.llm import ModelConfig, _call_model, bind_request_sink
from xenon.utils import llm_client
from xenon.utils.provider_events import (
    END,
    START,
    emit,
    request_end,
    request_start,
)


def _sink() -> tuple[list[dict], object]:
    events: list[dict] = []

    def sink(payload: dict) -> None:
        events.append(payload)

    return events, sink


# ── the sink itself ─────────────────────────────────────────────────


def test_start_carries_phase_timeout_and_attempts() -> None:
    events, sink = _sink()

    request_start(
        sink, phase="deepseek/x", timeout=120.0, provider_attempts=3, attempt=1
    )

    assert events[0]["kind"] == START
    assert events[0]["phase"] == "deepseek/x"
    assert events[0]["timeout"] == 120.0
    assert events[0]["provider_attempts"] == 3
    assert events[0]["attempt"] == 1
    assert events[0]["streamed"] is False


def test_end_separates_retrying_from_exhausted() -> None:
    events, sink = _sink()

    request_end(sink, phase="p", success=False, attempt=1, retrying=True, reason="429")
    request_end(sink, phase="p", success=False, attempt=3, retrying=False)

    assert [event["kind"] for event in events] == [END, END]
    assert events[0]["retrying"] is True
    assert events[1]["retrying"] is False
    assert events[1]["attempt"] == 3


def test_a_broken_sink_is_swallowed() -> None:
    """Telemetry must never be able to lose a run."""

    def boom(payload: dict) -> None:
        raise RuntimeError("ui exploded")

    emit(boom, {"kind": START})  # must not raise
    emit(None, {"kind": START})


# ── the real transport reports attempts truthfully ──────────────────


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.invalid/v1/chat")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def test_a_retry_reports_both_attempts(monkeypatch) -> None:
    events, sink = _sink()
    calls: list[int] = []

    def flaky(endpoint, messages, max_tokens, temperature, timeout, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise _http_status_error(429)
        return "ok"

    monkeypatch.setattr(llm_client, "_call_openai_compat", flaky)
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    text = llm_client.chat_completion(
        "deepseek/x",
        [{"role": "user", "content": "hi"}],
        credentials={"deepseek": "sk-test"},
        request_sink=sink,
    )

    assert text == "ok"
    starts = [e for e in events if e["kind"] == START]
    ends = [e for e in events if e["kind"] == END]
    assert [e["attempt"] for e in starts] == [1, 2]
    assert ends[0]["retrying"] is True and ends[0]["reason"] == "429 rate limited"
    assert ends[-1]["success"] is True


def test_exhausting_the_attempts_says_so(monkeypatch) -> None:
    events, sink = _sink()

    def always_429(endpoint, messages, max_tokens, temperature, timeout, **kwargs):
        raise _http_status_error(429)

    monkeypatch.setattr(llm_client, "_call_openai_compat", always_429)
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    with pytest.raises(httpx.HTTPStatusError):
        llm_client.chat_completion(
            "deepseek/x",
            [{"role": "user", "content": "hi"}],
            credentials={"deepseek": "sk-test"},
            max_retries=2,
            request_sink=sink,
        )

    starts = [e for e in events if e["kind"] == START]
    ends = [e for e in events if e["kind"] == END]
    assert len(starts) == 2
    assert ends[-1]["retrying"] is False
    assert ends[-1]["attempt"] == 2


def test_a_timeout_is_reported_with_its_exception_name(monkeypatch) -> None:
    events, sink = _sink()

    def timing_out(endpoint, messages, max_tokens, temperature, timeout, **kwargs):
        raise httpx.ReadTimeout("too slow")

    monkeypatch.setattr(llm_client, "_call_openai_compat", timing_out)
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    with pytest.raises(httpx.ReadTimeout):
        llm_client.chat_completion(
            "deepseek/x",
            [{"role": "user", "content": "hi"}],
            credentials={"deepseek": "sk-test"},
            max_retries=1,
            request_sink=sink,
        )

    ends = [e for e in events if e["kind"] == END]
    assert ends[-1]["reason"] == "ReadTimeout"
    assert ends[-1]["success"] is False


def test_a_non_retryable_http_error_is_reported_once(monkeypatch) -> None:
    events, sink = _sink()

    def bad_request(endpoint, messages, max_tokens, temperature, timeout, **kwargs):
        raise _http_status_error(400)

    monkeypatch.setattr(llm_client, "_call_openai_compat", bad_request)

    with pytest.raises(httpx.HTTPStatusError):
        llm_client.chat_completion(
            "deepseek/x",
            [{"role": "user", "content": "hi"}],
            credentials={"deepseek": "sk-test"},
            request_sink=sink,
        )

    starts = [e for e in events if e["kind"] == START]
    ends = [e for e in events if e["kind"] == END]
    assert len(starts) == 1 and len(ends) == 1
    assert ends[0]["reason"] == "400 http error"


# ── the kernel path bridges the sink ────────────────────────────────


def test_call_model_forwards_the_request_sink(monkeypatch) -> None:
    events, sink = _sink()
    seen: dict = {}

    def fake_caller(model_id, messages, **kwargs):
        seen.update(kwargs)
        return "ok"

    config = ModelConfig(model_id="test/fake", request_sink=sink)
    _call_model(config, fake_caller, [{"role": "user", "content": "hi"}])

    assert seen["request_sink"] is sink


def test_without_a_sink_nothing_is_attached() -> None:
    seen: dict = {}

    def fake_caller(model_id, messages, **kwargs):
        seen.update(kwargs)
        return "ok"

    _call_model(
        ModelConfig(model_id="test/fake"), fake_caller, [{"role": "user", "content": "x"}]
    )

    assert "request_sink" not in seen


def test_bind_request_sink_reaches_every_provider() -> None:
    from xenon.harness.llm import build_llm_loop_strategy
    from xenon.kernel.strategies import LoopProfile

    strategy = build_llm_loop_strategy(
        ModelConfig(model_id="test/fake"), LoopProfile(plan=True, step_scope=True)
    )
    events, sink = _sink()

    bind_request_sink(strategy, sink)

    bound = [
        strategy.actor,
        strategy.planner,
        strategy.step_actor,
    ]
    assert all(provider.config.request_sink is sink for provider in bound if provider)


def test_binding_none_is_a_no_op(tmp_path: Path) -> None:
    from xenon.harness.llm import build_llm_loop_strategy
    from xenon.kernel.strategies import LoopProfile

    strategy = build_llm_loop_strategy(
        ModelConfig(model_id="test/fake"), LoopProfile()
    )
    before = strategy.actor.config

    assert bind_request_sink(strategy, None) is strategy
    assert strategy.actor.config is before


# ── the REPL renders it ─────────────────────────────────────────────


class _Console:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def print(self, text: str, **kwargs) -> None:
        self.lines.append(text)


def _repl(console: _Console) -> types.SimpleNamespace:
    return types.SimpleNamespace(console=console, _console=console)


def test_a_single_attempt_is_shown_without_attempt_numbers() -> None:
    from xenon.repl.runtime_adapter import kernel_request_reporter

    console = _Console()
    report = kernel_request_reporter(_repl(console))

    report({"kind": START, "phase": "deepseek/x", "provider_attempts": 1, "attempt": 1})

    assert "请求中" in console.lines[0]
    assert "1/1" not in console.lines[0]


def test_retries_show_the_attempt_count() -> None:
    from xenon.repl.runtime_adapter import kernel_request_reporter

    console = _Console()
    report = kernel_request_reporter(_repl(console))

    report(
        {
            "kind": START,
            "phase": "deepseek/x",
            "provider_attempts": 3,
            "attempt": 2,
            "timeout": 120.0,
        }
    )

    assert "2/3" in console.lines[0]
    assert "120s" in console.lines[0]


def test_a_retry_notice_differs_from_exhaustion() -> None:
    from xenon.repl.runtime_adapter import kernel_request_reporter

    console = _Console()
    report = kernel_request_reporter(_repl(console))

    report({"kind": END, "phase": "p", "success": False, "attempt": 1, "retrying": True, "reason": "429"})
    report({"kind": END, "phase": "p", "success": False, "attempt": 3, "retrying": False, "reason": "429"})

    assert "将重试" in console.lines[0]
    assert "已用尽" in console.lines[1]


def test_a_successful_end_prints_nothing() -> None:
    from xenon.repl.runtime_adapter import kernel_request_reporter

    console = _Console()
    report = kernel_request_reporter(_repl(console))

    report({"kind": END, "phase": "p", "success": True, "attempt": 1})

    assert console.lines == []


def test_a_broken_console_does_not_break_the_run() -> None:
    from xenon.repl.runtime_adapter import kernel_request_reporter

    class Bad(_Console):
        def print(self, text: str, **kwargs) -> None:
            raise RuntimeError("no tty")

    report = kernel_request_reporter(_repl(Bad()))

    report({"kind": START, "phase": "p", "provider_attempts": 1, "attempt": 1})


# ── streaming reports one honest attempt ────────────────────────────


def test_streaming_has_no_retry_so_it_reports_one_attempt(monkeypatch) -> None:
    events, sink = _sink()

    def body(**kwargs):
        yield "he"
        yield "llo"

    monkeypatch.setattr(llm_client, "_stream_body", body)

    text = "".join(
        llm_client.chat_completion_stream(
            "deepseek/x",
            [{"role": "user", "content": "hi"}],
            credentials={"deepseek": "sk-test"},
            request_sink=sink,
        )
    )

    assert text == "hello"
    starts = [e for e in events if e["kind"] == START]
    ends = [e for e in events if e["kind"] == END]
    assert starts[0]["provider_attempts"] == 1
    assert starts[0]["streamed"] is True
    assert ends[-1]["success"] is True


def test_a_streaming_failure_is_reported_then_raised(monkeypatch) -> None:
    events, sink = _sink()

    def body(**kwargs):
        yield "partial"
        raise httpx.RemoteProtocolError("cut off")

    monkeypatch.setattr(llm_client, "_stream_body", body)

    with pytest.raises(httpx.RemoteProtocolError):
        list(
            llm_client.chat_completion_stream(
                "deepseek/x",
                [{"role": "user", "content": "hi"}],
                credentials={"deepseek": "sk-test"},
                request_sink=sink,
            )
        )

    ends = [e for e in events if e["kind"] == END]
    assert ends[-1]["success"] is False
    assert ends[-1]["reason"] == "RemoteProtocolError"


def test_run_kernel_turn_attaches_the_reporter(tmp_path: Path, monkeypatch) -> None:
    """`run_kernel_turn` is where the front-end sinks are attached."""

    from tests.test_repl_kernel_runtime import FakeRepl
    from xenon.repl import runtime_adapter as ra
    from xenon.repl.execution_policy import ExecutionLevel
    from xenon.repl.model_registry import ModelRegistry

    registry = ModelRegistry()
    registry.add_model("test/fake", "fake")
    repl = FakeRepl(tmp_path, registry)
    seen: dict = {}

    class _Run:
        ledger_path = tmp_path / "l.jsonl"
        task_id = "t"
        from xenon.kernel.contracts import EvidenceStatus, TaskPhase

        result = types.SimpleNamespace(
            answer="",
            accepted=True,
            phase=TaskPhase.DELIVERED,
            debt=(),
            metrics={},
            evidence_status=EvidenceStatus.VERIFIED,
            finalize_sha="",
        )

    def fake_run(self, *args, **kwargs):
        seen["request_sink"] = self.request_sink
        seen["progress_sink"] = self.progress_sink
        return _Run()

    monkeypatch.setattr(ra.RuntimeAdapter, "run", fake_run)
    policy = types.SimpleNamespace(level=ExecutionLevel.EXECUTE)
    ra.run_kernel_turn(
        repl,
        objective="inspect",
        user_input="inspect",
        model_ids=["test/fake"],
        execution_policy=policy,
        client=lambda *a, **k: "{}",
    )

    assert seen["request_sink"] is not None
    assert callable(seen["request_sink"])
    assert seen["progress_sink"] is not None


def test_the_adapter_binds_whatever_sink_it_is_given(tmp_path: Path) -> None:
    """The end of the chain: a supplied sink reaches the built strategy."""

    from tests.test_repl_kernel_runtime import FakeRepl
    from xenon.harness import llm
    from xenon.repl.execution_policy import ExecutionLevel
    from xenon.repl.model_registry import ModelRegistry
    from xenon.repl.runtime_adapter import RuntimeAdapter

    registry = ModelRegistry()
    registry.add_model("test/fake", "fake")
    repl = FakeRepl(tmp_path, registry)
    events, sink = _sink()
    adapter = RuntimeAdapter.from_repl(
        repl,
        model_ids=["test/fake"],
        level=ExecutionLevel.EXECUTE,
        client=_quiet_client(),
    )
    adapter.request_sink = sink
    bound: list = []
    real = llm.bind_request_sink

    def spy(strategy, bound_sink):
        bound.append(bound_sink)
        return real(strategy, bound_sink)

    llm.bind_request_sink = spy
    try:
        adapter.run("inspect the workspace")
    finally:
        llm.bind_request_sink = real

    assert bound == [sink], "the adapter must bind the sink it was given"


def _quiet_client():
    """A client that always says "finish now" so the turn ends immediately."""

    def client(model_id, messages, **kwargs):
        return '{"action":"finish","answer":"nothing to do","claims":[]}'

    return client
