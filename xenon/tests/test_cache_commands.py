"""User-facing cache status, explanation, history and toolbar tests."""

from __future__ import annotations

import io
from types import SimpleNamespace

from rich.console import Console

from xenon.repl.commands import COMMANDS, _cmd_cache, _cmd_cost
from xenon.repl.context_manager import ContextManager
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.status_bar import StatusBar
from xenon.utils.cache_telemetry import CacheEventStore, MANIFEST_RESPONSE_KEY, build_prompt_manifest
from xenon.utils.deepseek_cache import CacheTracker


def _state(tracker: CacheTracker) -> dict:
    return {"_repl": SimpleNamespace(_cache_tracker=tracker)}


def _response(*, hit=0, miss=100, cache_fields=True, manifest=None) -> dict:
    usage = {"prompt_tokens": 100, "completion_tokens": 5}
    if cache_fields:
        usage.update({
            "prompt_cache_hit_tokens": hit,
            "prompt_cache_miss_tokens": miss,
        })
    response = {"usage": usage}
    if manifest:
        response[MANIFEST_RESPONSE_KEY] = manifest
    return response


def test_cache_command_is_registered_and_cold_is_not_zero() -> None:
    tracker = CacheTracker()
    text = _cmd_cache(args="status", session_state=_state(tracker))

    assert "/cache" in COMMANDS
    assert "COLD" in text
    assert "不是 0%" in text
    tracker.close()


def test_unavailable_cache_fields_display_na_in_cache_and_cost() -> None:
    tracker = CacheTracker()
    tracker.record_response("openai/gpt", _response(cache_fields=False))

    status = _cmd_cache(args="status", session_state=_state(tracker))
    cost = _cmd_cost(args="", session_state=_state(tracker))
    assert "N/A" in status
    assert "n/a" in cost
    assert "不能解释为 0%" in cost
    tracker.close()


def test_explain_uses_actual_hit_evidence() -> None:
    tracker = CacheTracker()
    manifest = build_prompt_manifest(
        "deepseek-v4-flash",
        [{"role": "system", "content": "stable"}, {"role": "user", "content": "q"}],
        cache_context={"engine": "direct", "phase": "chat"},
    ).as_dict()
    tracker.record_response(
        "deepseek-v4-flash",
        _response(hit=80, miss=20, manifest=manifest),
    )

    text = _cmd_cache(args="explain", session_state=_state(tracker))
    assert "厂商确认缓存命中" in text
    assert "hit=80" in text
    assert "direct/chat" in text
    tracker.close()


def test_explain_attributes_model_switch() -> None:
    tracker = CacheTracker()
    tracker.record_response("provider/model-a", _response())
    tracker.record_response("provider/model-b", _response())

    text = _cmd_cache(args="explain", session_state=_state(tracker))
    assert "模型发生切换" in text
    assert "本地 Manifest 差异推断" in text
    tracker.close()


def test_history_and_doctor_use_privacy_safe_persisted_events(tmp_path) -> None:
    tracker = CacheTracker(event_store=CacheEventStore(tmp_path))
    manifest = build_prompt_manifest(
        "deepseek-v4-flash",
        [{"role": "system", "content": "private-system"},
         {"role": "user", "content": "private-question"}],
    ).as_dict()
    tracker.record_response(
        "deepseek-v4-flash",
        _response(hit=80, miss=20, manifest=manifest),
    )

    history = _cmd_cache(args="history 5", session_state=_state(tracker))
    doctor = _cmd_cache(args="doctor", session_state=_state(tracker))
    assert "WARM" in history
    assert "private-system" not in history
    assert "private-question" not in history
    assert "缓存字段" in doctor
    assert "仅保存哈希与计数" in doctor
    tracker.close()


def test_toolbar_shows_cold_na_and_actual_rate() -> None:
    tracker = CacheTracker()
    bar = StatusBar(
        Console(file=io.StringIO()),
        ContextManager(),
        ModelRegistry(),
        cache_tracker=tracker,
    )
    assert "cache cold" in "".join(text for _style, text in bar.get_toolbar_fragments())

    tracker.record_response("openai/gpt", _response(cache_fields=False))
    assert "cache n/a" in "".join(text for _style, text in bar.get_toolbar_fragments())

    tracker.record_response("deepseek-v4-flash", _response(hit=80, miss=20))
    assert "cache 80%" in "".join(text for _style, text in bar.get_toolbar_fragments())
    tracker.close()
