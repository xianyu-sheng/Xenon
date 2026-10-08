"""Local hooks (CC-compatible event names, zero cloud dependency).

Config: ``~/.xenon/hooks.yaml`` (user) and ``<cwd>/.xenon/hooks.yaml``
(project), merged in that order.  ``XENON_HOOKS=0`` disables hooks.

Events and their contract (mirroring Claude Code):

* PreToolUse    exit 0 = allow; exit 2 = block (stderr is the reason);
                any other non-zero = allow, stderr shown as a warning.
* PostToolUse   exit 2 = stderr is fed back as tool context; else ignore.
* Stop          exit 0 = request the agent to stop; stderr shown otherwise.
* SubagentStop  same contract as Stop for subagents (fires once subagents
                v1 lands; the name is reserved).

Every hook receives one JSON object on stdin:
{event, tool_name, tool_input, session_id, ...extra}.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_EVENTS = ("PreToolUse", "PostToolUse", "Stop", "SubagentStop")


@dataclass
class HookOutcome:
    blocked: bool = False
    stop: bool = False
    message: str = ""


def _disabled() -> bool:
    return os.environ.get("XENON_HOOKS") == "0"


@dataclass
class HookRunner:
    """Loads hook config and runs matching commands."""

    user_dir: Path | None = None
    project_dir: Path | None = None
    timeout: float = 10.0
    session_id: str = ""
    _hooks: dict[str, list[dict]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if _disabled():
            self._hooks = {}
            return
        for directory in (self.user_dir, self.project_dir):
            self._load(directory)

    def _load(self, directory: Path | None) -> None:
        if directory is None or not directory.exists():
            return
        try:
            import yaml  # type: ignore[import-untyped]

            data = yaml.safe_load(directory.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001 — 配置损坏只警告
            logger.warning("hooks 配置读取失败 %s: %s", directory, exc)
            return
        if not isinstance(data, dict):
            return
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            return
        for event in _EVENTS:
            entries = hooks.get(event) or []
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict) or not entry.get("command"):
                    continue
                self._hooks.setdefault(event, []).append(
                    {
                        "match": str(entry.get("match") or "*"),
                        "command": str(entry["command"]),
                    }
                )

    @staticmethod
    def _matches(pattern: str, tool_name: str) -> bool:
        import fnmatch

        return fnmatch.fnmatch(tool_name, pattern) or pattern == "*"

    def run(
        self,
        event: str,
        tool_name: str,
        tool_input: dict | None = None,
        *,
        extra: dict | None = None,
    ) -> HookOutcome:
        if event not in _EVENTS or not self._hooks.get(event):
            return HookOutcome()
        payload = json.dumps(
            {
                "event": event,
                "tool_name": tool_name,
                "tool_input": tool_input or {},
                "session_id": self.session_id,
                **(extra or {}),
            },
            ensure_ascii=False,
        )
        messages: list[str] = []
        blocked = False
        stop = False
        for entry in self._hooks[event]:
            if not self._matches(entry["match"], tool_name):
                continue
            try:
                proc = subprocess.run(
                    entry["command"],
                    shell=True,
                    input=payload,
                    text=True,
                    capture_output=True,
                    timeout=self.timeout,
                )
            except subprocess.TimeoutExpired:
                messages.append(f"hook 超时: {entry['command']}")
                continue
            except OSError as exc:
                messages.append(f"hook 启动失败: {exc}")
                continue
            stderr = (proc.stderr or "").strip()
            if event == "PreToolUse":
                if proc.returncode == 2:
                    blocked = True
                    if stderr:
                        messages.append(stderr)
                elif proc.returncode != 0 and stderr:
                    messages.append(stderr)
            elif event == "PostToolUse":
                if proc.returncode == 2 and stderr:
                    messages.append(stderr)
            elif event in ("Stop", "SubagentStop"):
                if proc.returncode == 0:
                    stop = True
                elif stderr:
                    messages.append(stderr)
        return HookOutcome(blocked=blocked, stop=stop, message="\n".join(messages))


_default_runner: HookRunner | None = None


def set_default_runner(runner: HookRunner | None) -> None:
    global _default_runner
    _default_runner = runner


def get_default_runner() -> HookRunner | None:
    return _default_runner
