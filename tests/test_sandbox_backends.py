"""Sandbox backends: the process default, and external runtimes behind a seam."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from xenon.sandbox import backends


def _which(monkeypatch, value: str | None) -> None:
    monkeypatch.setattr(
        backends.shutil, "which", lambda _name: value
    )


def test_the_process_backend_is_the_unchanged_host_behaviour(
    tmp_path: Path,
) -> None:
    runtime = backends.build_runtime(tmp_path)

    assert runtime.command_prefix == ()
    assert runtime.command_prelude == ""
    assert runtime.backend_workdir is None


def test_a_missing_docker_binary_fails_closed(tmp_path: Path, monkeypatch) -> None:
    _which(monkeypatch, None)

    with pytest.raises(backends.SandboxUnavailable) as excinfo:
        backends.build_runtime(
            tmp_path, backends.SandboxSpec(backend="docker")
        )

    assert excinfo.value.kind == "sandbox"
    assert excinfo.value.retryable is False


def test_docker_wraps_the_command_and_maps_paths(
    tmp_path: Path, monkeypatch
) -> None:
    _which(monkeypatch, "docker")

    runtime = backends.build_runtime(
        tmp_path, backends.SandboxSpec(backend="docker", image="x:1")
    )

    prefix = runtime.command_prefix
    assert prefix[:3] == ("docker", "run", "--rm")
    assert "--cap-drop" in prefix and "ALL" in prefix
    assert "--network" in prefix and "none" in prefix
    assert f"{tmp_path.resolve()}:/workspace" in prefix
    assert "x:1" in prefix
    assert runtime.backend_workdir == "/workspace"
    # Container paths are translated back to the host before matching.
    assert runtime.translate_path("/workspace/calc.py") == str(
        tmp_path.resolve() / "calc.py"
    )


def test_wsl_runs_in_the_project_directory(tmp_path: Path, monkeypatch) -> None:
    _which(monkeypatch, "wsl")

    runtime = backends.build_runtime(
        tmp_path, backends.SandboxSpec(backend="wsl")
    )

    assert runtime.command_prefix == (
        "wsl.exe",
        "--cd",
        str(tmp_path.resolve()),
        "-e",
    )


def test_an_unknown_backend_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(backends.SandboxUnavailable):
        backends.build_runtime(tmp_path, backends.SandboxSpec(backend="nope"))


def test_the_capability_report_does_not_claim_isolation_for_process() -> None:
    report = backends.describe()

    assert report["backend"] == "process"
    assert report["isolation"] == "none"
    assert "not a security boundary" in report["note"]


def test_the_spec_is_read_from_the_config_section() -> None:
    config = SimpleNamespace(
        backend="DOCKER", image="x:1", network=True, workdir="/work"
    )

    spec = backends.spec_from_config(config)

    assert spec.backend == "docker"
    assert spec.image == "x:1"
    assert spec.network is True
    assert spec.workdir == "/work"
    assert backends.spec_from_config(None).backend == "process"


def test_the_config_layer_reads_the_sandbox_section(
    monkeypatch, tmp_path: Path
) -> None:
    from xenon.repl import system_config

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "sandbox:\n  backend: docker\n  network: true\n  image: x:1\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("XENON_SANDBOX", raising=False)
    monkeypatch.setattr(system_config, "CONFIG_PATH", config_file)
    system_config.reload_config()

    sandbox = system_config.get_config().sandbox
    assert sandbox.backend == "docker"
    assert sandbox.network is True
    assert sandbox.image == "x:1"

    monkeypatch.setenv("XENON_SANDBOX", "wsl")
    assert system_config.get_config().sandbox.backend == "wsl"

    system_config._file_cache = None
    system_config._file_cache_key = None
