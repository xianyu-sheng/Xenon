"""REPL shell command execution helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omniagent.engine.context import AgentContext
from omniagent.nodes.tool_node import SecurityError, ToolNode


@dataclass(frozen=True)
class ShellResult:
    """Normalized shell execution result for REPL rendering and context storage."""

    command: str
    returncode: int | None
    stdout: str
    stderr: str
    success: bool
    error: str = ""

    @property
    def combined_output(self) -> str:
        parts = []
        if self.stdout.strip():
            parts.append(self.stdout.rstrip())
        if self.stderr.strip():
            parts.append("STDERR:\n" + self.stderr.rstrip())
        if self.error:
            parts.append("ERROR:\n" + self.error)
        return "\n\n".join(parts).strip()


def run_shell_command(
    command: str,
    *,
    cwd: str | Path | None = None,
    timeout: int = 120,
    context: AgentContext | None = None,
) -> ShellResult:
    """Run a shell command through ToolNode so existing safety checks still apply."""

    clean = command.strip()
    if not clean:
        return ShellResult(command=command, returncode=None, stdout="", stderr="", success=False, error="命令不能为空")

    node = ToolNode(
        "repl_shell",
        action_type="command",
        action=clean,
        cwd=str(cwd) if cwd is not None else str(Path.cwd()),
        timeout=timeout,
        output_slot="_last_shell_stdout",
    )
    ctx = context or AgentContext()

    try:
        raw = node.execute(ctx)
    except SecurityError as exc:
        return ShellResult(command=clean, returncode=None, stdout="", stderr="", success=False, error=str(exc))
    except Exception as exc:
        return ShellResult(command=clean, returncode=None, stdout="", stderr="", success=False, error=str(exc))

    return _from_tool_result(clean, raw)


def format_shell_result(result: ShellResult, *, max_chars: int = 8000) -> str:
    """Format shell output for a Rich panel."""

    status = "OK" if result.success else "FAILED"
    code = result.returncode if result.returncode is not None else "-"
    output = result.combined_output or "(no output)"
    if len(output) > max_chars:
        output = output[:max_chars] + "\n\n... output truncated ..."
    return f"$ {result.command}\nstatus: {status} | exit: {code}\n\n{output}"


def _from_tool_result(command: str, raw: dict[str, Any]) -> ShellResult:
    return ShellResult(
        command=str(raw.get("command") or command),
        returncode=raw.get("returncode"),
        stdout=str(raw.get("stdout") or ""),
        stderr=str(raw.get("stderr") or ""),
        success=bool(raw.get("success")),
        error=str(raw.get("error") or ""),
    )
