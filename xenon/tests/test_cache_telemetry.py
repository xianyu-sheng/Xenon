"""Prompt manifest and per-request cache telemetry tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from xenon.utils.cache_telemetry import (
    MANIFEST_RESPONSE_KEY,
    CacheEventStore,
    build_cache_event,
    build_prompt_manifest,
)
from xenon.utils.deepseek_cache import CacheTracker
from xenon.engine.base import BaseEngine
from xenon.repl.context_manager import ContextManager


def _messages(question: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "stable-secret-system-instruction"},
        {"role": "user", "content": question},
    ]


def test_current_request_changes_do_not_split_cache_family() -> None:
    first = build_prompt_manifest(
        "deepseek/deepseek-v4-flash",
        _messages("question-one-private"),
        cache_context={"engine": "direct", "phase": "chat"},
    )
    second = build_prompt_manifest(
        "deepseek/deepseek-v4-flash",
        _messages("question-two-private"),
        cache_context={"engine": "direct", "phase": "chat"},
    )

    assert first.cache_family == second.cache_family
    assert first.prompt_hash != second.prompt_hash
    assert first.stable_prefix_hash == second.stable_prefix_hash


def test_phase_tool_schema_and_epoch_split_cache_families() -> None:
    common = _messages("same-request")
    base = build_prompt_manifest(
        "deepseek-v4-flash",
        common,
        cache_context={"engine": "react", "phase": "reason_act"},
    )
    reviewed = build_prompt_manifest(
        "deepseek-v4-flash",
        common,
        cache_context={"engine": "react", "phase": "review"},
    )
    with_tool = build_prompt_manifest(
        "deepseek-v4-flash",
        common,
        tools=[{"type": "function", "function": {"name": "read_file"}}],
        cache_context={"engine": "react", "phase": "reason_act"},
    )
    compacted = build_prompt_manifest(
        "deepseek-v4-flash",
        common,
        cache_context={
            "engine": "react",
            "phase": "reason_act",
            "context_epoch": 1,
        },
    )
    different_contract = build_prompt_manifest(
        "deepseek-v4-flash",
        common,
        tools=[{"type": "function", "function": {"name": "read_file"}}],
        request_shape={"tool_choice": "required"},
        cache_context={"engine": "react", "phase": "reason_act"},
    )

    assert len({
        base.cache_family,
        reviewed.cache_family,
        with_tool.cache_family,
        compacted.cache_family,
        different_contract.cache_family,
    }) == 5


def test_manifest_never_contains_raw_prompt_or_tool_text() -> None:
    manifest = build_prompt_manifest(
        "deepseek-v4-flash",
        _messages("highly-private-user-prompt"),
        tools=[{"name": "private-tool-name"}],
    )
    serialized = json.dumps(manifest.as_dict())

    assert "highly-private-user-prompt" not in serialized
    assert "stable-secret-system-instruction" not in serialized
    assert "private-tool-name" not in serialized


@pytest.mark.parametrize(
    ("fields_present", "family_call", "hit", "state", "cause"),
    [
        (False, 1, 0, "unavailable", "cache_fields_unavailable"),
        (True, 1, 0, "cold", "cold_family"),
        (True, 2, 0, "warming", "warming"),
        (True, 3, 0, "miss", "provider_best_effort_miss"),
        (True, 2, 800, "warm", "cache_hit"),
    ],
)
def test_event_state_classification(
    fields_present: bool,
    family_call: int,
    hit: int,
    state: str,
    cause: str,
) -> None:
    manifest = build_prompt_manifest("deepseek-v4-flash", _messages("q")).as_dict()
    event = build_cache_event(
        manifest,
        model_id="deepseek-v4-flash",
        prompt_tokens=1000,
        completion_tokens=20,
        cache_hit_tokens=hit,
        cache_miss_tokens=1000 - hit if fields_present else 0,
        cache_fields_present=fields_present,
        family_call=family_call,
    )
    assert event.state == state
    assert event.cause == cause


def test_event_store_is_bounded_and_contains_no_prompts(tmp_path) -> None:
    store = CacheEventStore(tmp_path, max_events=10)
    manifest = build_prompt_manifest(
        "deepseek-v4-flash",
        _messages("do-not-persist-this"),
    ).as_dict()
    for family_call in range(1, 16):
        store.append(build_cache_event(
            manifest,
            model_id="deepseek-v4-flash",
            prompt_tokens=100,
            completion_tokens=10,
            cache_hit_tokens=80,
            cache_miss_tokens=20,
            cache_fields_present=True,
            family_call=family_call,
        ))

    assert len(store.load()) == 10
    assert "do-not-persist-this" not in store.path.read_text(encoding="utf-8")
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_persistent_hmac_keeps_family_stable_across_processes(tmp_path) -> None:
    script = """
