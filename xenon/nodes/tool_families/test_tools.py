"""Structured test execution without a shell.

``run_tests`` is the first-class replacement for free-form ``pytest`` shell
commands.  It accepts structured paths/args, discovers a project-local Python
interpreter when available, and invokes ``python -m pytest`` or
``python -m unittest`` directly.  The legacy ``command`` tool remains as a
compatibility fallback for projects with custom runners.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _as_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, list):
                return [str(item) for item in parsed if str(item).strip()]
        return [raw]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def _python_candidates(workspace: Path) -> list[Path]:
    if sys.platform == "win32":
        names = (
            Path(".venv") / "Scripts" / "python.exe",
            Path("venv") / "Scripts" / "python.exe",
            Path("env") / "Scripts" / "python.exe",
        )
    else:
        names = (
            Path(".venv") / "bin" / "python",
            Path("venv") / "bin" / "python",
            Path("env") / "bin" / "python",
        )
    return [workspace / name for name in names]


def _resolve_interpreter(workspace: Path, explicit: str) -> tuple[Path, str]:
    if explicit:
        candidate = Path(explicit).expanduser()
        if not candidate.is_absolute():
            candidate = workspace / candidate
        candidate = candidate.resolve()
        if not candidate.is_file():
            raise FileNotFoundError(f"指定的解释器不存在: {explicit}")
        return candidate, "explicit"

    for candidate in _python_candidates(workspace):
        if candidate.is_file():
            return candidate, "project"

    # sys.executable is the safe fallback.  It may not contain the project's
    # dependencies, but silently guessing another global interpreter is worse.
    return Path(sys.executable), "runtime"


def _strip_node_suffix(path: str) -> str:
    """``tests/test_x.py::test_y`` refers to a real path before ``::``."""
    return path.split("::", 1)[0]


def _looks_like_file_path(value: str) -> bool:
    return (
        value.endswith(".py")
        or "/" in value
        or "\\" in value
        or value in {".", ".."}
    )


class TestToolsMixin:
    """Mixin providing the built-in structured ``run_tests`` action."""

    def _run_tests(self, context: Any) -> dict[str, Any]:
        workspace = Path(getattr(self, "cwd", None) or Path.cwd()).resolve()
        if not workspace.is_dir():
            return self._run_tests_error(
                "invalid_cwd", f"测试 cwd 不存在或不是目录: {workspace}"
            )

        framework = str(
            getattr(self, "test_framework", "auto") or "auto"
        ).strip().lower()
        if framework not in {"auto", "pytest", "unittest", "custom"}:
            return self._run_tests_error(
                "unsupported_framework",
                f"不支持的 test_framework: {framework}（可选: auto, pytest, unittest, custom）",
            )

        paths = _as_list(getattr(self, "test_paths", None))
        args = _as_list(getattr(self, "test_args", None))
        raw_custom = getattr(self, "test_command", None)
        custom = _as_list(raw_custom)
        if isinstance(raw_custom, str) and raw_custom.strip() and not raw_custom.startswith("["):
            try:
                custom = shlex.split(raw_custom)
            except ValueError:
                custom = [raw_custom]
        if framework == "auto":
            framework = "custom" if custom else "pytest"

        timeout = getattr(self, "timeout", 60)
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError):
            timeout_value = 60.0
        if timeout_value <= 0:
            timeout_value = 60.0

        if framework != "custom":
            missing = [
                path
                for path in paths
                if _looks_like_file_path(_strip_node_suffix(path))
                and not (workspace / _strip_node_suffix(path)).exists()
            ]
            if missing:
                return self._run_tests_error(
                    "missing_test_path",
                    "测试路径不存在: " + ", ".join(missing),
                    framework=framework,
                    test_paths=paths,
                )
            if getattr(self, "security_enabled", True):
                for path in paths:
                    base = _strip_node_suffix(path)
                    if not _looks_like_file_path(base):
                        continue
                    try:
                        self._validate_path(base, for_write=False)
                    except Exception as exc:  # noqa: BLE001 — SecurityError
                        return self._run_tests_error(
                            "security_error",
                            f"测试路径越界或被拒绝: {path} ({exc})",
                            framework=framework,
                            test_paths=paths,
                        )

        try:
            interpreter, interpreter_source = _resolve_interpreter(
                workspace, str(getattr(self, "interpreter", "") or "").strip()
            )
        except FileNotFoundError as exc:
            return self._run_tests_error(
                "missing_interpreter", str(exc), framework=framework
            )

        if framework == "custom":
            if not custom:
                return self._run_tests_error(
                    "missing_custom_command",
                    "framework=custom 时必须提供 test_command（argv 列表或命令字符串）",
                    framework=framework,
                )
            argv = custom + args + paths
            # Custom commands are executed as an argv vector, never through a
            # shell.  This preserves quoting and avoids platform shell parsing.
            executable = argv[0]
            if not Path(executable).is_absolute() and (
                "/" in executable or "\\" in executable
            ):
                argv[0] = str(workspace / executable)
        elif framework == "pytest":
            argv = [str(interpreter), "-m", "pytest", *paths, *args]
        else:  # unittest
            argv = [str(interpreter), "-m", "unittest", *paths, *args]

        command_label = " ".join(shlex.quote(part) for part in argv)
        started = time.monotonic()
        env = dict(os.environ)
        # Test bytecode is not a deliverable and used to make the worktree
        # dirty after an otherwise successful verification.
        env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        try:
            proc = subprocess.run(
                argv,
                cwd=str(workspace),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_value,
                env=env,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            duration = round(time.monotonic() - started, 3)
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", "replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", "replace")
            return {
                "action_type": "run_tests",
                "framework": framework,
                "command": command_label,
                "argv": argv,
                "cwd": str(workspace),
                "interpreter": str(interpreter),
                "interpreter_source": interpreter_source,
                "test_paths": paths,
                "returncode": -1,
                "exit_code": -1,
                "duration": duration,
                "stdout": stdout,
                "stderr": stderr,
                "content": (stdout + stderr).strip(),
                "success": False,
                "error_kind": "timeout",
                "error": f"测试执行超时 ({timeout_value:g}s): {command_label}",
            }
        except OSError as exc:
            duration = round(time.monotonic() - started, 3)
            return {
                **self._run_tests_error_payload(
                    "launch_error", f"测试进程启动失败: {exc}", framework=framework
                ),
                "command": command_label,
                "argv": argv,
                "cwd": str(workspace),
                "interpreter": str(interpreter),
                "interpreter_source": interpreter_source,
                "test_paths": paths,
                "duration": duration,
            }

        duration = round(time.monotonic() - started, 3)
        output = (proc.stdout or "") + (proc.stderr or "")
        if output.strip() and hasattr(self, "_write_output") and context is not None:
            self._write_output(context, proc.stdout.strip())
        result: dict[str, Any] = {
            "action_type": "run_tests",
            "framework": framework,
            "command": command_label,
            "argv": argv,
            "cwd": str(workspace),
            "interpreter": str(interpreter),
            "interpreter_source": interpreter_source,
            "test_paths": paths,
            "returncode": proc.returncode,
            "exit_code": proc.returncode,
            "duration": duration,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "content": output.strip(),
            "success": proc.returncode == 0,
        }
        if proc.returncode != 0:
            result["error_kind"] = "test_failure"
            result["error"] = output.strip()[-800:] or f"退出码 {proc.returncode}"
        return result

    @staticmethod
    def _run_tests_error_payload(
        kind: str, message: str, *, framework: str = "auto"
    ) -> dict[str, Any]:
        return {
            "action_type": "run_tests",
            "framework": framework,
            "success": False,
            "content": "",
            "error_kind": kind,
            "error": message,
        }

    def _run_tests_error(
        self,
        kind: str,
        message: str,
        *,
        framework: str = "auto",
        test_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        payload = self._run_tests_error_payload(
            kind, message, framework=framework
        )
        payload["test_paths"] = list(test_paths or [])
        return payload
