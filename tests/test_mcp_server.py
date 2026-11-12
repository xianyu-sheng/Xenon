"""MCP server: protocol shape, permission ceiling, workspace fence, evidence."""

from __future__ import annotations

import io
import json
import types
from pathlib import Path

import pytest

import xenon.kernel  # noqa: F401 — ledger<->kernel import cycle
from xenon.mcp.server import PROTOCOL_VERSION, McpServer, ToolError


def _server(**kwargs) -> McpServer:
    base = {"workspace": ".", "permission": "read-only"}
    base.update(kwargs)
    return McpServer(**base)


def _call(server: McpServer, name: str, arguments: dict) -> dict:
    return server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )


def _text(response: dict) -> str:
    return response["result"]["content"][0]["text"]


def test_initialize_negotiates_a_known_protocol() -> None:
    server = _server()

    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
        }
    )

    assert response["result"]["protocolVersion"] == "2024-11-05"
    assert response["result"]["serverInfo"]["name"] == "xenon"


def test_an_unknown_protocol_falls_back_to_ours() -> None:
    response = _server().handle(
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {"protocolVersion": "1999-01-01"},
        }
    )

    assert response["result"]["protocolVersion"] == PROTOCOL_VERSION


def test_notifications_get_no_response() -> None:
    assert _server().handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_tools_list_advertises_the_three_tools() -> None:
    response = _server().handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    names = {tool["name"] for tool in response["result"]["tools"]}
    assert names == {"xenon_run_task", "xenon_evidence", "xenon_audit"}
    for tool in response["result"]["tools"]:
        assert tool["inputSchema"]["type"] == "object"


def test_an_unknown_method_is_a_protocol_error() -> None:
    response = _server().handle({"jsonrpc": "2.0", "id": 7, "method": "nope"})

    assert response["error"]["code"] == -32601


def test_an_unknown_tool_is_tool_error_content() -> None:
    response = _call(_server(), "nope", {})

    assert response["result"]["isError"] is True


def test_a_call_cannot_raise_the_permission_ceiling(tmp_path: Path) -> None:
    server = _server(workspace=str(tmp_path), permission="read-only")

    with pytest.raises(ToolError, match="exceeds the server ceiling"):
        server._permission_for("bypass")


def test_a_call_may_lower_the_ceiling(tmp_path: Path) -> None:
    server = _server(workspace=str(tmp_path), permission="auto-edit")

    assert server._permission_for("read-only") == "read-only"


def test_workspace_must_stay_inside_the_server_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "elsewhere"
    root.mkdir()
    outside.mkdir()
    server = _server(workspace=str(root))

    with pytest.raises(ToolError, match="escapes the server root"):
        server._workspace_for(str(outside))

    permissive = _server(workspace=str(root), allow_any_workspace=True)
    assert permissive._workspace_for(str(outside)) == outside.resolve()


def test_run_task_returns_the_ledger_and_evidence(tmp_path: Path, monkeypatch) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("", encoding="utf-8")

    class _Run:
        ledger_path = ledger
        result = types.SimpleNamespace(
            task_id="t-1",
            phase=types.SimpleNamespace(value="finalized"),
            accepted=True,
            evidence_status=types.SimpleNamespace(value="verified"),
            answer="done",
            stop_signal="",
            debt=(),
        )

    import xenon.harness.llm as llm_mod
    import xenon.harness.runner as runner_mod
    import xenon.repl.system_config as config_mod
    import xenon.sandbox.backends as sandbox_mod

    monkeypatch.setattr(
        llm_mod, "resolve_model_config", lambda *_a, **_k: types.SimpleNamespace(model_id="m")
    )
    monkeypatch.setattr(llm_mod, "build_llm_loop_strategy", lambda *_a, **_k: object())
    monkeypatch.setattr(runner_mod, "run_task", lambda objective, **kwargs: _Run())
    monkeypatch.setattr(
        config_mod,
        "get_config",
        lambda: types.SimpleNamespace(sandbox=object()),
    )
    monkeypatch.setattr(sandbox_mod, "spec_from_config", lambda _cfg: object())
    monkeypatch.setattr(sandbox_mod, "build_runtime", lambda *_a, **_k: None)

    server = _server(workspace=str(tmp_path), permission="read-only")
    payload = json.loads(_text(_call(server, "xenon_run_task", {"objective": "do it"})))

    assert payload["task_id"] == "t-1"
    assert payload["evidence"] == "verified"
    assert payload["ledger"] == str(ledger)


def test_run_task_requires_an_objective(tmp_path: Path) -> None:
    server = _server(workspace=str(tmp_path))

    with pytest.raises(ToolError, match="objective is required"):
        server.dispatch("xenon_run_task", {})


def test_evidence_reads_a_real_ledger(tmp_path: Path) -> None:
    from xenon.ledger.events import EventKind, EventSource
    from xenon.ledger.store import Ledger

    path = tmp_path / "task.jsonl"
    ledger = Ledger(path, "t")
    ledger.append(EventKind.TASK_STARTED, EventSource.RUNTIME, {"objective": "o"})
    ledger.append(
        EventKind.STATE_TRANSITION,
        EventSource.RUNTIME,
        {"from": "running", "to": "finalized"},
    )
    ledger.append(
        EventKind.FINALIZED,
        EventSource.RUNTIME,
        {"evidence_status": "unverified", "answer": "ok"},
    )

    server = _server(workspace=str(tmp_path))
    report = json.loads(
        _text(_call(server, "xenon_evidence", {"ledger": str(path)}))
    )

    assert report["task"]["id"] == "t"
    assert report["status"]["phase"] in {"finalized", "audited"}


def test_audit_reads_a_transcript(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "calc.py").write_text("x", encoding="utf-8")
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {"file_path": str(workspace / "calc.py")},
                        }
                    ]
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    server = _server(workspace=str(workspace))
    report = json.loads(
        _text(_call(server, "xenon_audit", {"transcript": str(transcript)}))
    )

    assert report["schema"]
    assert report["claims"][0]["target"] == "calc.py"


def test_stdout_carries_only_protocol_lines(monkeypatch) -> None:
    from xenon.mcp import server as server_mod

    requests = "\n".join(
        [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        ]
    )
    stdin = io.StringIO(requests + "\n")
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(server_mod.sys, "stdin", stdin)
    monkeypatch.setattr(server_mod.sys, "stdout", stdout)
    monkeypatch.setattr(server_mod.sys, "stderr", stderr)

    assert server_mod.serve(workspace=".") == 0

    lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
    assert len(lines) == 2
    assert all(json.loads(line)["jsonrpc"] == "2.0" for line in lines)
