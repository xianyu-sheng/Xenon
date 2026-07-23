"""Non-interactive integration CLI contract tests."""

from __future__ import annotations

import io
import json
import stat
import sys
import threading
from pathlib import Path

import yaml

from xenon.integration_cli import run_integration_cli
from xenon.engine.context import AgentContext
from xenon.mcp.registry import MCPRegistry
from xenon.repl.repl import REPL
from xenon.repl.provider_registry import load_mcp_servers, save_mcp_server


def _write_skill(root: Path, name: str = "sample") -> Path:
    skill = root / name
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: Test integration skill\n"
        "version: 1.2.3\n"
        "---\n\n"
        "# Workflow\nRead references/guide.md when needed.\n",
        encoding="utf-8",
    )
    (skill / "references" / "guide.md").write_text("guide", encoding="utf-8")
    return skill


def _run(argv, tmp_path, capsys, **kwargs):
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir(exist_ok=True)
    project.mkdir(exist_ok=True)
    code = run_integration_cli(
        argv,
        home=home,
        project_root=project,
        credentials_path=home / ".xenon" / "credentials.yaml",
        **kwargs,
    )
    captured = capsys.readouterr()
    return code, captured.out, captured.err, home, project


def test_integrations_describe_json_is_machine_readable(tmp_path, capsys):
    code, stdout, stderr, home, project = _run(
        ["integrations", "describe", "--json"], tmp_path, capsys
    )

    payload = json.loads(stdout)
    assert code == 0
    assert stderr == ""
    assert payload["schema_version"] == "1.0"
    assert payload["product"]["id"] == "xenon"
    assert payload["agent_skills"]["format"] == "SKILL.md"
    assert str(home / ".agents" / "skills") in payload["agent_skills"]["user_paths"]
    assert str(project / ".xenon" / "skills") in payload["agent_skills"]["project_paths"]
    assert payload["mcp"]["supports_stdio_env"] is True
    assert payload["mcp"]["supports_http_headers"] is True


def test_skill_install_list_and_force_replace(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")

    code, stdout, _, home, _ = _run(
        ["skill", "install", str(source), "--json"], tmp_path, capsys
    )
    receipt = json.loads(stdout)
    destination = home / ".xenon" / "skills" / "sample"

    assert code == 0
    assert receipt["file_count"] == 2
    assert receipt["replaced"] is False
    assert destination.joinpath("references/guide.md").read_text() == "guide"

    code, stdout, _, _, _ = _run(
        ["skill", "list", "--json"], tmp_path, capsys
    )
    listing = json.loads(stdout)
    assert code == 0
    assert listing["count"] == 1
    assert listing["skills"][0]["instructions_loaded"] is False

    code, stdout, _, _, _ = _run(
        ["skill", "install", str(source), "--json"], tmp_path, capsys
    )
    assert code == 1
    assert json.loads(stdout)["ok"] is False

    source.joinpath("references/guide.md").write_text("updated", encoding="utf-8")
    code, stdout, _, _, _ = _run(
        ["skill", "install", str(source), "--force", "--json"], tmp_path, capsys
    )
    assert code == 0
    assert json.loads(stdout)["replaced"] is True
    assert destination.joinpath("references/guide.md").read_text() == "updated"


def test_skill_install_rejects_out_of_tree_symlink(tmp_path, capsys):
    source = _write_skill(tmp_path / "source")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    source.joinpath("references/escape.txt").symlink_to(outside)

    code, stdout, _, home, _ = _run(
        ["skill", "install", str(source), "--json"], tmp_path, capsys
    )

    assert code == 1
    assert "符号链接" in json.loads(stdout)["error"]
    assert not home.joinpath(".xenon/skills/sample").exists()


def test_mcp_stdio_config_from_stdin_persists_but_redacts_secrets(
    tmp_path, capsys, monkeypatch,
):
    secret = "plan-key-super-secret"
    config = {
        "transport": "stdio",
        "command": sys.executable,
        "args": ["-m", "example_server"],
        "env": {"AGENTPLAN_API_KEY": secret},
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(config)))

    code, stdout, stderr, home, _ = _run(
        ["mcp", "add", "ark-search", "--config", "-", "--json"],
        tmp_path,
        capsys,
    )
    payload = json.loads(stdout)
    credentials = home / ".xenon" / "credentials.yaml"

    assert code == 0
    assert stderr == ""
    assert secret not in stdout
    assert payload["server"]["env_keys"] == ["AGENTPLAN_API_KEY"]
    stored = yaml.safe_load(credentials.read_text(encoding="utf-8"))
    assert stored["_mcp_servers"][0]["env"]["AGENTPLAN_API_KEY"] == secret
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600

    code, stdout, _, _, _ = _run(["mcp", "list", "--json"], tmp_path, capsys)
    assert code == 0
    assert secret not in stdout
    assert json.loads(stdout)["servers"][0]["args_count"] == 2


