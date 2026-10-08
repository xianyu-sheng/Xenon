"""D3：沙箱 seam（只读模式 / 命令白名单 / 后端检测）。"""

from __future__ import annotations

from xenon.engine.context import AgentContext
from xenon.nodes import tool_executor as te_mod
from xenon.nodes.tool_executor import ToolExecutor
from xenon.sandbox import (
    command_allowlist,
    command_denial_reason,
    detect_sandbox_backends,
)


class _FakeNode:
    script: list = []

    def __init__(self, name, action_type=None, **params):
        self.action_type = action_type

    @staticmethod
    def normalize_params(p):
        return p

    def execute(self, context):
        if not _FakeNode.script:
            return {"success": True, "content": "default"}
        return _FakeNode.script.pop(0)


def _run(tool, params, monkeypatch, level=3):
    monkeypatch.setattr(te_mod, "ToolNode", _FakeNode)
    _FakeNode.script = [{"success": True, "content": "ran"}]
    ctx = AgentContext({"_execution_level": level})
    return ToolExecutor(retry_attempts=1).execute(
        tool, params, ctx, tools={tool: {}}
    )


def test_detect_backends_returns_list():
    backends = detect_sandbox_backends()
    assert isinstance(backends, list)
    assert backends
    assert all(b in {"wsl", "docker", "none"} for b in backends)


def test_read_only_mode_blocks_writes_and_allows_reads(monkeypatch):
    monkeypatch.setenv("XENON_READ_ONLY", "1")

    blocked = _run("write_file", {"file_path": "a.py", "content": "x"}, monkeypatch)
    assert blocked.success is False
    assert "只读模式" in blocked.observation

    allowed = _run("read_file", {"file_path": "a.py"}, monkeypatch, level=1)
    assert allowed.success is True


def test_command_allowlist_denies_unknown_tokens(monkeypatch):
    monkeypatch.setenv("XENON_COMMAND_ALLOWLIST", "git,pytest")

    blocked = _run("command", {"command": "rm -rf /"}, monkeypatch)
    assert blocked.success is False
    assert "白名单" in blocked.observation

    allowed = _run("command", {"command": "git status"}, monkeypatch)
    assert allowed.success is True


def test_allowlist_helpers():
    assert command_allowlist() is None
    assert command_denial_reason("rm -rf /") is None


def test_no_env_means_no_sandbox_denials(monkeypatch):
    result = _run("write_file", {"file_path": "a.py", "content": "x"}, monkeypatch)
    assert result.success is True
