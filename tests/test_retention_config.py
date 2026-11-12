"""retention 配置键打通：config.yaml 段 → SystemConfig，环境变量优先。

保留策略默认关闭；开启后只影响子账本归档，任务账本永不自动清理。
"""

from __future__ import annotations

import pytest

from xenon.repl import system_config

_ENV_KEYS = ("XENON_RETENTION_ENABLED", "XENON_CHILDREN_DAYS", "XENON_LEDGER_MAX_MB")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    system_config._file_cache = None
    system_config._file_cache_key = None
    yield
    system_config._file_cache = None
    system_config._file_cache_key = None


def _use_config_file(monkeypatch, tmp_path, yaml_text: str) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml_text, encoding="utf-8")
    monkeypatch.setattr(system_config, "CONFIG_PATH", cfg_file)
    system_config.reload_config()


def test_defaults_are_off(monkeypatch, tmp_path) -> None:
    _use_config_file(monkeypatch, tmp_path, "")

    retention = system_config.get_config().retention

    assert retention.enabled is False
    assert retention.children_days == 30
    assert retention.max_total_mb == 0.0


def test_the_file_can_enable_and_tune_it(monkeypatch, tmp_path) -> None:
    _use_config_file(
        monkeypatch,
        tmp_path,
        "retention:\n  enabled: true\n  children_days: 7\n  max_total_mb: 12.5\n",
    )

    retention = system_config.get_config().retention

    assert retention.enabled is True
    assert retention.children_days == 7
    assert retention.max_total_mb == 12.5


def test_env_wins_over_the_file(monkeypatch, tmp_path) -> None:
    _use_config_file(
        monkeypatch,
        tmp_path,
        "retention:\n  enabled: false\n  children_days: 7\n",
    )
    monkeypatch.setenv("XENON_RETENTION_ENABLED", "1")
    monkeypatch.setenv("XENON_CHILDREN_DAYS", "3")

    retention = system_config.get_config().retention

    assert retention.enabled is True
    assert retention.children_days == 3


def test_bad_values_fall_back_instead_of_raising(monkeypatch, tmp_path) -> None:
    _use_config_file(
        monkeypatch,
        tmp_path,
        "retention:\n  enabled: maybe\n  children_days: -3\n  max_total_mb: -1\n",
    )

    retention = system_config.get_config().retention

    assert retention.enabled is False
    assert retention.children_days == 1  # clamped; 0/-N would archive everything
    assert retention.max_total_mb == 0.0
