"""clone_repo 的 git 认证头：git smart HTTP 只接受 Basic，不接受 Bearer。"""

from __future__ import annotations

import base64

import xenon.nodes.tool_families.github_tools as github_tools
from xenon.nodes.tool_node import ToolNode


def test_git_auth_env_uses_basic_x_access_token(monkeypatch) -> None:
    monkeypatch.setattr(github_tools, "load_github_token", lambda: "tok-123")
    env = ToolNode._git_auth_env()

    index = int(env["GIT_CONFIG_COUNT"]) - 1
    value = env[f"GIT_CONFIG_VALUE_{index}"]
    assert value.startswith("Authorization: basic ")
    decoded = base64.b64decode(value.split(" ", 2)[2]).decode("utf-8")
    assert decoded == "x-access-token:tok-123"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_git_auth_env_without_token_has_no_header(monkeypatch) -> None:
    monkeypatch.setattr(github_tools, "load_github_token", lambda: "")
    env = ToolNode._git_auth_env()

    assert "GIT_CONFIG_COUNT" not in env or env.get("GIT_CONFIG_COUNT") == "0"