def test_mcp_http_headers_are_loaded_and_redacted(tmp_path, capsys, monkeypatch):
    secret = "bearer-secret"
    config = {
        "transport": "http",
        "url": "https://mcp.example.test/rpc?token=url-secret",
        "headers": {"Authorization": f"Bearer {secret}"},
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(config)))

    code, stdout, _, home, _ = _run(
        ["mcp", "add", "datapro", "--config", "-", "--json"],
        tmp_path,
        capsys,
    )
    payload = json.loads(stdout)

    assert code == 0
    assert secret not in stdout
    assert "url-secret" not in stdout
    assert payload["server"]["header_keys"] == ["Authorization"]
    assert payload["server"]["query_configured"] is True
    stored = load_mcp_servers(home / ".xenon" / "credentials.yaml")
    assert stored[0]["headers"]["Authorization"] == f"Bearer {secret}"


def test_pending_mcp_registry_forwards_env_and_headers(monkeypatch):
    registry = MCPRegistry()
    registry.add_server_pending(
        "stdio",
        command="tool",
        args=["serve"],
        env={"TOKEN": "stdio-secret"},
    )
    registry.add_server_pending(
        "http",
        url="https://mcp.example.test/rpc",
        headers={"Authorization": "Bearer http-secret"},
    )
    calls = []

    def fake_add(name, **kwargs):
        calls.append((name, kwargs))

    monkeypatch.setattr(registry, "add_server", fake_add)

    registry._ensure_connected()

    assert calls == [
        (
            "stdio",
            {
                "command": "tool",
                "args": ["serve"],
                "env": {"TOKEN": "stdio-secret"},
            },
        ),
        (
            "http",
            {
                "url": "https://mcp.example.test/rpc",
                "headers": {"Authorization": "Bearer http-secret"},
            },
        ),
    ]


def test_repl_preload_preserves_persisted_mcp_credentials(monkeypatch):
    servers = [
        {
            "name": "stdio",
            "command": "tool",
            "args": ["serve"],
            "env": {"TOKEN": "stdio-secret"},
        },
        {
            "name": "http",
            "url": "https://mcp.example.test/rpc",
            "headers": {"Authorization": "Bearer http-secret"},
        },
    ]
    monkeypatch.setattr("xenon.repl.provider_registry.load_mcp_servers", lambda: servers)
    repl = REPL.__new__(REPL)
    repl._mcp_registry = None
    repl.agent_context = AgentContext()

    repl._preload_mcp_server_configs()

    assert repl._mcp_registry._pending_configs["stdio"]["env"] == {
        "TOKEN": "stdio-secret"
    }
    assert repl._mcp_registry._pending_configs["http"]["headers"] == {
        "Authorization": "Bearer http-secret"
    }


def test_mcp_legacy_command_form_and_doctor(tmp_path, capsys):
    code, stdout, _, _, _ = _run(
        ["mcp", "add", "local", sys.executable, "--json", "--", "-m", "server"],
        tmp_path,
        capsys,
    )
    assert code == 0
    assert json.loads(stdout)["server"]["args_count"] == 2

    code, stdout, _, _, _ = _run(["mcp", "doctor", "--json"], tmp_path, capsys)
    report = json.loads(stdout)
    assert code == 0
    assert report["ok"] is True
    assert report["server_count"] == 1


def test_mcp_remove_is_explicit_and_noninteractive(tmp_path, capsys):
    _run(["mcp", "add", "local", sys.executable, "--json"], tmp_path, capsys)

    code, stdout, _, _, _ = _run(
        ["mcp", "remove", "local", "--json"], tmp_path, capsys
    )

    assert code == 0
    assert json.loads(stdout)["removed"] is True
    code, stdout, _, _, _ = _run(["mcp", "list", "--json"], tmp_path, capsys)
    assert json.loads(stdout)["count"] == 0


def test_invalid_mcp_config_is_rejected_before_persistence(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps({"transport": "http", "url": "https://host:bad/rpc"})),
    )

    code, stdout, stderr, home, _ = _run(
        ["mcp", "add", "broken", "--config", "-", "--json"], tmp_path, capsys
    )

    assert code == 1
    assert stderr == ""
    assert json.loads(stdout)["ok"] is False
    assert load_mcp_servers(home / ".xenon" / "credentials.yaml") == []


def test_concurrent_mcp_writes_keep_all_servers(tmp_path):
    credentials = tmp_path / "credentials.yaml"
    errors = []

    def writer(index):
        try:
            save_mcp_server(
                f"server-{index}",
                command=sys.executable,
                args=[str(index)],
                path=credentials,
            )
        except Exception as exc:  # pragma: no cover - assertion reports details
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert {server["name"] for server in load_mcp_servers(credentials)} == {
        f"server-{index}" for index in range(12)
    }


def test_main_routes_integration_commands_without_starting_repl(monkeypatch):
    import xenon.integration_cli as integration_cli
    import xenon.main as main

    calls = []
    monkeypatch.setattr(integration_cli, "run_integration_cli", lambda argv: calls.append(argv) or 0)
    monkeypatch.setattr(sys, "argv", ["xenon", "integrations", "describe", "--json"])

    main.cli()

    assert calls == [["integrations", "describe", "--json"]]


def test_invalid_json_command_keeps_stdout_structured(tmp_path, capsys):
    code, stdout, stderr, _, _ = _run(
        ["skill", "install", "--json"], tmp_path, capsys
    )

    assert code == 2
    assert stderr == ""
    assert json.loads(stdout)["ok"] is False
