"""命令注册完整性：自动发现兜底（R6）。"""

from __future__ import annotations

import xenon.repl.commands  # noqa: F401 — 导入即触发全部命令组注册
from xenon.repl.command_registry import COMMANDS


def test_all_expected_commands_registered():
    for name in (
        "/rewind",
        "/fork",
        "/review",
        "/plan",
        "/goal",
        "/permissions",
        "/skill",
        "/memory",
        "/sessions",
        "/help",
        "/exit",
    ):
        assert name in COMMANDS, f"{name} 未注册"


def test_discovery_imports_all_group_modules():
    """command_groups 包内每个模块都定义/注册命令；发现机制保证全部导入。"""

    import pkgutil
    import sys

    import xenon.repl.command_groups as pkg

    prefix = pkg.__name__ + "."
    for info in pkgutil.iter_modules(pkg.__path__):
        if info.ispkg:
            continue
        full = prefix + info.name
        assert full in sys.modules, f"{full} 未被导入（自动发现失效）"
