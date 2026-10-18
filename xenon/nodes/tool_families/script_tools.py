"""Safe-ish inline script execution without shell quoting (C4).

``python -c`` and PowerShell ``$()`` are blocked at the shell boundary because
they hide a payload from the visible-command scanner.  The same code can be
written to a workspace script and executed directly, so this tool provides that
path explicitly instead of making the model fight shell quoting.

This is execution convenience, not a sandbox: the command tool already runs
arbitrary commands under the permission gate, and this tool is registered as
SENSITIVE for the same approval semantics.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Any

_VALID_LANGUAGES = {"python", "powershell", "pwsh"}


def _looks_like_powershell(code: str) -> bool:
    """Best-effort detection for PowerShell snippets passed without language.

    A ``$`` variable or a common cmdlet at line start is not valid Python, so
    switching is safe. This matters because the model often sends
    ``run_script(code=...)`` without the optional language field.
    """
    stripped = code.lstrip()
    if stripped.startswith("$") or "$env:" in code:
        return True
    markers = (
        "Write-Output",
        "Write-Host",
        "Join-Path",
        "Set-Location",
        "Get-ChildItem",
        "New-Item",
        "Remove-Item",
        "Select-Object",
        "Out-Null",
        "ErrorActionPreference",
    )
    return any(marker in code for marker in markers)


class ScriptToolsMixin:
    """Mixin providing the built-in ``run_script`` action."""

    def _run_script(self, context: Any) -> dict[str, Any]:
        language = str(getattr(self, "language", "python") or "python").strip().lower()
        # Direct ToolNode construction may use the weather-style ``lang`` alias.
        raw_lang = str(getattr(self, "lang", "") or "").strip().lower()
        if language == "python" and raw_lang in _VALID_LANGUAGES:
            language = raw_lang
        code = str(getattr(self, "code", "") or "")
        if language == "python" and _looks_like_powershell(code):
            language = "powershell"
        if language not in _VALID_LANGUAGES:
            return {
                "action_type": "run_script",
                "success": False,
                "content": "",
                "error": f"不支持的 language: {language}（可选: python, powershell）",
            }
        if not code.strip():
            return {
                "action_type": "run_script",
                "success": False,
                "content": "",
                "error": "run_script 需要非空 code",
            }

        workdir = Path(getattr(self, "cwd", None) or Path.cwd()).resolve()
        temp_dir = workdir / ".xenon_tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        suffix = ".py" if language == "python" else ".ps1"
        digest = hashlib.sha256(code.encode("utf-8")).hexdigest()[:12]
        script = temp_dir / f"run_{digest}{suffix}"
        script.write_text(code, encoding="utf-8")

        if language == "python":
            argv = [sys.executable, str(script)]
        else:
            argv = [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
            ]

        try:
            proc = subprocess.run(
                argv,
                cwd=str(workdir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=getattr(self, "timeout", 60),
            )
        except FileNotFoundError as exc:
            return {
                "action_type": "run_script",
                "success": False,
                "content": "",
                "language": language,
                "script": str(script),
                "error": f"{language} 不可用: {exc}",
            }
        except subprocess.TimeoutExpired:
            return {
                "action_type": "run_script",
                "success": False,
                "content": "",
                "language": language,
                "script": str(script),
                "error": f"run_script 超时 ({getattr(self, 'timeout', 60)}s)",
            }

        output = (proc.stdout or "") + (proc.stderr or "")
        return {
            "action_type": "run_script",
            "language": language,
            "script": str(script),
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "content": output.strip(),
            "success": proc.returncode == 0,
            "error": None if proc.returncode == 0 else output.strip()[:500],
        }
