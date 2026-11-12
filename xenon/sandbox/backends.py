"""Where a model-issued command runs.

The default ``process`` backend executes on the host with only the in-process
fences (path validation, command policy).  That is **not** a security boundary
and must not be described as one.  The other backends wrap the command in an
external runtime (Docker / WSL); they exist so an operator can choose a real
isolation boundary without Xenon pretending to provide one itself.

The seam is already built: ``ToolRuntime.command_prefix / command_prelude /
backend_workdir`` is consumed by ``ToolNode.bind_command_runtime`` and by the
git/test helpers.  A backend only produces that configuration.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xenon.engine.tool_runtime import ToolRuntime

DEFAULT_IMAGE = "debian:stable-slim"
DEFAULT_CONTAINER_WORKDIR = "/workspace"


class SandboxUnavailable(RuntimeError):
    """The configured backend cannot run here; execution must fail closed."""

    kind = "sandbox"
    retryable = False


@dataclass(frozen=True, slots=True)
class SandboxSpec:
    backend: str = "process"
    image: str = DEFAULT_IMAGE
    network: bool = False
    workdir: str = DEFAULT_CONTAINER_WORKDIR

    def normalized(self) -> "SandboxSpec":
        return SandboxSpec(
            backend=(self.backend or "process").strip().lower(),
            image=(self.image or DEFAULT_IMAGE).strip(),
            network=bool(self.network),
            workdir=(self.workdir or DEFAULT_CONTAINER_WORKDIR).strip(),
        )


def spec_from_config(config: Any) -> SandboxSpec:
    """Map ``SystemConfig.sandbox`` onto a spec; missing config means process."""

    if config is None:
        return SandboxSpec()
    return SandboxSpec(
        backend=str(getattr(config, "backend", "process") or "process"),
        image=str(getattr(config, "image", DEFAULT_IMAGE) or DEFAULT_IMAGE),
        network=bool(getattr(config, "network", False)),
        workdir=str(
            getattr(config, "workdir", DEFAULT_CONTAINER_WORKDIR)
            or DEFAULT_CONTAINER_WORKDIR
        ),
    ).normalized()


def spec_from_env() -> SandboxSpec:
    """The headless entry has no config object; the env is its only input."""

    return SandboxSpec(
        backend=os.environ.get("XENON_SANDBOX", "process"),
        image=os.environ.get("XENON_SANDBOX_IMAGE", DEFAULT_IMAGE),
        network=os.environ.get("XENON_SANDBOX_NETWORK", "").strip().lower()
        in {"1", "true", "yes", "on"},
        workdir=os.environ.get(
            "XENON_SANDBOX_WORKDIR", DEFAULT_CONTAINER_WORKDIR
        ),
    ).normalized()


def _docker_runtime(workspace: Path, spec: SandboxSpec) -> ToolRuntime:
    if shutil.which("docker") is None:
        raise SandboxUnavailable(
            "sandbox backend 'docker' is configured but docker is not on PATH"
        )
    prefix = [
        "docker",
        "run",
        "--rm",
        "-i",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "-v",
        f"{workspace}:{spec.workdir}",
        "-w",
        spec.workdir,
    ]
    if not spec.network:
        prefix += ["--network", "none"]
    prefix.append(spec.image)
    return ToolRuntime(
        workspace_root=workspace,
        command_prefix=tuple(prefix),
        backend_workdir=spec.workdir,
    )


def _wsl_runtime(workspace: Path, _spec: SandboxSpec) -> ToolRuntime:
    if shutil.which("wsl") is None:
        raise SandboxUnavailable(
            "sandbox backend 'wsl' is configured but wsl is not on PATH"
        )
    return ToolRuntime(
        workspace_root=workspace,
        command_prefix=("wsl.exe", "--cd", str(workspace), "-e"),
    )


def build_runtime(
    workspace: str | Path, spec: SandboxSpec | None = None
) -> ToolRuntime:
    """The ToolRuntime for the configured backend (process by default)."""

    root = Path(workspace).resolve()
    chosen = (spec or SandboxSpec()).normalized()
    if chosen.backend == "process":
        return ToolRuntime(workspace_root=root)
    if chosen.backend == "docker":
        return _docker_runtime(root, chosen)
    if chosen.backend == "wsl":
        return _wsl_runtime(root, chosen)
    raise SandboxUnavailable(f"unknown sandbox backend: {chosen.backend!r}")


def describe(spec: SandboxSpec | None = None) -> dict[str, Any]:
    """Honest capability report for the current backend."""

    chosen = (spec or SandboxSpec()).normalized()
    if chosen.backend == "process":
        return {
            "backend": "process",
            "available": True,
            "isolation": "none",
            "note": "in-process fences only; not a security boundary",
            "network": True,
        }
    executable = shutil.which("docker" if chosen.backend == "docker" else "wsl")
    return {
        "backend": chosen.backend,
        "available": executable is not None,
        "isolation": "external",
        "image": chosen.image if chosen.backend == "docker" else "",
        "network": chosen.network,
        "executable": executable or "",
    }


__all__ = [
    "DEFAULT_CONTAINER_WORKDIR",
    "DEFAULT_IMAGE",
    "SandboxSpec",
    "SandboxUnavailable",
    "build_runtime",
    "describe",
    "spec_from_config",
    "spec_from_env",
]
