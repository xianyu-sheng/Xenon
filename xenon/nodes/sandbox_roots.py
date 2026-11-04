"""Extra roots the file sandbox may touch in this process (#412).

The fence itself is deliberate: reads are free, writes are confined.  What was
missing is a way to widen the fence *without restarting*.  A dogfood run wanted
to write CI files into a repo one directory over, was refused, and the only
documented remedy was `XENON_EXTRA_ROOTS=` before launching the process — which
a user cannot discover from the refusal, and cannot do mid-session at all.

Roots added here are:

* **process-scoped**: nothing is persisted, so a restart returns to the
  configured fence;
* **user-scoped**: only the REPL calls :func:`add_root`; the model has no
  capability that reaches this module (the catalog has no tool for it).

The kernel keeps its purity rule either way: this file names no tool and is
imported only by the tool runtime.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)

_LOCK = Lock()
_SESSION_ROOTS: list[Path] = []


def _normalise(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def add_root(path: str | Path) -> Path:
    """Allow one more directory for the rest of this process.

    Raises ``ValueError`` when the path is not an existing directory — a root
    that cannot be resolved would sit in the list doing nothing, which is worse
    than saying no.
    """

    candidate = _normalise(path)
    if not candidate.is_dir():
        raise ValueError(f"不是已存在的目录: {candidate}")
    with _LOCK:
        if candidate not in _SESSION_ROOTS:
            _SESSION_ROOTS.append(candidate)
            logger.info("sandbox root added: %s", candidate)
    return candidate


def remove_root(path: str | Path) -> bool:
    """Drop a previously added root; returns whether anything was removed."""

    candidate = _normalise(path)
    with _LOCK:
        if candidate in _SESSION_ROOTS:
            _SESSION_ROOTS.remove(candidate)
            logger.info("sandbox root removed: %s", candidate)
            return True
    return False


def session_roots() -> tuple[Path, ...]:
    with _LOCK:
        return tuple(_SESSION_ROOTS)


def env_roots(raw: str | None = None) -> tuple[Path, ...]:
    """Roots from ``XENON_EXTRA_ROOTS`` (os.pathsep separated)."""

    text = os.environ.get("XENON_EXTRA_ROOTS", "") if raw is None else raw
    roots: list[Path] = []
    for entry in text.split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue
        try:
            roots.append(_normalise(entry))
        except (OSError, RuntimeError):
            logger.warning("XENON_EXTRA_ROOTS 条目无法解析，已忽略: %s", entry)
    return tuple(roots)


def clear_session_roots() -> None:
    """Forget every session root (tests and `/workspace clear`)."""

    with _LOCK:
        _SESSION_ROOTS.clear()
