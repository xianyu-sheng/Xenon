"""Find a project root without reading configuration.

Three callers needed this and two grew their own copy (``project_context`` and
``skill_manager``), each with a different marker list and depth.  A config-free
finder is also required *by* the config loader: ``get_config()`` must know
where the project file lives before any config exists, so this module must not
import ``system_config``.

The walk is bounded and stops before ``$HOME`` (a dotfiles git repo in HOME is
not an implicit project).  The filesystem root is never a project.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Files that mark a project.  A union of what the two old finders used; the
#: value (language) is read by ``project_context`` for its type label.
PROJECT_MARKERS: tuple[str, ...] = (
    ".git",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "Pipfile",
    "package.json",
    "tsconfig.json",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "Gemfile",
    "composer.json",
    "CMakeLists.txt",
)

DEFAULT_MAX_LEVELS = 6


def find_project_root(
    start: str | Path | None = None,
    *,
    max_levels: int = DEFAULT_MAX_LEVELS,
    allow_home: bool = False,
    fallback: bool = True,
) -> Path | None:
    """The nearest directory containing a marker, else ``start`` (or None).

    ``fallback=False`` is for callers that must distinguish "a real project"
    from "just a directory"; ``True`` is for config lookup, where a bare
    ``.xenon/config.yaml`` in the working directory should still apply.
    """

    current = Path(start or Path.cwd()).expanduser()
    if current.is_file():
        current = current.parent
    current = current.resolve()
    home = Path(os.path.expanduser("~")).resolve()
    anchor = Path(current.anchor)

    if not allow_home and current in {home, anchor}:
        return None

    cursor = current
    for _ in range(max_levels):
        if any((cursor / marker).exists() for marker in PROJECT_MARKERS):
            return cursor
        parent = cursor.parent
        if parent == cursor or parent == home:
            break
        cursor = parent

    if not fallback:
        return None
    return current


__all__ = ["DEFAULT_MAX_LEVELS", "PROJECT_MARKERS", "find_project_root"]
