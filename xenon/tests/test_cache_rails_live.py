"""Opt-in real-provider acceptance test for append-only Cache Rails."""

from __future__ import annotations

import pytest

from xenon.repl.context_manager import ContextManager
from xenon.repl.provider_registry import load_credentials
from xenon.utils.deepseek_cache import CacheTracker
from xenon.utils.llm_client import chat_completion


pytestmark = pytest.mark.live


def test_deepseek_reports_cache_usage_for_extended_prompt_lane() -> None:
    credentials = load_credentials()
    api_key = str(credentials.get("deepseek") or "")
    if not api_key:
        pytest.skip("DeepSeek credential is not configured")

    model_id = "deepseek/deepseek-v4-flash"
    context = ContextManager()
    tracker = CacheTracker()
    # Long enough to exceed small provider cache-block thresholds while still
    # keeping this opt-in acceptance test inexpensive.
    context.add_system_message(
        "Xenon Cache Rails acceptance prefix. " * 160
        + "Reply with only the requested marker."
    )
    try:
        first = context.add_request_message("Reply exactly: rail-one")
        assert first.endswith("rail-one")
        first_messages = context.get_messages()
        first_answer = chat_completion(
            model_id,
            first_messages,
            credentials={"deepseek": api_key},
            max_tokens=32,
            temperature=0,
            max_retries=1,
            cache_lane_registry=context.prompt_lanes,
            cache_context={
                "engine": "direct",
                "phase": "chat",
                "context_epoch": context.cache_epoch,
                "event_cursor": context.event_cursor,
            },
        )
        context.add_assistant_message(first_answer, model_used=model_id)
        context.add_request_message("Reply exactly: rail-two")
        second_answer = chat_completion(
            model_id,
            context.get_messages(),
            credentials={"deepseek": api_key},
            max_tokens=32,
            temperature=0,
            max_retries=1,
            cache_lane_registry=context.prompt_lanes,
            cache_context={
                "engine": "direct",
                "phase": "chat",
                "context_epoch": context.cache_epoch,
                "event_cursor": context.event_cursor,
            },
        )

        assert first_answer.strip()
        assert second_answer.strip()
        lane = context.prompt_lanes.snapshots()
        assert len(lane) == 1
        assert lane[0]["request_count"] == 2
        latest = tracker.latest_event
        assert latest is not None
        assert latest["cache_fields_present"] is True
        assert latest["lane_append_only"] is True
        assert latest["lane_reason"] == "prefix_extended"
        assert latest["lane_reusable_tokens"] > 0
        print({
            "model": latest["model_id"],
            "hit_tokens": latest["cache_hit_tokens"],
            "miss_tokens": latest["cache_miss_tokens"],
            "lane_reusable_tokens": latest["lane_reusable_tokens"],
            "lane_reason": latest["lane_reason"],
        })
    finally:
        tracker.close()

