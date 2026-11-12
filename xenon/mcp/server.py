"""MCP server: expose Xenon's kernel and evidence to other agents.

The product's claim is evidence, not trust.  Being called over MCP must not
become a way around that: every task runs through the same kernel, leaves the
same append-only ledger, and reports the same evidence labels a human would
see.  The server is deliberately small:

* ``xenon_run_task``  — run one task, return ledger + evidence;
* ``xenon_evidence``  — read a ledger back as claims/receipts;
* ``xenon_audit``     — audit another agent's transcript against a workspace.

Transport is JSON-RPC 2.0 over stdio, one JSON object per line.  stdout carries
protocol traffic only; every diagnostic goes to stderr.

Permissions are fail-closed: the server starts read-only unless the operator
passes ``--permission auto-edit`` (or higher), a tool call may lower but never
raise that ceiling, and a task that needs an approval nobody can answer is
denied by the kernel instead of hanging.
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from xenon import __version__

SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
PROTOCOL_VERSION = SUPPORTED_PROTOCOLS[0]

#: What the operator will allow; a request can only ask for less.
PERMISSION_RANK = {
    "read-only": 0,
    "auto-edit": 1,
    "scoped": 2,
    "bypass": 3,
}

TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "xenon_run_task",
        "description": (
            "Run one task through Xenon's kernel in a workspace. Every call "
            "writes an append-only ledger; the response returns the task id, "
            "phase, evidence status, answer, debt and the ledger path. "
            "Permission is capped by how the server was started."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "objective": {"type": "string", "description": "What to do."},
                "workspace": {
                    "type": "string",
                    "description": (
                        "Workspace root. Must stay inside the server's workspace "
                        "unless it was started with --allow-any-workspace."
                    ),
                },
                "permission": {
                    "type": "string",
                    "enum": list(PERMISSION_RANK),
                    "description": "Optional; may lower but never raise the server ceiling.",
                },
                "max_steps": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Step budget for the loop (default: kernel default).",
                },
            },
            "required": ["objective"],
            "additionalProperties": False,
        },
    },
    {
        "name": "xenon_evidence",
        "description": (
            "Read a Xenon ledger and return its report: phase, evidence status, "
            "claims bound to receipts, what actually ran, and integrity."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ledger": {
                    "type": "string",
                    "description": "Path to the ledger .jsonl returned by xenon_run_task.",
                }
            },
            "required": ["ledger"],
            "additionalProperties": False,
        },
    },
    {
        "name": "xenon_audit",
        "description": (
            "Audit another agent's Claude Code transcript against a workspace. "
            "The transcript only provides claims; Xenon's own probe provides the "
            "facts. Claims outside the workspace are reported as out_of_scope, "
            "not as contradictions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "transcript": {
                    "type": "string",
                    "description": "Path to the agent's .jsonl transcript.",
                },
                "workspace": {
                    "type": "string",
                    "description": "Workspace to compare against (default: server workspace).",
                },
                "base": {
                    "type": "string",
                    "description": "Git commit the session started from (optional).",
                },
                "agent": {
                    "type": "string",
                    "description": "Adapter name when the format is not auto-detected.",
                },
            },
            "required": ["transcript"],
            "additionalProperties": False,
        },
    },
)


class ToolError(Exception):
    """A tool call that cannot proceed; reported as ``isError`` content."""


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


class McpServer:
    """Protocol dispatch with no I/O of its own (``serve`` owns the loop)."""

    def __init__(
        self,
        *,
        workspace: str = ".",
        permission: str = "read-only",
        models: Sequence[str] | None = None,
        ledger_dir: str | None = None,
        allow_any_workspace: bool = False,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        ceiling = str(permission or "read-only").strip().lower().replace("_", "-")
        self.permission = ceiling if ceiling in PERMISSION_RANK else "read-only"
        self.models = [str(item) for item in (models or ()) if str(item).strip()]
        self.ledger_dir = ledger_dir
        self.allow_any_workspace = allow_any_workspace

    # ── protocol ────────────────────────────────────────────────────

    def handle(self, request: Mapping[str, Any]) -> dict[str, Any] | None:
        """Handle one JSON-RPC message; notifications produce no response."""

        method = request.get("method")
        request_id = request.get("id")
        is_notification = "id" not in request
        params = request.get("params")
        params = params if isinstance(params, Mapping) else {}

        if is_notification:
            # notifications/initialized, canceled, ... need no reply.
            return None

        try:
            if method == "initialize":
                result = self._initialize(params)
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": list(TOOLS)}
            elif method == "tools/call":
                return self._call_tool(request_id, params)
            elif method == "shutdown":
                result = {}
            else:
                return _error(request_id, -32601, f"method not found: {method}")
        except Exception as exc:  # noqa: BLE001 — a bad request is not fatal
            return _error(request_id, -32603, f"{type(exc).__name__}: {exc}")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _initialize(self, params: Mapping[str, Any]) -> dict[str, Any]:
        requested = str(params.get("protocolVersion") or "")
        version = (
            requested if requested in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION
        )
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "xenon", "version": __version__},
        }

    def _call_tool(
        self, request_id: Any, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        name = str(params.get("name") or "")
        arguments = params.get("arguments")
        arguments = arguments if isinstance(arguments, Mapping) else {}
        # Kill anything a library prints to stdout: stdout is the protocol.
        with contextlib.redirect_stdout(sys.stderr):
            try:
                text = self.dispatch(name, arguments)
                is_error = False
            except ToolError as exc:
                text, is_error = str(exc), True
            except Exception as exc:  # noqa: BLE001 — report, do not crash the server
                text = f"{type(exc).__name__}: {exc}"
                is_error = True
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": text}],
                "isError": is_error,
            },
        }

    # ── tools ───────────────────────────────────────────────────────

    def dispatch(self, name: str, arguments: Mapping[str, Any]) -> str:
        if name == "xenon_run_task":
            return self._run_task(arguments)
        if name == "xenon_evidence":
            return self._evidence(arguments)
        if name == "xenon_audit":
            return self._audit(arguments)
        raise ToolError(f"unknown tool: {name}")

    def _workspace_for(self, raw: Any) -> Path:
        if not raw:
            return self.workspace
        candidate = Path(str(raw)).expanduser().resolve()
        if not candidate.is_dir():
            raise ToolError(f"workspace is not a directory: {candidate}")
        if not self.allow_any_workspace:
            try:
                candidate.relative_to(self.workspace)
            except ValueError:
                raise ToolError(
                    f"workspace escapes the server root {self.workspace}: {candidate}"
                ) from None
        return candidate

    def _permission_for(self, raw: Any) -> str:
        requested = str(raw or self.permission).strip().lower().replace("_", "-")
        if requested not in PERMISSION_RANK:
            raise ToolError(
                f"unknown permission {requested!r}; expected one of "
                f"{', '.join(PERMISSION_RANK)}"
            )
        if PERMISSION_RANK[requested] > PERMISSION_RANK[self.permission]:
            raise ToolError(
                f"requested permission {requested!r} exceeds the server ceiling "
                f"{self.permission!r}"
            )
        return requested

    def _run_task(self, arguments: Mapping[str, Any]) -> str:
        objective = str(arguments.get("objective") or "").strip()
        if not objective:
            raise ToolError("objective is required")
        workspace = self._workspace_for(arguments.get("workspace"))
        permission = self._permission_for(arguments.get("permission"))
        max_steps = arguments.get("max_steps")
        if max_steps is not None:
            try:
                max_steps = int(max_steps)
            except (TypeError, ValueError):
                raise ToolError("max_steps must be an integer") from None
            if not 1 <= max_steps <= 100:
                raise ToolError("max_steps must be between 1 and 100")

        from xenon.harness.llm import build_llm_loop_strategy, resolve_model_config
        from xenon.harness.runner import run_task
        from xenon.kernel.policies import PermissionMode
        from xenon.kernel.strategies import DEFAULT_PROFILE
        from xenon.repl.system_config import get_config
        from xenon.sandbox.backends import build_runtime, spec_from_config

        try:
            model_config = resolve_model_config(self.models or None)
        except Exception as exc:  # noqa: BLE001 — say how to fix it
            raise ToolError(
                f"no usable model configured: {exc}. Run `xenon /setup` or pass "
                "`--model provider/model` when starting the server."
            ) from None

        tool_runtime = build_runtime(
            str(workspace), spec_from_config(get_config().sandbox)
        )
        kwargs: dict[str, Any] = {
            "strategy": build_llm_loop_strategy(model_config, DEFAULT_PROFILE),
            "workspace": str(workspace),
            "ledger_dir": self.ledger_dir,
            "permission": PermissionMode(permission),
            "max_steps": max_steps if max_steps is not None else 20,
            "constraints": (f"model:{model_config.model_id}",),
            "sink": None,
            "tool_runtime": tool_runtime,
        }
        run = run_task(objective, **kwargs)
        result = run.result
        payload = {
            "task_id": result.task_id,
            "phase": result.phase.value,
            "accepted": bool(result.accepted),
            "evidence": result.evidence_status.value,
            "answer": result.answer,
            "stop_signal": result.stop_signal,
            "ledger": str(run.ledger_path),
            "debt": [
                {
                    "kind": str(getattr(item.obligation, "kind", "")),
                    "reason": item.reason,
                    "acceptable": bool(item.acceptable),
                }
                for item in result.debt
            ],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _evidence(self, arguments: Mapping[str, Any]) -> str:
        raw = str(arguments.get("ledger") or "").strip()
        if not raw:
            raise ToolError("ledger is required")
        path = Path(raw).expanduser()
        if not path.is_file():
            raise ToolError(f"ledger not found: {path}")
        from xenon.report import build_report

        report = build_report(path)
        return json.dumps(report, ensure_ascii=False, indent=2, default=str)

    def _audit(self, arguments: Mapping[str, Any]) -> str:
        raw = str(arguments.get("transcript") or "").strip()
        if not raw:
            raise ToolError("transcript is required")
        transcript = Path(raw).expanduser()
        if not transcript.is_file():
            raise ToolError(f"transcript not found: {transcript}")
        workspace = self._workspace_for(arguments.get("workspace"))
        from xenon.external import audit, to_report

        result = audit(
            transcript,
            workspace=workspace,
            base=str(arguments.get("base") or "") or None,
            agent=str(arguments.get("agent") or ""),
        )
        return json.dumps(
            to_report(result), ensure_ascii=False, indent=2, default=str
        )


def serve(
    *,
    workspace: str = ".",
    permission: str = "read-only",
    models: Sequence[str] | None = None,
    ledger_dir: str | None = None,
    allow_any_workspace: bool = False,
) -> int:
    """Run the stdio loop until stdin closes."""

    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

    server = McpServer(
        workspace=workspace,
        permission=permission,
        models=models,
        ledger_dir=ledger_dir,
        allow_any_workspace=allow_any_workspace,
    )
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            response: dict[str, Any] | None = _error(None, -32700, f"parse error: {exc}")
        else:
            if not isinstance(request, Mapping):
                response = _error(None, -32600, "request must be an object")
            else:
                response = server.handle(request)
        if response is not None:
            out.write(json.dumps(response, ensure_ascii=False) + "\n")
            out.flush()
    return 0


__all__ = ["McpServer", "TOOLS", "ToolError", "serve", "PROTOCOL_VERSION"]
