"""P2-1：引擎时限可通过 XENON_ENGINE_TIMEOUT 调整。"""

from __future__ import annotations

from xenon.engine.combined_engines import _deadline_note
from xenon.engine.execution_policy import (
    DEFAULT_ENGINE_TIMEOUT,
    engine_timeout_from_env,
)


def test_default_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("XENON_ENGINE_TIMEOUT", raising=False)
    assert engine_timeout_from_env() == DEFAULT_ENGINE_TIMEOUT


def test_env_override(monkeypatch) -> None:
    monkeypatch.setenv("XENON_ENGINE_TIMEOUT", "1200")
    assert engine_timeout_from_env() == 1200.0


def test_invalid_or_non_positive_falls_back(monkeypatch) -> None:
    for raw in ("abc", "0", "-5"):
        monkeypatch.setenv("XENON_ENGINE_TIMEOUT", raw)
        assert engine_timeout_from_env() == DEFAULT_ENGINE_TIMEOUT


def test_deadline_note_uses_configured_timeout(monkeypatch) -> None:
    monkeypatch.setenv("XENON_ENGINE_TIMEOUT", "1200")
    note = _deadline_note()
    assert "1200" in note
    assert "600" not in note
