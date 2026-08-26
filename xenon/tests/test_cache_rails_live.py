"""Opt-in real-provider acceptance test for append-only Cache Rails."""

from __future__ import annotations

import secrets

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
        print(
            {
                "model": latest["model_id"],
                "hit_tokens": latest["cache_hit_tokens"],
                "miss_tokens": latest["cache_miss_tokens"],
                "lane_reusable_tokens": latest["lane_reusable_tokens"],
                "lane_reason": latest["lane_reason"],
            }
        )
    finally:
        tracker.close()


def test_deepseek_cache_rails_survive_ten_alternating_model_calls() -> None:
    """Exercise two routed models repeatedly without rebuilding either lane."""
    credentials = load_credentials()
    api_key = str(credentials.get("deepseek") or "")
    if not api_key:
        pytest.skip("DeepSeek credential is not configured")

    models = (
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
    )
    context = ContextManager()
    tracker = CacheTracker()
    run_id = secrets.token_hex(8)
    context.add_system_message(
        (f"Xenon ten-turn Cache Rails acceptance prefix {run_id}. " * 160)
        + "Reply with only the requested marker."
    )
    events: list[dict[str, object]] = []

    try:
        for turn in range(1, 11):
            model_id = models[(turn - 1) % len(models)]
            context.add_request_message(f"Reply exactly: alternating-rail-{turn}")
            answer = chat_completion(
                model_id,
                context.get_messages(),
                credentials={"deepseek": api_key},
                # Pro may consume part of this budget on hidden reasoning even
                # when the visible answer is only a short marker.
                max_tokens=512,
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
            assert answer.strip()

            latest = tracker.latest_event
            assert latest is not None
            event = dict(latest)
            event["turn"] = turn
            events.append(event)
            context.add_assistant_message(answer, model_used=model_id)

        snapshots = context.prompt_lanes.snapshots()
        lanes = [lane for lane in snapshots if lane["active"]]
        archived = [lane for lane in snapshots if not lane["active"]]
        assert len(lanes) == 2
        assert archived == []
        assert sorted(lane["request_count"] for lane in lanes) == [5, 5]
        assert all(event["cache_fields_present"] is True for event in events)

        reused_events: list[dict[str, object]] = []
        for model_id in models:
            model_events = [event for event in events if event["model_id"] == model_id]
            assert len(model_events) == 5
            assert model_events[0]["lane_reason"] == "cold_start"
            for event in model_events[1:]:
                assert event["lane_append_only"] is True
                assert event["lane_reason"] == "prefix_extended"
                assert int(event["lane_reusable_tokens"]) > 0
            reused_events.extend(model_events[1:])

        warm_hits = sum(int(event["cache_hit_tokens"]) > 0 for event in reused_events)
        warm_hit_tokens = sum(int(event["cache_hit_tokens"]) for event in reused_events)
        warm_miss_tokens = sum(
            int(event["cache_miss_tokens"]) for event in reused_events
        )
        warm_total_tokens = warm_hit_tokens + warm_miss_tokens
        warm_hit_rate = (
            warm_hit_tokens / warm_total_tokens if warm_total_tokens else 0.0
        )

        # DeepSeek caching is provider-managed and therefore best effort. This
        # threshold proves repeated cross-model routing is materially reusing
        # prefixes without making one transient cache miss fail the benchmark.
        assert warm_hits >= 6
        assert warm_hit_rate >= 0.5

        print(
            {
                "calls": [
                    {
                        "turn": event["turn"],
                        "model": event["model_id"],
                        "hit": event["cache_hit_tokens"],
                        "miss": event["cache_miss_tokens"],
                        "lane_reason": event["lane_reason"],
                    }
                    for event in events
                ],
                "active_lanes": len(lanes),
                "archived_lanes": len(archived),
                "requests_per_lane": sorted(lane["request_count"] for lane in lanes),
                "warm_calls_with_hits": f"{warm_hits}/8",
                "warm_hit_rate": round(warm_hit_rate, 4),
            }
        )
    finally:
        tracker.close()
