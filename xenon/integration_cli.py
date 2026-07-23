"""Stable non-interactive integration surface for external agent tooling."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml

from xenon import __version__
from xenon.repl.provider_registry import (
    load_mcp_servers,
    remove_mcp_server,
    save_mcp_server,
)
from xenon.repl.skill_manager import SkillManager


_MCP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_CONFIG_BYTES = 256 * 1024


class IntegrationUsageError(ValueError):
    """The requested integration command has invalid CLI syntax."""


class _ArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that reports errors through Xenon's JSON contract."""

    def error(self, message: str) -> None:
        raise IntegrationUsageError(message)


@dataclass(frozen=True)
class IntegrationContext:
    """Filesystem boundaries used by the non-interactive CLI."""

    home: Path
    project_root: Path | None
    credentials_path: Path

    @classmethod
    def resolve(
        cls,
        *,
        home: Path | None = None,
        project_root: Path | None = None,
        credentials_path: Path | None = None,
    ) -> "IntegrationContext":
        resolved_home = (home or Path.home()).expanduser().resolve()
        resolved_project = (
            project_root.expanduser().resolve()
            if project_root is not None
            else SkillManager._detect_project_root()
        )
        return cls(
            home=resolved_home,
            project_root=resolved_project,
            credentials_path=(
                credentials_path.expanduser().resolve()
                if credentials_path is not None
                else resolved_home / ".xenon" / "credentials.yaml"
            ),
        )


def run_integration_cli(
    argv: list[str],
    *,
    home: Path | None = None,
    project_root: Path | None = None,
    credentials_path: Path | None = None,
) -> int:
    """Run ``integrations``, ``skill``, or ``mcp`` without starting the REPL."""
    context = IntegrationContext.resolve(
        home=home,
        project_root=project_root,
        credentials_path=credentials_path,
    )
    if not argv:
        _print_root_help()
        return 2
    domain, rest = argv[0], argv[1:]
    try:
        if domain == "integrations":
            return _run_integrations(rest, context)
        if domain == "skill":
            return _run_skill(rest, context)
        if domain == "mcp":
            return _run_mcp(rest, context)
        _emit_error(f"未知集成命令: {domain}", json_output="--json" in rest)
        return 2
    except IntegrationUsageError as exc:
        _emit_error(str(exc), json_output="--json" in argv)
        return 2
    except (OSError, ValueError, KeyError, yaml.YAMLError) as exc:
        _emit_error(str(exc), json_output="--json" in argv)
        return 1


def _manager(context: IntegrationContext) -> SkillManager:
    return SkillManager(
        context.home / ".xenon" / "skills",
        project_root=context.project_root,
        shared_skills_dir=context.home / ".agents" / "skills",
    )


def _run_integrations(argv: list[str], context: IntegrationContext) -> int:
    json_output, args = _remove_flag(argv, "--json")
    action = args[0] if args else "describe"
    if action in {"-h", "--help", "help"}:
        print("usage: xenon integrations describe [--json]")
        return 0
    if action != "describe" or len(args) > 1:
        _emit_error("用法: xenon integrations describe [--json]", json_output=json_output)
        return 2

    project = context.project_root
    payload = {
        "schema_version": "1.0",
        "product": {
            "id": "xenon",
            "name": "Xenon",
            "version": __version__,
            "executable": "xenon",
        },
        "non_interactive": True,
        "json_output": True,
        "agent_skills": {
            "format": "SKILL.md",
            "progressive_loading": True,
            "user_paths": [
                str(context.home / ".agents" / "skills"),
                str(context.home / ".xenon" / "skills"),
            ],
            "project_paths": (
                [
                    str(project / ".agents" / "skills"),
                    str(project / ".xenon" / "skills"),
                ]
                if project is not None else []
            ),
            "install_command": (
                "xenon skill install <path> "
                "[--scope user|shared-user|project|shared-project] [--json]"
            ),
        },
        "mcp": {
            "transports": ["stdio", "http", "sse"],
            "supports_stdio_env": True,
            "supports_http_headers": True,
            "secret_safe_stdin": True,
            "add_command": "xenon mcp add <name> --config - --json",
            "list_command": "xenon mcp list --json",
            "doctor_command": "xenon mcp doctor --json",
        },
        "providers": {
            "openai_compatible": True,
            "configuration_command": "xenon models import -f <yaml>",
        },
    }
    _emit(payload, json_output=json_output, text=_describe_text(payload))
    return 0


