"""Local hooks for Xenon."""

from xenon.hooks.runner import (
    HookOutcome,
    HookRunner,
    get_default_runner,
    set_default_runner,
)

__all__ = [
    "HookOutcome",
    "HookRunner",
    "get_default_runner",
    "set_default_runner",
]
