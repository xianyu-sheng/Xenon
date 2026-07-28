"""Trusted tool runtime configuration shared by coding engines."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xenon.engine.execution_policy import walk_engine_graph


@dataclass(frozen=True, slots=True)
class ToolRuntime:
    """Bind file tools to a host worktree and commands to a trusted backend."""

    workspace_root: Path
    command_prefix: tuple[str, ...] = ()
    backend_workdir: str | None = None
    command_prelude: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_root", self.workspace_root.resolve())
        if not self.workspace_root.is_dir():
            raise ValueError(f"workspace_root is not a directory: {self.workspace_root}")
        if any(not isinstance(part, str) or not part for part in self.command_prefix):
            raise ValueError("command_prefix must contain non-empty strings")

    def translate_path(self, value: Any) -> Any:
        """Map an absolute backend path (for example /testbed/a.py) to host."""

        if not isinstance(value, str) or not self.backend_workdir:
            return value
        backend = Path(self.backend_workdir)
        candidate = Path(value)
        if not candidate.is_absolute():
            return value
        try:
            relative = candidate.relative_to(backend)
        except ValueError:
            return value
        return str(self.workspace_root / relative)


def bind_tool_runtime(engine: Any, runtime: ToolRuntime) -> None:
    """Bind one runtime to all current tool executors in an engine graph."""

    for node in walk_engine_graph(engine):
        node.tool_runtime = runtime
        executor = getattr(node, "_tool_executor", None)
        if executor is not None:
            executor.set_runtime(runtime)