def _describe_text(payload: dict[str, Any]) -> str:
    skills = payload["agent_skills"]
    mcp = payload["mcp"]
    return "\n".join([
        f"Xenon {payload['product']['version']} integration contract v{payload['schema_version']}",
        "Agent Skills: SKILL.md（渐进式加载）",
        *[f"  - {path}" for path in skills["user_paths"] + skills["project_paths"]],
        f"MCP: {', '.join(mcp['transports'])}（stdio env / HTTP headers）",
        "JSON: 所有集成命令支持 --json",
    ])


def _run_skill(argv: list[str], context: IntegrationContext) -> int:
    json_output, args = _remove_flag(argv, "--json")
    if not args or args[0] in {"-h", "--help", "help"}:
        print(
            "usage: xenon skill install <path> [--scope SCOPE] [--force] [--json]\n"
            "       xenon skill list|doctor [--json]"
        )
        return 0 if args else 2
    action, rest = args[0], args[1:]
    manager = _manager(context)

    if action == "install":
        parser = _ArgumentParser(prog="xenon skill install", add_help=False)
        parser.add_argument("source")
        parser.add_argument(
            "--scope",
            choices=["user", "shared-user", "project", "shared-project"],
            default="user",
        )
        parser.add_argument("--force", action="store_true")
        options = parser.parse_args(rest)
        receipt = manager.install(options.source, scope=options.scope, force=options.force)
        payload = {
            "ok": True,
            "action": "installed",
            "name": receipt.name,
            "scope": receipt.scope,
            "destination": str(receipt.destination),
            "file_count": receipt.file_count,
            "total_bytes": receipt.total_bytes,
            "replaced": receipt.replaced,
        }
        _emit(
            payload,
            json_output=json_output,
            text=(
                f"✅ 已安装 Agent Skill '{receipt.name}' → {receipt.destination}\n"
                f"   {receipt.file_count} 个文件 · {receipt.total_bytes} bytes"
                + (" · 已替换旧版本" if receipt.replaced else "")
            ),
        )
        return 0

    if action == "list" and not rest:
        skills = [
            {
                "name": skill.name,
                "description": skill.description,
                "format": skill.format,
                "source": skill.source,
                "version": skill.version or None,
                "path": str(skill.path) if skill.path else None,
                "instructions_loaded": skill.instructions is not None,
            }
            for skill in manager.list_all()
        ]
        payload = {"ok": True, "count": len(skills), "skills": skills}
        text = "\n".join(
            [f"已安装 {len(skills)} 个技能:"]
            + [f"  /{item['name']} · {item['format']} · {item['source']}" for item in skills]
        )
        _emit(payload, json_output=json_output, text=text)
        return 0

    if action == "doctor" and not rest:
        report = manager.diagnostics()
        payload = {"ok": not report["errors"], **report}
        lines = [
            f"技能: {report['skill_count']}（Agent {report['agent_skill_count']} / "
            f"YAML {report['legacy_skill_count']}）",
            f"加载错误: {len(report['errors'])}",
        ]
        lines.extend(f"  - {error}" for error in report["errors"])
        _emit(payload, json_output=json_output, text="\n".join(lines))
        return 0 if payload["ok"] else 1

    _emit_error("用法: xenon skill install|list|doctor ...", json_output=json_output)
    return 2


def _run_mcp(argv: list[str], context: IntegrationContext) -> int:
    json_output, args = _remove_flag_before_delimiter(argv, "--json")
    if not args or args[0] in {"-h", "--help", "help"}:
        print(
            "usage: xenon mcp add <name> <command-or-url> [args...] [--json]\n"
            "       xenon mcp add <name> --config <path|-> [--json]\n"
            "       xenon mcp list|doctor [--json]\n"
            "       xenon mcp remove <name> [--json]"
        )
        return 0 if args else 2
    action, rest = args[0], args[1:]

    if action == "add":
        return _mcp_add(rest, context, json_output=json_output)
    if action == "list" and not rest:
        servers = [
            _summarize_mcp_server(server)
            for server in sorted(
                load_mcp_servers(context.credentials_path),
                key=lambda item: str(item.get("name", "")),
            )
        ]
        payload = {"ok": True, "count": len(servers), "servers": servers}
        text = "\n".join(
            [f"已配置 {len(servers)} 个 MCP 服务器:"]
            + [f"  {server['name']} · {server['transport']} · {server['target']}" for server in servers]
        )
        _emit(payload, json_output=json_output, text=text)
        return 0
    if action == "remove" and len(rest) == 1:
        removed = remove_mcp_server(rest[0], path=context.credentials_path)
        payload = {"ok": removed, "name": rest[0], "removed": removed}
        _emit(
            payload,
            json_output=json_output,
            text=(f"✅ 已移除 MCP 服务器 '{rest[0]}'" if removed else f"未找到 MCP 服务器 '{rest[0]}'"),
        )
        return 0 if removed else 1
    if action == "doctor" and not rest:
        payload = _doctor_mcp(context)
        text = "\n".join([
            f"MCP 配置: {payload['server_count']} 个服务器",
            f"错误: {len(payload['errors'])} · 警告: {len(payload['warnings'])}",
            *[f"  ❌ {item}" for item in payload["errors"]],
            *[f"  ⚠️  {item}" for item in payload["warnings"]],
        ])
        _emit(payload, json_output=json_output, text=text)
        return 0 if payload["ok"] else 1

    _emit_error("用法: xenon mcp add|list|remove|doctor ...", json_output=json_output)
    return 2


