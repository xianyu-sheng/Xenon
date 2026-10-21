"""Small cross-platform test helpers (not production code)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest


def symlink_or_skip(
    link: Path,
    target: Path,
    *,
    target_is_directory: bool = False,
) -> None:
    """Create a symlink, skipping tests when Windows lacks the privilege."""
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as exc:
        pytest.skip(f"symlink unavailable on this platform: {exc}")


def assert_posix_mode(path: Path, expected: int) -> None:
    """Assert POSIX mode bits; Windows has no equivalent ACL projection."""
    if os.name == "nt":
        return
    assert stat.S_IMODE(path.stat().st_mode) == expected
