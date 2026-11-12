"""Config layering: project overrides user; environment overrides both."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from xenon.repl import system_config
from xenon.utils.project_root import find_project_root


@pytest.fixture(autouse=True)
def _reset_cache():
    system_config._file_cache = None
    system_config._file_cache_key = None
    yield
    system_config._file_cache = None
    system_config._file_cache_key = None


def _layers(monkeypatch, tmp_path: Path) -> tuple[Path, Path, Path]:
    user = tmp_path / "user" / "config.yaml"
    user.parent.mkdir()
    project = tmp_path / "project"
    (project / ".xenon").mkdir(parents=True)
    (project / "pyproject.toml").write_text("", encoding="utf-8")
    project_config = project / ".xenon" / "config.yaml"
    user.write_text(
        "validation:\n  strict: false\n"
        "limits:\n  max_models_per_provider: 3\n",
        encoding="utf-8",
    )
    project_config.write_text(
        "validation:\n  strict: true\nlimits:\n  max_undo_snapshots: 7\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(system_config, "_DEFAULT_CONFIG_PATH", user)
    monkeypatch.setattr(system_config, "CONFIG_PATH", user)
    monkeypatch.setattr(
        "xenon.utils.project_root.find_project_root",
        lambda *args, **kwargs: project,
    )
    return user, project_config, project


def test_the_project_layer_overrides_the_user_layer(
    monkeypatch, tmp_path: Path
) -> None:
    _layers(monkeypatch, tmp_path)

    config = system_config.get_config()

    assert config.intent_classifier.enabled is False  # project override applies
    assert config.validation.strict is False  # user-only key survives
    assert config.limits.max_models_per_provider == 3
    assert config.limits.max_undo_snapshots == 7  # project-only key is read


def test_the_environment_still_beats_both_files(monkeypatch, tmp_path: Path) -> None:
    _layers(monkeypatch, tmp_path)
    monkeypatch.setenv("XENON_STRICT_VALIDATION", "0")

    assert system_config.get_config().validation.strict is False


def test_editing_the_project_file_invalidates_the_cache(
    monkeypatch, tmp_path: Path
) -> None:
    _user, project_config, _project = _layers(monkeypatch, tmp_path)
    assert system_config.get_config().limits.max_undo_snapshots == 7

    project_config.write_text(
        "limits:\n  max_undo_snapshots: 9\n", encoding="utf-8"
    )
    stamp = time.time() + 5
    os.utime(project_config, (stamp, stamp))

    assert system_config.get_config().limits.max_undo_snapshots == 9


def test_an_explicit_config_path_disables_the_project_layer(
    monkeypatch, tmp_path: Path
) -> None:
    explicit = tmp_path / "explicit.yaml"
    explicit.write_text("validation:\n  strict: false\n", encoding="utf-8")
    monkeypatch.setattr(system_config, "CONFIG_PATH", explicit)

    config = system_config.get_config()

    assert config.validation.strict is False
    # The project layer was skipped: the strict key would have flipped it.
    assert config.limits.max_undo_snapshots == 10


def test_the_finder_walks_up_to_the_marker(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)

    assert find_project_root(nested) == tmp_path.resolve()


def test_the_finder_does_not_treat_home_as_a_project(
    monkeypatch, tmp_path: Path
) -> None:
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "xenon.utils.project_root.os.path.expanduser",
        lambda _value: str(tmp_path),
    )

    assert find_project_root(tmp_path) is None


def test_the_finder_falls_back_only_when_asked(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()

    assert find_project_root(bare) == bare.resolve()
    assert find_project_root(bare, fallback=False) is None


def test_a_project_cannot_redirect_credentials_or_weaken_validation(
    monkeypatch, tmp_path: Path
) -> None:
    """A cloned repo is not a trust boundary (the hooks rule, applied to config)."""

    _user, project_config, _project = _layers(monkeypatch, tmp_path)
    project_config.write_text(
        "paths:\n  credentials: /tmp/evil-credentials.yaml\n"
        "validation:\n  strict: true\n"
        "limits:\n  max_undo_snapshots: 7\n",
        encoding="utf-8",
    )

    config = system_config.get_config()

    assert config.paths.credentials != "/tmp/evil-credentials.yaml"
    # ``validation`` is protected: the project cannot flip it either way.
    assert config.validation.strict is False
    # Non-sensitive project sections still apply.
    assert config.limits.max_undo_snapshots == 7