def _mcp_add(argv: list[str], context: IntegrationContext, *, json_output: bool) -> int:
    passthrough: list[str] = []
    if "--" in argv:
        delimiter = argv.index("--")
        argv, passthrough = argv[:delimiter], argv[delimiter + 1:]
    parser = _ArgumentParser(prog="xenon mcp add", add_help=False)
    parser.add_argument("name")
    parser.add_argument("target", nargs="?")
    parser.add_argument("--transport", choices=["stdio", "http", "sse"])
    parser.add_argument("--command")
    parser.add_argument("--url")
    parser.add_argument("--arg", action="append", default=[])
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("--header", action="append", default=[])
    parser.add_argument("--config")
    options, unknown = parser.parse_known_args(argv)
    if not _MCP_NAME_RE.fullmatch(options.name):
        raise ValueError("MCP 名称必须为 1-64 位字母、数字、点、连字符或下划线")

    if options.config:
        if any((options.target, options.command, options.url, options.arg, options.env, options.header, unknown, passthrough)):
            raise ValueError("--config 不能与命令、URL、env、header 或额外参数混用")
        config = _read_mcp_config(options.config)
    else:
        command = options.command
        url = options.url
        if options.target:
            if options.target.startswith(("http://", "https://")):
                url = url or options.target
            else:
                command = command or options.target
        config = {
            "transport": options.transport or ("http" if url else "stdio"),
            "command": command,
            "url": url,
            "args": [*options.arg, *unknown, *passthrough],
            "env": _parse_pairs(options.env, "env"),
            "headers": _parse_pairs(options.header, "header"),
        }

    normalized = _validate_mcp_config(config)
    if normalized["transport"] == "stdio":
        save_mcp_server(
            options.name,
            command=normalized["command"],
            args=normalized["args"],
            env=normalized["env"],
            path=context.credentials_path,
        )
    else:
        save_mcp_server(
            options.name,
            url=normalized["url"],
            headers=normalized["headers"],
            path=context.credentials_path,
        )
    summary = _summarize_mcp_server({"name": options.name, **normalized})
    payload = {
        "ok": True,
        "action": "configured",
        "server": summary,
        "configuration_path": str(context.credentials_path),
        "lazy": True,
        "restart_required": True,
    }
    _emit(
        payload,
        json_output=json_output,
        text=(
            f"✅ MCP 服务器 '{options.name}' 已登记（按需连接）\n"
            f"   {summary['transport']} · {summary['target']}\n"
            f"   配置: {context.credentials_path}（重启 Xenon 后加载）"
        ),
    )
    return 0


def _read_mcp_config(location: str) -> dict[str, Any]:
    if location == "-":
        content = sys.stdin.read(_CONFIG_BYTES + 1)
    else:
        path = Path(location).expanduser()
        if path.stat().st_size > _CONFIG_BYTES:
            raise ValueError(f"MCP 配置超过 {_CONFIG_BYTES // 1024} KiB 限制")
        content = path.read_text(encoding="utf-8")
    if len(content.encode("utf-8")) > _CONFIG_BYTES:
        raise ValueError(f"MCP 配置超过 {_CONFIG_BYTES // 1024} KiB 限制")
    data = yaml.safe_load(content)
    if not isinstance(data, dict):
        raise ValueError("MCP 配置必须是 JSON/YAML 对象")
    return data


