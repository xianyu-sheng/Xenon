"""Clickable file links and file opening helpers for the REPL."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from rich.markup import escape


@dataclass(frozen=True)
class FileTarget:
    """A local file target, optionally with editor line/column metadata."""

    path: Path
    line: int | None = None
    column: int | None = None


@dataclass(frozen=True)
class OpenFileResult:
    """Result returned after requesting a file open."""

    target: FileTarget
    command: list[str] | None
    message: str


def parse_file_target(spec: str, *, cwd: str | Path | None = None) -> FileTarget:
    """Parse ``path[:line[:column]]`` while preserving Windows drive letters."""

    text = spec.strip().strip('"').strip("'")
    if not text:
        raise ValueError("文件路径不能为空")

    raw_path = text
    line: int | None = None
    column: int | None = None

    parts = text.rsplit(":", 2)
    if len(parts) == 3 and parts[-1].isdigit() and parts[-2].isdigit():
        raw_path = parts[0]
        line = int(parts[-2])
        column = int(parts[-1])
    elif len(parts) >= 2 and parts[-1].isdigit():
        raw_path = text[: -(len(parts[-1]) + 1)]
        line = int(parts[-1])

    if raw_path.startswith("file://"):
        raw_path = _path_from_file_uri(raw_path)

    path = Path(raw_path)
    if not path.is_absolute():
        base = Path(cwd) if cwd is not None else Path.cwd()
        path = base / path

    return FileTarget(path=path, line=line, column=column)


def format_file_link(
    spec: str | Path | FileTarget,
    *,
    cwd: str | Path | None = None,
    label: str | None = None,
) -> str:
    """Return Rich markup that renders as a clickable terminal hyperlink."""

    if isinstance(spec, FileTarget):
        target = spec
    else:
        target = parse_file_target(str(spec), cwd=cwd)

    display = label or _display_target(target)
    return f"[link={target.path.resolve().as_uri()}]{escape(display)}[/link]"


def linkify_file_paths(text: str, *, cwd: str | Path | None = None) -> str:
    """Replace common source-file paths in text with clickable Rich links."""

    def replace(match: re.Match[str]) -> str:
        value = match.group(0)
        try:
            return format_file_link(value, cwd=cwd, label=value)
        except Exception:
            return value

    return _FILE_PATH_RE.sub(replace, text)


def build_editor_command(
    target: FileTarget,
    *,
    editor: str | None = None,
) -> list[str] | None:
    """Build an editor command for a target, preferring explicit config then VS Code."""

    configured = editor or os.environ.get("OMNIAGENT_EDITOR") or os.environ.get("EDITOR")
    line = str(target.line or 1)
    column = str(target.column or 1)
    file_name = str(target.path)

    if configured:
        if "{file}" in configured or "{line}" in configured or "{column}" in configured:
            rendered = configured.format(file=file_name, line=line, column=column)
            return shlex.split(rendered, posix=sys.platform != "win32")
        return shlex.split(configured, posix=sys.platform != "win32") + [file_name]

    code = shutil.which("code") or shutil.which("code.cmd")
    if code:
        return [code, "-g", f"{file_name}:{line}:{column}"]

    return None


def open_file_target(
    spec: str,
    *,
    cwd: str | Path | None = None,
    editor: str | None = None,
) -> OpenFileResult:
    """Open a file using the configured editor, VS Code, or the OS default app."""

    target = parse_file_target(spec, cwd=cwd)
    command = build_editor_command(target, editor=editor)
    launch_cwd = str(Path(cwd).resolve()) if cwd is not None else None

    if command:
        subprocess.Popen(command, cwd=launch_cwd)
        return OpenFileResult(target, command, "已用编辑器打开文件")

    if sys.platform == "win32":
        os.startfile(str(target.path))  # type: ignore[attr-defined]
        return OpenFileResult(target, None, "已用系统默认程序打开文件")

    opener = ["open"] if sys.platform == "darwin" else ["xdg-open"]
    command = opener + [str(target.path)]
    subprocess.Popen(command, cwd=launch_cwd)
    return OpenFileResult(target, command, "已用系统默认程序打开文件")


def _path_from_file_uri(uri: str) -> str:
    parsed = urlparse(uri)
    path = unquote(parsed.path)
    if sys.platform == "win32" and re.match(r"^/[A-Za-z]:/", path):
        path = path[1:]
    return path


def _display_target(target: FileTarget) -> str:
    text = str(target.path)
    if target.line is not None:
        text += f":{target.line}"
    if target.column is not None:
        text += f":{target.column}"
    return text


_FILE_PATH_RE = re.compile(
    r"(?<![\w:/\\])"
    r"(?:"
    r"(?:[A-Za-z]:\\|\.{1,2}[\\/]|[\w.@-]+[\\/])"
    r"[\w .@()\-\\/]+?"
    r"\.(?:py|js|ts|jsx|tsx|java|c|cpp|h|hpp|go|rs|rb|php|html|css|json|ya?ml|toml|xml|md|txt|sh|bat|ps1)"
    r"(?:[:]\d+(?:[:]\d+)?)?"
    r"|"
    r"[\w.@()\-]+"
    r"\.(?:py|js|ts|jsx|tsx|java|c|cpp|h|hpp|go|rs|rb|php|html|css|json|ya?ml|toml|xml|md|txt|sh|bat|ps1)"
    r"(?:[:]\d+(?:[:]\d+)?)?"
    r")"
)