from xenon.utils.cache_telemetry import configure_persistent_secret, build_prompt_manifest
from pathlib import Path
import sys
configure_persistent_secret(Path(sys.argv[1]))
manifest = build_prompt_manifest(
    "deepseek-v4-flash",
    [{"role": "system", "content": "stable"}, {"role": "user", "content": "q"}],
)
print(manifest.cache_family)
"""
    environment = dict(os.environ)
    first = subprocess.check_output(
        [sys.executable, "-c", script, str(tmp_path)],
        text=True,
        env=environment,
    ).strip()
    second = subprocess.check_output(
        [sys.executable, "-c", script, str(tmp_path)],
        text=True,
        env=environment,
    ).strip()

    assert first == second
    assert (tmp_path / "telemetry.key").stat().st_mode & 0o777 == 0o600


def test_tracker_distinguishes_missing_fields_from_explicit_zero() -> None:
    tracker = CacheTracker()
    manifest = build_prompt_manifest("deepseek-v4-flash", _messages("q")).as_dict()

    tracker.record_response("deepseek-v4-flash", {
        "usage": {"prompt_tokens": 100, "completion_tokens": 5},
        MANIFEST_RESPONSE_KEY: manifest,
    })
    tracker.record_response("deepseek-v4-flash", {
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 100,
        },
        MANIFEST_RESPONSE_KEY: manifest,
    })

    events = tracker.recent_events()
    assert events[0]["state"] == "unavailable"
    assert events[1]["state"] == "warming"
    snapshot = tracker.model_snapshot("deepseek-v4-flash")
    assert snapshot["cache_reported_calls"] == 1
    assert snapshot["cache_field_coverage"] == pytest.approx(0.5)
    tracker.close()


class _ConcreteEngine(BaseEngine):
    def run(self, user_input: str, context=None, ctx_mgr=None) -> str:
        return user_input


def test_engine_phase_reaches_llm_manifest_context(monkeypatch) -> None:
    captured: dict = {}

    def fake_chat(model_id, messages, **kwargs):
        captured.update(kwargs["cache_context"])
        return "ok"

    monkeypatch.setattr("xenon.engine.base.chat_completion", fake_chat)
    engine = _ConcreteEngine(["deepseek/deepseek-v4-flash"])
    manager = ContextManager()
    manager.cache_epoch = 3
    engine._ctx_mgr = manager

    assert engine._call_llm_for_phase("review", _messages("q")) == "ok"
    assert captured == {
        "engine": "_concrete",
        "phase": "review",
        "context_epoch": 3,
        "event_cursor": 0,
    }


def test_context_epoch_changes_only_on_structural_rewrites() -> None:
    manager = ContextManager(compact_threshold=0.0)
    manager.add_user_message("first")
    manager.add_assistant_message("answer")
    assert manager.cache_epoch == 0

    manager.clear()
    assert manager.cache_epoch == 1
    assert manager.undo() is True
    assert manager.cache_epoch == 2
