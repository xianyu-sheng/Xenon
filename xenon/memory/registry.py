"""Backend registry and default scope-to-path mapping."""

from __future__ import annotations

import os
from pathlib import Path

from xenon.memory.backend import JsonMarkdownBackend, MemoryBackend
from xenon.memory.models import MemoryScope


class MemoryBackendRegistry:
    """Resolve a logical memory scope without coupling callers to storage."""

    def __init__(
        self,
        project_root: Path | None,
        *,
        user_data_root: Path | None = None,
        user_config_root: Path | None = None,
    ) -> None:
        self.project_root = project_root.resolve() if project_root else None
        data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        user_root = user_data_root or (data_home / "xenon" / "memory")
        self.user_config_root = user_config_root or (config_home / "xenon")
        self._backends: dict[MemoryScope, MemoryBackend] = {
            MemoryScope.USER: JsonMarkdownBackend(user_root, MemoryScope.USER, private=True),
        }
        if self.project_root is not None:
            self._backends.update({
                MemoryScope.PROJECT_LOCAL: JsonMarkdownBackend(
                    self.project_root / ".xenon" / "memory" / "local",
                    MemoryScope.PROJECT_LOCAL,
                    private=True,
                ),
                MemoryScope.PROJECT_SHARED: JsonMarkdownBackend(
                    self.project_root / ".xenon" / "memory" / "shared",
                    MemoryScope.PROJECT_SHARED,
                    private=False,
                ),
            })

    @property
    def has_project(self) -> bool:
        return self.project_root is not None

    def register(self, scope: MemoryScope, backend: MemoryBackend) -> None:
        if scope == MemoryScope.SESSION:
            raise ValueError("session scope is managed in memory, not by a persistent backend")
        self._backends[scope] = backend

    def get(self, scope: MemoryScope) -> MemoryBackend:
        if scope == MemoryScope.SESSION:
            raise KeyError("session scope has no persistent backend")
        if scope in {MemoryScope.PROJECT_LOCAL, MemoryScope.PROJECT_SHARED} and not self.has_project:
            raise ValueError(
                "当前未检测到项目，不能使用项目记忆；请先进入具体项目目录，"
                "或改用 user/session 作用域"
            )
        return self._backends[scope]

    def persistent_scopes(self) -> tuple[MemoryScope, ...]:
        return tuple(self._backends)