def _validate_mcp_config(config: dict[str, Any]) -> dict[str, Any]:
    transport = str(config.get("transport") or ("http" if config.get("url") else "stdio")).lower()
    if transport == "sse":
        transport = "http"
    if transport not in {"stdio", "http"}:
        raise ValueError("MCP transport 必须是 stdio/http/sse")
    if transport == "stdio":
        command = str(config.get("command") or "").strip()
        if not command:
            raise ValueError("stdio MCP 缺少 command")
        args = config.get("args", [])
        env = config.get("env", {})
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise ValueError("stdio MCP args 必须是字符串数组")
        if not isinstance(env, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in env.items()
        ):
            raise ValueError("stdio MCP env 必须是字符串键值对象")
        invalid_env = [
            key for key in env
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
        ]
        if invalid_env:
            raise ValueError(f"stdio MCP env key 无效: {invalid_env[0]}")
        return {
            "transport": "stdio",
            "command": command,
            "args": args,
            "env": env,
            "url": "",
            "headers": {},
        }

    url = str(config.get("url") or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("HTTP MCP 需要有效的 http/https URL")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("HTTP MCP URL 端口无效") from exc
    headers = config.get("headers", {})
    if not isinstance(headers, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in headers.items()
    ):
        raise ValueError("HTTP MCP headers 必须是字符串键值对象")
    invalid_header = next(
        (
            key for key, value in headers.items()
            if not key or any(char in key + value for char in "\r\n")
        ),
        None,
    )
    if invalid_header is not None:
        raise ValueError(f"HTTP MCP header 无效: {invalid_header}")
    return {
        "transport": "http",
        "command": "",
        "args": [],
        "env": {},
        "url": url,
        "headers": headers,
    }


def _parse_pairs(values: list[str], label: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"--{label} 必须使用 KEY=VALUE")
        key, item = value.split("=", 1)
        if not key:
            raise ValueError(f"--{label} 的 KEY 不能为空")
        result[key] = item
    return result


def _summarize_mcp_server(server: dict[str, Any]) -> dict[str, Any]:
    name = str(server.get("name", ""))
    if server.get("url"):
        headers = server.get("headers", {})
        url = str(server.get("url", ""))
        return {
            "name": name,
            "transport": "http",
            "target": _redact_url(url),
            "query_configured": bool(urlsplit(url).query),
            "header_keys": sorted(headers) if isinstance(headers, dict) else [],
        }
    env = server.get("env", {})
    args = server.get("args", [])
    return {
        "name": name,
        "transport": "stdio",
        "target": str(server.get("command", "")),
        "args_count": len(args) if isinstance(args, list) else 0,
        "env_keys": sorted(env) if isinstance(env, dict) else [],
    }


def _redact_url(url: str) -> str:
    parsed = urlsplit(url)
    hostname = parsed.hostname or ""
    if parsed.port:
        hostname = f"{hostname}:{parsed.port}"
    return urlunsplit((parsed.scheme, hostname, parsed.path, "<redacted>" if parsed.query else "", ""))


def _doctor_mcp(context: IntegrationContext) -> dict[str, Any]:
    servers = load_mcp_servers(context.credentials_path)
    errors: list[str] = []
    warnings: list[str] = []
    names: set[str] = set()
    for index, server in enumerate(servers):
        name = str(server.get("name", ""))
        label = name or f"entry[{index}]"
        if not _MCP_NAME_RE.fullmatch(name):
            errors.append(f"{label}: 名称无效")
        if name in names:
            errors.append(f"{label}: 名称重复")
        names.add(name)
        try:
            normalized = _validate_mcp_config(server)
        except ValueError as exc:
            errors.append(f"{label}: {exc}")
            continue
        if normalized["transport"] == "stdio":
            command = normalized["command"]
            if Path(command).is_absolute():
                available = Path(command).is_file()
            else:
                available = shutil.which(command) is not None
            if not available:
                warnings.append(f"{label}: command 当前不可用: {command}")
    if context.credentials_path.exists():
        mode = stat.S_IMODE(context.credentials_path.stat().st_mode)
        if mode & 0o077:
            errors.append(
                f"凭证文件权限过宽: {oct(mode)}，应为 0o600: {context.credentials_path}"
            )
    return {
        "ok": not errors,
        "server_count": len(servers),
        "configuration_path": str(context.credentials_path),
        "errors": errors,
        "warnings": warnings,
    }


def _remove_flag(argv: list[str], flag: str) -> tuple[bool, list[str]]:
    found = flag in argv
    return found, [item for item in argv if item != flag]


def _remove_flag_before_delimiter(argv: list[str], flag: str) -> tuple[bool, list[str]]:
    if "--" not in argv:
        return _remove_flag(argv, flag)
    delimiter = argv.index("--")
    head = argv[:delimiter]
    tail = argv[delimiter:]
    found, cleaned = _remove_flag(head, flag)
    return found, [*cleaned, *tail]


def _emit(payload: dict[str, Any], *, json_output: bool, text: str) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(text)


def _emit_error(message: str, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"ok": False, "error": message}, ensure_ascii=False, sort_keys=True))
    else:
        print(f"❌ {message}", file=sys.stderr)


def _print_root_help() -> None:
    print("usage: xenon integrations|skill|mcp ...", file=sys.stderr)
