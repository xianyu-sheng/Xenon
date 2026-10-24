"""Real stdio MCP install/verify path, without touching user credentials."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from xenon.engine.context import AgentContext
from xenon.nodes.tool_node import ToolNode

FAKE_MCP_SERVER = r'''
import json
import sys

def send(payload):
    print(json.dumps(payload), flush=True)

for line in sys.stdin:
    try:
        request = json.loads(line)
    except json.JSONDecodeError:
        continue
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "fake", "version": "1.0"},
            },
        })
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "tools": [
                    {
                        "name": "echo",
                        "description": "echo text",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    }
                ]
            },
        })
    elif method == "tools/call":
        params = request.get("params") or {}
        text = (params.get("arguments") or {}).get("text", "")
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": "echo:" + str(text)}],
                "isError": False,
            },
        })
'''


@pytest.fixture()
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "credentials.yaml"
    path.write_text("deepseek: test-key\n", encoding="utf-8")
    monkeypatch.setattr(
        "xenon.repl.provider_registry._get_credentials_path", lambda: path
    )
    return path


@pytest.fixture()
def fake_server(tmp_path):
    script = tmp_path / "fake_mcp_server.py"
    script.write_text(FAKE_MCP_SERVER, encoding="utf-8")
    return script


def test_mcp_install_persists_and_verifies_real_stdio_server(config_path, fake_server):
    node = ToolNode(
        "install",
        action_type="mcp_install",
        mcp_name="fake",
        mcp_command=sys.executable,
        mcp_args=[str(fake_server)],
        mcp_verify=True,
    )

    result = node._mcp_install(AgentContext({}))

    assert result["success"] is True, result
    assert result["verified"] is True
    assert result["tool_count"] == 1
    assert result["tools"] == ["fake:echo"]
    assert Path(result["config_path"]) == config_path
    text = config_path.read_text(encoding="utf-8")
    assert "deepseek: test-key" in text
    assert "name: fake" in text
    assert str(fake_server) in text


def test_mcp_verify_calls_the_installed_tool(config_path, fake_server):
    install = ToolNode(
        "install",
        action_type="mcp_install",
        mcp_name="fake",
        mcp_command=sys.executable,
        mcp_args=[str(fake_server)],
        mcp_verify=False,
    )
    assert install._mcp_install(AgentContext({}))["success"] is True

    verify = ToolNode(
        "verify",
        action_type="mcp_verify",
        mcp_name="fake",
        tool_name="fake:echo",
        tool_args={"text": "hello"},
    )
    result = verify._mcp_verify(AgentContext({}))

    assert result["success"] is True, result
    assert result["verified"] is True
    assert result["tool_count"] == 1
    assert "echo:hello" in result["result_summary"]


def test_mcp_install_missing_env_does_not_write_config(config_path, fake_server):
    node = ToolNode(
        "install",
        action_type="mcp_install",
        mcp_name="fake",
        mcp_command=sys.executable,
        mcp_args=[str(fake_server)],
        mcp_env_from=["XENON_TEST_ENV_THAT_DOES_NOT_EXIST"],
    )

    result = node._mcp_install(AgentContext({}))

    assert result["success"] is False
    assert "XENON_TEST_ENV_THAT_DOES_NOT_EXIST" in result["error"]
    assert "name: fake" not in config_path.read_text(encoding="utf-8")


def test_mcp_install_requires_a_real_command(config_path):
    node = ToolNode(
        "install",
        action_type="mcp_install",
        mcp_name="missing",
        mcp_command="this-command-does-not-exist-xenon-test",
    )

    result = node._mcp_install(AgentContext({}))

    assert result["success"] is False
    assert "不在 PATH" in result["error"]


def test_mcp_install_rejects_invalid_server_name(config_path, fake_server):
    node = ToolNode(
        "install",
        action_type="mcp_install",
        mcp_name="bad:name",
        mcp_command=sys.executable,
        mcp_args=[str(fake_server)],
    )

    result = node._mcp_install(AgentContext({}))

    assert result["success"] is False
    assert ":" in result["error"]


def test_mcp_management_tools_are_registered_and_sensitive():
    from xenon.engine.react_prompts import BUILTIN_TOOLS
    from xenon.nodes.tool_classify import classify_tool, required_execution_level
    from xenon.nodes.tool_registry import BUILTIN_TOOL_REGISTRY

    for name in ("mcp_install", "mcp_verify"):
        assert name in BUILTIN_TOOLS
        assert name in BUILTIN_TOOL_REGISTRY.names()
        assert classify_tool(name, {}) == "SENSITIVE"
        assert required_execution_level(name, {}) == 3
