"""Trusted tool runtime configuration shared by coding engines."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
            raise ValueError(
                f"workspace_root is not a directory: {self.workspace_root}"
            )
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


# ``bind_tool_runtime`` walked the legacy engine graph to attach a runtime to
# every node's executor.  The kernel binds one runtime per catalog gateway, so
# the graph walk has no caller and went with the engines (#202).
