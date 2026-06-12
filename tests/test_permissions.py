from __future__ import annotations

from pathlib import Path

import pytest

from omniagent.engine.context import AgentContext
from omniagent.engine.permissions import PermissionManager, reset_permission_manager
from omniagent.nodes.tool_node import SecurityError, ToolNode


def test_permission_manager_denies_default_dangerous_command():
    manager = PermissionManager(policy_path=Path("missing-policy.yaml"))

    result = manager.evaluate("command", {"command": "shutdown /s /t 0"})

    assert result.decision == "deny"
    assert "deny pattern" in result.reason


def test_permission_manager_loads_project_policy(tmp_path: Path):
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        """
tools:
  write_file:
    deny_patterns:
      - "*.secret"
""",
        encoding="utf-8",
    )
    manager = PermissionManager(policy_path=policy)

    result = manager.evaluate("write_file", {"file_path": "tokens.secret"})

    assert result.decision == "deny"
    assert result.matched == "*.secret"


def test_tool_node_uses_project_permission_policy(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    policy_dir = tmp_path / ".omniagent"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
tools:
  write_file:
    deny_patterns:
      - "*.blocked"
""",
        encoding="utf-8",
    )
    reset_permission_manager()

    node = ToolNode(
        "writer",
        action_type="write_file",
        file_path=str(tmp_path / "demo.blocked"),
        content="blocked",
    )

    with pytest.raises(SecurityError, match="权限策略拒绝"):
        node.execute(AgentContext())


def test_tool_node_security_disabled_skips_permission_policy(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    policy_dir = tmp_path / ".omniagent"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
tools:
  write_file:
    default: deny
""",
        encoding="utf-8",
    )
    reset_permission_manager()

    target = tmp_path / "allowed.txt"
    node = ToolNode(
        "writer",
        action_type="write_file",
        file_path=str(target),
        content="ok",
        security_enabled=False,
    )
    result = node.execute(AgentContext())

    assert result["success"] is True
    assert target.read_text(encoding="utf-8") == "ok"
