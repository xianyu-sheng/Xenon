"""The one extension bootstrap seam shared by every entry point."""

from __future__ import annotations

import pytest

from xenon.repl.repl import REPL


def test_bootstrap_loads_commands_and_mcp_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        REPL, "_load_custom_commands", lambda self: calls.append("commands")
    )
    monkeypatch.setattr(
        REPL, "_preload_mcp_server_configs", lambda self: calls.append("mcp")
    )

    REPL.bootstrap_extensions(object.__new__(REPL))

    assert calls == ["commands", "mcp"]
