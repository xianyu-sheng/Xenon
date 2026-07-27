"""Conservative cache-aware routing and settings tests."""

from __future__ import annotations

import json

from xenon.repl.auto_router import AutoRouter
from xenon.repl.difficulty_estimator import TaskProfile
from xenon.repl.model_pool import ModelPool
from xenon.repl.context_manager import ContextManager
from xenon.utils.cache_telemetry import (
    MANIFEST_RESPONSE_KEY,
    CacheEventStore,
    build_prompt_manifest,
)
from xenon.utils.deepseek_cache import CacheTracker


class _FixedEstimator:
    def __init__(self, profile: TaskProfile) -> None:
        self.profile = profile

    def estimate(self, user_input, context_messages=None):
        return self.profile


def _record_warm(tracker: CacheTracker, model_id: str) -> None:
    manifest = build_prompt_manifest(
        model_id,
        [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "current"},
        ],
        cache_context={"engine": "direct", "phase": "chat"},
    ).as_dict()
    tracker.record_response(model_id, {
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "prompt_cache_hit_tokens": 90,
            "prompt_cache_miss_tokens": 10,
        },
        MANIFEST_RESPONSE_KEY: manifest,
    })


def _peer_pool(*, leader_weight: float = 1.05) -> ModelPool:
    pool = ModelPool()
    common = {
        "tier": 3,
        "reasoning_score": 0.5,
        "coding_score": 0.5,
        "tool_use_score": 0.5,
        "cost_efficiency": 0.5,
    }
    pool.register("provider/leader", alias="leader", weight=leader_weight, **common)
    pool.register("provider/warm", alias="warm", weight=1.0, **common)
    return pool


def _router(pool: ModelPool, tracker: CacheTracker) -> AutoRouter:
    return AutoRouter(
        pool,
        estimator=_FixedEstimator(TaskProfile(complexity=0.5)),
        cache_tracker=tracker,
    )


def test_recent_provider_hit_breaks_only_a_near_tie() -> None:
    tracker = CacheTracker()
    _record_warm(tracker, "provider/warm")
    router = _router(_peer_pool(), tracker)

    result = router.route("same capability task")

    assert result[:2] == ["provider/warm", "provider/leader"]
    assert router.last_cache_affinity_decision["applied"] is True
    assert router.last_cache_affinity_decision["reason"] == "warm_equivalent_peer"
    tracker.close()


def test_warm_model_cannot_overcome_base_score_gap() -> None:
    tracker = CacheTracker()
    _record_warm(tracker, "provider/warm")
    router = _router(_peer_pool(leader_weight=1.5), tracker)

    result = router.route("quality must win")

    assert result[0] == "provider/leader"
    assert router.last_cache_affinity_decision["applied"] is False
    tracker.close()


def test_warm_lower_tier_model_never_outranks_leader() -> None:
    tracker = CacheTracker()
    _record_warm(tracker, "provider/lower")
    pool = ModelPool()
    common = {
        "reasoning_score": 0.5,
        "coding_score": 0.5,
        "tool_use_score": 0.5,
        "cost_efficiency": 0.5,
    }
    pool.register("provider/leader", alias="leader", tier=3, **common)
    pool.register("provider/lower", alias="lower", tier=2, **common)
    router = _router(pool, tracker)

    result = router.route("capability boundary")

    assert result[0] == "provider/leader"
    assert router.last_cache_affinity_decision["applied"] is False
    tracker.close()


def test_disabled_affinity_preserves_base_order() -> None:
    tracker = CacheTracker()
    _record_warm(tracker, "provider/warm")
    tracker.set_cache_affinity_enabled(False, persist=False)
    router = _router(_peer_pool(), tracker)

    assert router.route("disabled")[:2] == ["provider/leader", "provider/warm"]
    assert router.last_cache_affinity_decision["reason"] == "disabled"
    tracker.close()


def test_stale_provider_hit_cannot_influence_routing() -> None:
    tracker = CacheTracker()
    _record_warm(tracker, "provider/warm")
    tracker.AFFINITY_MAX_AGE_SECONDS = -1
    router = _router(_peer_pool(), tracker)

    assert router.route("stale evidence")[:2] == ["provider/leader", "provider/warm"]
    warm_evidence = router.last_cache_affinity_decision["evidence"]["provider/warm"]
    assert warm_evidence["eligible"] is False
    assert warm_evidence["reason"] == "stale_provider_evidence"
    tracker.close()


def test_explicit_model_preference_remains_above_cache_affinity() -> None:
    tracker = CacheTracker()
    _record_warm(tracker, "provider/warm")
    router = _router(_peer_pool(), tracker)

    result = router.route(
        "explicit preference",
        preferred_models=["provider/leader"],
    )

    assert result[0] == "provider/leader"
    tracker.close()


def test_explicit_model_outside_selected_fallbacks_is_not_discarded() -> None:
    tracker = CacheTracker()
    pool = _peer_pool()
    pool.register(
        "provider/explicit",
        alias="explicit",
        weight=0.1,
        tier=3,
        reasoning_score=0.5,
        coding_score=0.5,
        tool_use_score=0.5,
        cost_efficiency=0.5,
    )
    router = _router(pool, tracker)

    result = router.route(
        "explicit model outside top two",
        count=2,
        preferred_models=["provider/explicit"],
    )

    assert result[0] == "provider/explicit"
    assert len(result) == 2
    tracker.close()


def test_affinity_setting_is_private_persistent_and_reversible(tmp_path) -> None:
    store = CacheEventStore(tmp_path)
    tracker = CacheTracker(event_store=store)
    assert tracker.cache_affinity_enabled is True
    assert tracker.set_cache_affinity_enabled(False) is True
    settings_path = tracker.cache_settings_path
    tracker.close()

    assert settings_path is not None
    assert settings_path.stat().st_mode & 0o777 == 0o600
    assert json.loads(settings_path.read_text(encoding="utf-8")) == {
        "cache_affinity_enabled": False,
        "schema_version": 2,
    }

    restored = CacheTracker(event_store=CacheEventStore(tmp_path))
    assert restored.cache_affinity_enabled is False
    restored.set_cache_affinity_enabled(True)
    restored.close()

    enabled_again = CacheTracker(event_store=CacheEventStore(tmp_path))
    assert enabled_again.cache_affinity_enabled is True
    enabled_again.close()


def test_matching_local_prompt_lane_breaks_near_tie_without_claiming_provider_hit() -> None:
    tracker = CacheTracker()
    context = ContextManager()
    context.prompt_lanes.prepare(
        "provider/warm",
        "direct",
        "chat",
        0,
        [
            {"role": "system", "content": "stable" * 300},
            {"role": "user", "content": "current"},
        ],
    )
    router = AutoRouter(
        _peer_pool(),
        estimator=_FixedEstimator(TaskProfile(complexity=0.5)),
        cache_tracker=tracker,
        context_manager=context,
    )

    result = router.route(
        "same contract",
        cache_engine="direct",
        cache_phase="chat",
    )

    assert result[:2] == ["provider/warm", "provider/leader"]
    evidence = router.last_cache_affinity_decision["evidence"]["provider/warm"]
    assert evidence["reason"] == "warm_prompt_lane"
    assert evidence["evidence"] == "local_exact_prefix"
    assert tracker.cache_hits == 0
    tracker.close()
