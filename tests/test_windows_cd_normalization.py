"""Windows cmd-style cd must work in the PowerShell command host."""

from __future__ import annotations

from xenon.nodes.tool_node import normalize_windows_cd


def test_cd_switch_is_removed() -> None:
    assert normalize_windows_cd("cd /d D:/repo; python x.py") == "cd D:/repo; python x.py"


def test_other_cd_text_is_untouched() -> None:
    assert normalize_windows_cd("echo /d; cd repo") == "echo /d; cd repo"
