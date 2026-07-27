"""Cache Rails invariants: immutable events and exact-prefix model lanes."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from xenon.repl.prompt_lanes import PromptLaneRegistry, SessionEventLog
from xenon.repl.context_manager import ContextManager
from xenon.utils import llm_client


def test_session_event_log_is_monotonic_and_immutable():
    log = SessionEventLog()
    first = log.append(
        "message",
        role="user",
        content="hello",
        metadata={"tags": ["one"]},
    )
    second = log.append("message", role="assistant", content="hi", model_id="m/a")

    assert [event.event_id for event in log.snapshot()] == [1, 2]
    assert log.events_since(first.event_id) == (second,)
    assert first.metadata == (("tags", ("one",)),)
    with pytest.raises(FrozenInstanceError):
        first.content = "rewritten"  # type: ignore[misc]


def test_new_epoch_appends_control_event_without_deleting_history():
    log = SessionEventLog()
    log.append("message", role="user", content="before")
    marker = log.start_epoch(1, "compact")

    assert marker.event_id == 2
    assert marker.epoch == 1
    assert [event.kind for event in log.snapshot()] == ["message", "epoch_started"]


def test_lane_reuses_an_exactly_extended_prefix():
    lanes = PromptLaneRegistry()
    first_messages = [
        {"role": "system", "content": "fixed"},
        {"role": "user", "content": "one"},
    ]
    first = lanes.prepare("deepseek/pro", "direct", "chat", 0, first_messages)
    second = lanes.prepare(
        "deepseek/pro",
        "direct",
        "chat",
        0,
        [
            *first_messages,
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "two"},
        ],
    )

    assert first.cold_start is True
    assert first.reusable_message_count == 0
    assert second.cold_start is False
    assert second.append_only is True
    assert second.reason == "prefix_extended"
    assert second.reusable_message_count == len(first_messages)
    assert second.reusable_tokens == first.estimated_prompt_tokens


def test_identical_retry_is_fully_reusable():
    lanes = PromptLaneRegistry()
    messages = [{"role": "user", "content": "retry me"}]
    lanes.prepare("deepseek/pro", "direct", "chat", 0, messages)
    retry = lanes.prepare("deepseek/pro", "direct", "chat", 0, messages)

    assert retry.reason == "exact_retry"
    assert retry.reusable_message_count == 1


def test_history_rewrite_forks_lane_generation():
    lanes = PromptLaneRegistry()
    lanes.prepare(
        "deepseek/pro",
        "direct",
        "chat",
        0,
        [{"role": "user", "content": "original"}],
    )
    rewritten = lanes.prepare(
        "deepseek/pro",
        "direct",
        "chat",
        0,
        [{"role": "user", "content": "changed"}],
    )

    assert rewritten.cold_start is True
    assert rewritten.append_only is False
    assert rewritten.generation == 1
    assert rewritten.reason == "history_rewritten"
    snapshots = lanes.snapshots()
    assert len(snapshots) == 2
    assert sum(snapshot["active"] for snapshot in snapshots) == 1


def test_contract_and_model_create_independent_lanes():
    lanes = PromptLaneRegistry()
    messages = [
        {"role": "system", "content": "fixed"},
        {"role": "user", "content": "hello"},
    ]
    direct = lanes.prepare("deepseek/pro", "direct", "chat", 0, messages)
    react = lanes.prepare(
        "deepseek/pro",
        "react",
        "request",
        0,
        messages,
        tools=[{"type": "function", "function": {"name": "read_file"}}],
    )
    flash = lanes.prepare("deepseek/flash", "direct", "chat", 0, messages)

    assert len({direct.lane_id, react.lane_id, flash.lane_id}) == 3
    assert lanes.model_warmth("deepseek/pro")["lane_id"]


def test_context_manager_appends_events_and_advances_epoch():
    context = ContextManager()
    context.add_user_message("hello")
    context.add_assistant_message("hi", model_used="deepseek/pro")

    assert context.event_cursor == 2
    events = context.event_log.snapshot()
    assert [event.role for event in events] == ["user", "assistant"]
    assert events[-1].model_id == "deepseek/pro"

    context.clear()
    assert context.cache_epoch == 1
    assert context.event_log.snapshot()[-1].kind == "epoch_started"


def test_compiled_provider_call_records_lane_without_network(monkeypatch):
    lanes = PromptLaneRegistry()
    monkeypatch.setattr(
        llm_client,
        "_call_openai_compat",
        lambda endpoint, messages, max_tokens, temperature, timeout: "ok",
    )
    common = [
        {"role": "system", "content": "fixed"},
        {"role": "user", "content": "one"},
    ]
    options = {
        "credentials": {"deepseek": "test-key"},
        "max_retries": 1,
        "cache_lane_registry": lanes,
        "cache_context": {
            "engine": "direct",
            "phase": "chat",
            "context_epoch": 0,
            "event_cursor": 2,
        },
    }

    assert llm_client.chat_completion("deepseek/test", common, **options) == "ok"
    assert llm_client.chat_completion(
        "deepseek/test",
        [
            *common,
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "two"},
        ],
        **options,
    ) == "ok"

    snapshot = lanes.snapshots()
    assert len(snapshot) == 1
    assert snapshot[0]["request_count"] == 2
    assert snapshot[0]["last_event_id"] == 2


def test_request_envelope_freezes_dynamic_context_and_keeps_prefix_append_only():
    context = ContextManager()
    context.add_system_message("fixed")
    context.set_context_message("project", "stable project", stable=True)
    context.update_working_memory("active_file", "/work/main.py")
    context.set_context_message("retrieval", "first memory")

    first_prompt = context.add_request_message("question one")
    first_messages = context.get_messages(include_context_messages=True)
    context.prompt_lanes.prepare(
        "deepseek/pro", "direct", "chat", 0, first_messages,
    )
    context.add_assistant_message("answer one", model_used="deepseek/pro")
    context.set_context_message("retrieval", "second memory")
    second_prompt = context.add_request_message("question two")
    second_messages = context.get_messages(include_context_messages=True)
    decision = context.prompt_lanes.prepare(
        "deepseek/pro", "direct", "chat", 0, second_messages,
    )

    assert "first memory" in first_prompt
    assert "second memory" not in first_prompt
    assert "second memory" in second_prompt
    assert context.get_context_messages(stable=False) == []
    assert decision.append_only is True
    assert decision.reason == "prefix_extended"
