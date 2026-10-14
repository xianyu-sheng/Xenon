"""Tests for learning paradigm preferences from manual overrides (issue #85).

All offline and deterministic: the feature extractor and selector are stubbed.
"""

from __future__ import annotations

import os
import stat
import threading
from types import SimpleNamespace

import pytest

from xenon.repl.command_groups.model import _cmd_mode
from xenon.repl.feature_extractor import TaskFeatures
from xenon.repl.intelligent_router import IntelligentRouter
from xenon.repl.paradigm_selector import RoutingDecision
from xenon.repl.routing_history import (
    OverrideRecord,
    ParadigmPreferences,
)


def _features(**overrides) -> TaskFeatures:
    base = dict(
        is_query=False,
        is_coding=True,
        is_exploratory=False,
        input_length=20,
        estimated_steps=3,
        estimated_tool_calls=2,
        complexity_score=0.5,
        has_file_operations=True,
        has_git_operations=False,
        needs_web_access=False,
        quality_critical=False,
    )
    base.update(overrides)
    return TaskFeatures(**base)


class _StubExtractor:
    def extract(self, user_input, context):
        return _features()


class _StubSelector:
    def __init__(self, paradigm: str, confidence: float):
        self._decision = RoutingDecision(
            paradigm=paradigm, confidence=confidence, reason="stub", matched_rule="stub"
        )

    def select(self, features):
        return self._decision


def _router(paradigm: str, confidence: float, **kwargs) -> IntelligentRouter:
    router = IntelligentRouter(enabled=True, confidence_threshold=0.6, **kwargs)
    router.extractor = _StubExtractor()
    router.selector = _StubSelector(paradigm, confidence)
    return router


# ── 1. recording ────────────────────────────────────────────


def test_override_is_recorded_with_recommendation_and_preview():
    router = _router("react", 0.9)
    router.route("fix the bug in parser.py", None, "direct")
    router.record_override("plan-execute")

    (rec,) = router.preferences.all()
    assert rec.recommended == "react"
    assert rec.chosen == "plan-execute"
    assert rec.user_input_preview == "fix the bug in parser.py"
    assert rec.timestamp > 0


def test_choosing_the_recommended_mode_is_not_an_override():
    router = _router("react", 0.9)
    router.route("fix the bug", None, "direct")
    router.record_override("react")
    assert router.preferences.all() == []


def test_only_the_first_mode_switch_after_a_recommendation_is_recorded():
    router = _router("react", 0.9)
    router.route("fix the bug", None, "direct")
    router.record_override("plan-execute")
    router.record_override("reflection")  # pending recommendation already consumed

    assert [r.chosen for r in router.preferences.all()] == ["plan-execute"]


def test_override_without_recommendation_is_ignored():
    router = _router("react", 0.9)
    router.record_override("plan-execute")
    assert router.preferences.all() == []


def test_mode_command_records_override():
    router = _router("react", 0.9)
    router.route("fix the bug", None, "direct")
    mode = SimpleNamespace(name="plan-execute", description="d")
    registry = SimpleNamespace(set_mode=lambda name: mode)

    out = _cmd_mode(
        args="plan-execute",
        registry=registry,
        session_state={"intelligent_router": router},
    )

    assert out == "✅ 已切换到范式: plan-execute — d"  # user-visible text unchanged
    assert [r.chosen for r in router.preferences.all()] == ["plan-execute"]


# ── 2. persistence & failure handling ───────────────────────


def test_preferences_persist_and_reload(tmp_path):
    path = tmp_path / "routing_preferences.json"
    prefs = ParadigmPreferences(persist_path=str(path))
    prefs.record(OverrideRecord(1.0, "x", "sig", "react", "plan-execute"))

    reloaded = ParadigmPreferences(persist_path=str(path))
    assert [r.chosen for r in reloaded.all()] == ["plan-execute"]


def test_corrupt_file_starts_empty_without_crashing(tmp_path):
    path = tmp_path / "routing_preferences.json"
    path.write_text("{{ not json", encoding="utf-8")
    prefs = ParadigmPreferences(persist_path=str(path))
    assert prefs.all() == []


