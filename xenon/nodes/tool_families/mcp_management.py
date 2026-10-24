"""High-level MCP management actions.

``mcp_install`` and ``mcp_verify`` keep the agent away from Xenon's source
tree and the generic shell while still going through ToolExecutor receipts.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from xenon.engine.context import AgentContext
from xenon.mcp.registry import MCPRegistry, _redact_text, _validate_server_name

logger = logging.getLogger(__name__)


def _as_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        raw = value.strip()
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        return [raw] if raw else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "n"}


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _result_text(result: Any, limit: int = 2000) -> str:
    if not isinstance(result, dict):
        return _redact_text(str(result))[:limit]
    parts: list[str] = []
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            else:
                parts.append(str(item))
    if not parts:
        parts.append(json.dumps(result, ensure_ascii=False, default=str))
    return _redact_text("\n".join(parts))[:limit]


def _mark_recipe(context: AgentContext, **fields: Any) -> None:
    recipe = context.get("_task_recipe") if context is not None else None
    if isinstance(recipe, dict):
        recipe.update(fields)


def _error(action: str, message: str, **extra: Any) -> dict[str, Any]:
    return {
        "action_type": action,
        "success": False,
        "error": _redact_text(message)[:800],
        **extra,
    }


def _restore_server(previous: dict[str, Any] | None, name: str) -> None:
    from xenon.repl.provider_registry import remove_mcp_server, save_mcp_server

    if not previous:
        remove_mcp_server(name)
        return
    save_mcp_server(
        name,
        command=str(previous.get("command") or ""),
        args=_as_list(previous.get("args")),
        url=str(previous.get("url") or ""),
        env=previous.get("env") if isinstance(previous.get("env"), dict) else None,
        headers=(
            previous.get("headers")
            if isinstance(previous.get("headers"), dict)
            else None
        ),
    )


class MCPManagementToolsMixin:
    """Install/verify MCP servers through one structured tool call each."""

    def _mcp_install(self, context: AgentContext) -> dict[str, Any]:
        """Persist one MCP entry and optionally verify it.

        It writes only the Xenon config, starts the child once for discovery,
        and returns a tool count.  It never edits the repository, runs project
        tests, or creates a commit.
        """

        from xenon.repl.provider_registry import (
            _get_credentials_path,
            load_mcp_servers,
            save_mcp_server,
        )

        name = str(
            getattr(self, "mcp_name", "")
            or getattr(self, "mcp_server", "")
            or ""
        ).strip()
        command = str(
            getattr(self, "mcp_command", "")
            or (getattr(self, "action", "") if self.action_type == "mcp_install" else "")
            or ""
        ).strip()
        url = str(
            getattr(self, "mcp_url", "") or getattr(self, "url", "") or ""
        ).strip()
        args = _as_list(getattr(self, "mcp_args", None))
        env_from = _as_list(getattr(self, "mcp_env_from", None))
        verify = _as_bool(getattr(self, "mcp_verify", True))
        warnings: list[str] = []

        if not name:
            return _error("mcp_install", "mcp_install 需要 mcp_name")
        try:
            _validate_server_name(name)
        except ValueError as exc:
            return _error("mcp_install", str(exc))
        if bool(command) == bool(url):
            return _error(
                "mcp_install", "mcp_install 必须且只能提供 mcp_command 或 mcp_url"
            )
        if url:
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                return _error("mcp_install", "mcp_url 必须是 http(s) URL")
        else:
            command_path = Path(command).expanduser()
            path_like = any(sep and sep in command for sep in (os.sep, os.altsep))
            if path_like:
                if not command_path.is_file():
                    return _error("mcp_install", f"mcp_command 不存在: {command}")
                command = str(command_path.resolve())
            elif shutil.which(command) is None:
                return _error("mcp_install", f"mcp_command 不在 PATH 中: {command}")

        env: dict[str, str] = {}
        missing = [key for key in env_from if not os.environ.get(key)]
        if missing:
            return _error(
                "mcp_install",
                "缺少环境变量: " + ", ".join(missing),
                missing_env=missing,
            )
        env = {key: str(os.environ[key]) for key in env_from}

        try:
            previous = next(
                (item for item in load_mcp_servers() if item.get("name") == name),
                None,
            )
        except Exception as exc:  # noqa: BLE001
            return _error("mcp_install", f"读取现有 MCP 配置失败: {exc}")

        config_path = _get_credentials_path()
        try:
            save_mcp_server(
                name, command=command, args=args, url=url, env=env or None
            )
        except Exception as exc:  # noqa: BLE001
            return _error("mcp_install", f"写入 MCP 配置失败: {exc}")

        if not verify:
            _mark_recipe(context, installed=True, verified=False)
            return {
                "action_type": "mcp_install",
                "success": True,
                "name": name,
                "config_path": str(config_path),
                "tool_count": 0,
                "tools": [],
                "verified": False,
                "warnings": warnings,
            }

        registry = MCPRegistry()
        try:
            registry.add_server_pending(
                name,
                command=command or None,
                url=url or None,
                args=args,
                env=env or None,
            )
            discovered = registry.discover_tools()
            tool_count = len(discovered.get(name, []))
            if tool_count <= 0:
                raise RuntimeError("服务器已启动，但未发现任何 MCP 工具")
            tool_names = sorted(
                key for key in registry.tool_map if key.startswith(f"{name}:")
            )

            # Make the new server visible to the current session too.
            session_registry = (
                context.get("_mcp_registry") if context is not None else None
            )
            if isinstance(session_registry, MCPRegistry):
                already_loaded = (
                    name in session_registry.clients
                    or name in session_registry.get_pending_server_names()
                )
                try:
                    session_registry.add_server_pending(
                        name,
                        command=command or None,
                        url=url or None,
                        args=args,
                        env=env or None,
                    )
                    if already_loaded:
                        warnings.append(
                            "同名服务器已在当前会话中加载；请重启 Xenon 或执行 "
                            "/mcp refresh 使新配置生效"
                        )
                    else:
                        session_registry.tool_map.clear()
                except Exception:  # noqa: BLE001
                    warnings.append("当前会话未登记；请重启 Xenon 或 /mcp refresh")

            _mark_recipe(context, installed=True, verified=True)
            return {
                "action_type": "mcp_install",
                "success": True,
                "name": name,
                "config_path": str(config_path),
                "tool_count": tool_count,
                "tools": tool_names[:50],
                "verified": True,
                "warnings": warnings,
            }
        except Exception as exc:  # noqa: BLE001
            try:
                _restore_server(previous, name)
            except Exception as restore_exc:  # noqa: BLE001
                warnings.append("配置回滚失败: " + _redact_text(str(restore_exc))[:200])
            return _error(
                "mcp_install",
                str(exc),
                name=name,
                config_path=str(config_path),
                warnings=warnings,
            )
        finally:
            registry.close_all()

    def _mcp_verify(self, context: AgentContext) -> dict[str, Any]:
        """Verify one persisted MCP server with one explicit tool call."""

        from xenon.repl.provider_registry import load_mcp_servers

        name = str(
            getattr(self, "mcp_name", "")
            or getattr(self, "mcp_server", "")
            or ""
        ).strip()
        tool = str(getattr(self, "tool_name", "") or "").strip()
        args = _as_mapping(getattr(self, "tool_args", None))
        if not name:
            return _error("mcp_verify", "mcp_verify 需要 mcp_name")
        if not tool:
            return _error("mcp_verify", "mcp_verify 需要 tool_name（例如 github:get_me）")

        entry = next(
            (item for item in load_mcp_servers() if item.get("name") == name),
            None,
        )
        if entry is None:
            return _error("mcp_verify", f"未找到 MCP 配置: {name}")

        registry = MCPRegistry()
        try:
            registry.add_server_pending(
                name,
                command=str(entry.get("command") or "") or None,
                url=str(entry.get("url") or "") or None,
                args=_as_list(entry.get("args")),
                env=entry.get("env") if isinstance(entry.get("env"), dict) else None,
                headers=(
                    entry.get("headers")
                    if isinstance(entry.get("headers"), dict)
                    else None
                ),
            )
            discovered = registry.discover_tools()
            tool_count = len(discovered.get(name, []))
            full_tool = tool if ":" in tool else f"{name}:{tool}"
            result = registry.call_tool(full_tool, args)
            summary = _result_text(result)
            is_error = bool(
                isinstance(result, dict)
                and (result.get("isError") or result.get("error"))
            )
            payload: dict[str, Any] = {
                "action_type": "mcp_verify",
                "success": not is_error,
                "name": name,
                "tool": full_tool,
                "tool_count": tool_count,
                "verified": not is_error,
                "is_error": is_error,
                "result_summary": summary,
                "content": summary,
            }
            if is_error:
                payload["error"] = summary[:800]
            else:
                _mark_recipe(context, verified=True)
            return payload
        except Exception as exc:  # noqa: BLE001
            return _error("mcp_verify", str(exc), name=name, tool=tool)
        finally:
            registry.close_all()