def test_unwritable_path_never_raises(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("i am a file, not a directory")
    prefs = ParadigmPreferences(persist_path=str(blocker / "sub" / "prefs.json"))
    prefs.record(OverrideRecord(1.0, "x", "sig", "react", "plan-execute"))  # no raise
    assert len(prefs.all()) == 1  # still works in memory


# ── 3. weighting ────────────────────────────────────────────


def test_boost_applies_after_enough_overrides_and_is_clamped():
    prefs = ParadigmPreferences()
    for _ in range(ParadigmPreferences.MIN_SAMPLES):
        prefs.record(OverrideRecord(1.0, "x", "sig", "react", "plan-execute"))

    assert prefs.weight("sig", "plan-execute", 0.5) == pytest.approx(0.6)
    assert prefs.weight("sig", "plan-execute", 0.95) == 1.0  # clamped
    assert prefs.weight("sig", "react", 0.5) == 0.5  # not the preferred paradigm
    assert prefs.weight("other-sig", "plan-execute", 0.5) == 0.5  # different task


def test_no_boost_below_minimum_samples():
    prefs = ParadigmPreferences()
    prefs.record(OverrideRecord(1.0, "x", "sig", "react", "plan-execute"))
    assert prefs.weight("sig", "plan-execute", 0.5) == 0.5


def test_boost_can_lift_confidence_over_threshold():
    # 0.55 < threshold 0.6, so without learning there is no switch.
    router = _router("react", 0.55)
    assert router.route("fix the bug", None, "direct") is None

    for _ in range(ParadigmPreferences.MIN_SAMPLES):
        router.preferences.record(
            OverrideRecord(
                1.0, "x", IntelligentRouter._signature(_features()), "plan-execute", "react"
            )
        )
    decision = router.route("fix the bug", None, "direct")
    assert decision is not None
    assert decision.paradigm == "react"
    assert decision.confidence == pytest.approx(0.65)


def test_boost_does_not_bypass_switch_only_on_improvement():
    # reflection(3) -> direct(1) is a 2-level downgrade: must stay blocked.
    router = _router("direct", 0.55)
    for _ in range(3):
        router.preferences.record(
            OverrideRecord(
                1.0, "x", IntelligentRouter._signature(_features()), "react", "direct"
            )
        )
    # boosted confidence 0.65 >= threshold, but Step 5 must still veto.
    assert router.route("hello", None, "reflection") is None


def test_boost_never_exceeds_one_through_route():
    router = _router("react", 0.98)
    for _ in range(3):
        router.preferences.record(
            OverrideRecord(
                1.0, "x", IntelligentRouter._signature(_features()), "direct", "react"
            )
        )
    decision = router.route("fix the bug", None, "direct")
    assert decision is not None
    assert decision.confidence == 1.0


# ── 4. failure isolation & bounds ───────────────────────────


def test_weighting_failure_returns_original_decision():
    router = _router("react", 0.9)

    def boom(*args, **kwargs):
        raise RuntimeError("preferences exploded")

    router.preferences.weight = boom
    decision = router.route("fix the bug", None, "direct")

    assert decision is not None  # routing still works
    assert decision.paradigm == "react"
    assert decision.confidence == pytest.approx(0.9)  # unboosted


def test_record_override_swallows_storage_errors():
    router = _router("react", 0.9)
    router.route("fix the bug", None, "direct")

    def boom(*args, **kwargs):
        raise OSError("disk on fire")

    router.preferences.record = boom
    router.record_override("plan-execute")  # must not raise


def test_preferences_are_bounded_and_drop_the_oldest():
    prefs = ParadigmPreferences(maxsize=2)
    for chosen in ("a", "b", "c"):
        prefs.record(OverrideRecord(1.0, "x", "sig", "react", chosen))
    assert [r.chosen for r in prefs.all()] == ["b", "c"]


def test_concurrent_records_stay_bounded_and_consistent():
    prefs = ParadigmPreferences(maxsize=100)

    def worker(tag: str) -> None:
        for _ in range(50):
            prefs.record(OverrideRecord(1.0, tag, "sig", "react", "plan-execute"))

    threads = [threading.Thread(target=worker, args=(f"t{n}",)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    records = prefs.all()
    assert len(records) == 100  # 200 appends, capped at maxsize
    assert all(r.chosen == "plan-execute" for r in records)


# ── 5. signature semantics ───────────────────────────────────


def test_signature_buckets_complexity_and_caps_at_two():
    assert "c0" in IntelligentRouter._signature(_features(complexity_score=0.0))
    assert "c1" in IntelligentRouter._signature(_features(complexity_score=0.5))
    assert "c2" in IntelligentRouter._signature(_features(complexity_score=1.0))
    # Values above 1.0 stay in the top bucket instead of overflowing it.
    assert "c2" in IntelligentRouter._signature(_features(complexity_score=5.0))


def test_signature_separates_task_classes():
    coding = IntelligentRouter._signature(
        _features(is_coding=True, is_query=False, is_exploratory=False)
    )
    query = IntelligentRouter._signature(
        _features(is_coding=False, is_query=True, is_exploratory=False)
    )
    explore = IntelligentRouter._signature(
        _features(is_coding=False, is_query=False, is_exploratory=True)
    )
    assert len({coding, query, explore}) == 3


def test_signature_is_deterministic_and_flag_sensitive():
    base = IntelligentRouter._signature(_features())
    assert base == IntelligentRouter._signature(_features())  # deterministic
    assert IntelligentRouter._signature(_features(quality_critical=True)) != base
    assert IntelligentRouter._signature(_features(has_file_operations=False)) != base


def test_boost_is_scoped_to_the_exact_signature_bucket():
    # Learned under complexity 0.1 (bucket c0) ...
    router = _router("react", 0.55)
    low = IntelligentRouter._signature(_features(complexity_score=0.1))
    for _ in range(ParadigmPreferences.MIN_SAMPLES):
        router.preferences.record(
            OverrideRecord(1.0, "x", low, "plan-execute", "react")
        )
    # ... but the live route sees complexity 0.5 (bucket c1): no boost, no switch.
    assert router.route("fix the bug", None, "direct") is None


def test_decision_history_records_the_boosted_confidence():
    router = _router("react", 0.55)
    for _ in range(ParadigmPreferences.MIN_SAMPLES):
        router.preferences.record(
            OverrideRecord(
                1.0,
                "x",
                IntelligentRouter._signature(_features()),
                "plan-execute",
                "react",
            )
        )

    decision = router.route("fix the bug", None, "direct")
    assert decision is not None
    assert decision.confidence == pytest.approx(0.65)
    latest = router.decision_history.all()[-1]
    assert latest.recommended_paradigm == "react"
    assert latest.confidence == pytest.approx(0.65)  # the value actually used
    assert latest.switched is True


# ── 6. file contract, wiring and integration ─────────────────


def test_override_preview_is_truncated_to_120_chars():
    router = _router("react", 0.9)
    router.route("y" * 500, None, "direct")
    router.record_override("plan-execute")
    (record,) = router.preferences.all()
    assert record.user_input_preview == "y" * 120


@pytest.mark.parametrize(
    "payload",
    ['{"not": "a list"}', "null", "42", '[{"missing": "fields"}]'],
)
def test_wrong_shape_preferences_file_starts_empty(tmp_path, payload):
    path = tmp_path / "routing_preferences.json"
    path.write_text(payload, encoding="utf-8")
    prefs = ParadigmPreferences(persist_path=str(path))
    assert prefs.all() == []


def test_reload_keeps_only_the_newest_maxsize_records(tmp_path):
    path = tmp_path / "routing_preferences.json"
    writer = ParadigmPreferences(persist_path=str(path))
    for index in range(5):
        writer.record(OverrideRecord(float(index), "x", "sig", "react", f"m{index}"))

    reloaded = ParadigmPreferences(maxsize=2, persist_path=str(path))
    assert [r.chosen for r in reloaded.all()] == ["m3", "m4"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_preferences_file_is_private(tmp_path):
    path = tmp_path / "routing_preferences.json"
    prefs = ParadigmPreferences(persist_path=str(path))
    prefs.record(OverrideRecord(1.0, "x", "sig", "react", "plan-execute"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_mode_command_survives_preference_storage_failure():
    router = _router("react", 0.9)
    router.route("fix the bug", None, "direct")

    def boom(*args, **kwargs):
        raise OSError("disk on fire")

    router.preferences.record = boom
    mode = SimpleNamespace(name="plan-execute", description="d")
    registry = SimpleNamespace(set_mode=lambda name: mode)

    out = _cmd_mode(
        args="plan-execute",
        registry=registry,
        session_state={"intelligent_router": router},
    )

    assert out == "✅ 已切换到范式: plan-execute — d"


def test_repl_wires_preferences_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    from xenon.repl.repl import REPL

    repl = REPL(streaming=False)
    persist = repl.intelligent_router.preferences._persist_path
    assert persist is not None
    assert persist.name == "routing_preferences.json"
