"""Soft verification nudge after code edits (#163).

The agent should run a test/build after changing code — the way pi / Claude
Code do — instead of treating a syntax check (or nothing) as verification.

This module decides *when* to remind it, from the tool tracker only: no new
infrastructure, no blocking gate.  The engine injects one user turn, the model
may still finish, and the reminder is never repeated in the same run.

Design constraints (see issue #163 and docs/EVAL_RESULTS.md):

* never blocking — the project deliberately downgraded semantic gates to
  reminders in v0.9.2, and the SWE-bench A/B showed blocking verification
  loops produced no measurable gain;
* repo-agnostic — only recognise common test/build commands, never guess a
  command that does not exist;
* no false positives on read-only work, failed writes, or when the last
  verification already happened after the last edit.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

# Tools that change code on disk.  Kept in sync with the write-tool family
# (file_mutation.py) without importing it, so this module stays dependency-free.
WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "write_file",
        "edit_file",
        "append_file",
        "batch_write",
        "batch_edit",
        "refactor",
        "edit_with_llm",
        "create_directory",
    }
)

# Common verification commands.  The alternative names are deliberately
# explicit: a false negative only costs one reminder, while a false positive
# would nag the model about a command that does not exist in the repo.
VERIFY_COMMAND_RE = re.compile(
    r"(?:^|[\s;&|])(?:"
    r"pytest|py\.test"
    r"|python(?:3(?:\.\d+)?)?\s+-m\s+(?:pytest|unittest|compileall)"
    r"|npm\s+(?:run\s+)?test"
    r"|yarn\s+test|pnpm\s+test"
    r"|npx\s+(?:jest|vitest|tsc)"
    r"|go\s+test|cargo\s+(?:test|check|build)"
    r"|make(?:\s|$)|tox|nox"
    r"|ruff|mypy|tsc|eslint|prettier"
    r"|mvn|gradle|dotnet\s+(?:test|build)"
    r")",
    re.IGNORECASE,
)


def _command_text(params: Any) -> str:
    if not isinstance(params, dict):
        return ""
    for key in ("action", "command", "cmd"):
        value = params.get(key)
        if value:
            return str(value)
    return ""


def is_verification_command(call: Any) -> bool:
    """True when this call is a successful-or-attempted test/build command."""

    if getattr(call, "tool_name", "") != "command":
        return False
    return bool(VERIFY_COMMAND_RE.search(_command_text(getattr(call, "params", {}))))


# Scratch files used to run a one-off query are not repo code.  Without this,
# a read-only task that writes ``.xenon_tmp/list.py`` gets a "run tests" nudge
# and may spend its final answer explaining why no test applies.
_TEMP_WRITE_MARKERS = (".xenon_tmp/", "/.xenon_tmp/", ".xenon/cache/")


def _is_temporary_write(call: Any) -> bool:
    params = getattr(call, "params", {})
    if not isinstance(params, dict):
        return False
    raw = params.get("file_path") or params.get("path") or ""
    normalized = str(raw).replace("\\", "/").lower()
    return any(marker in normalized for marker in _TEMP_WRITE_MARKERS)


def needs_verification(calls: Iterable[Any]) -> bool:
    """True when the newest successful write has no later verification command.

    Only successful writes count (a failed write needs no test run); any
    verification attempt counts, because a red test already gives the model the
    feedback it needs.  Scratch scripts under ``.xenon_tmp`` are ignored.
    """

    last_write = -1
    last_verify = -1
    for index, call in enumerate(calls):
        name = getattr(call, "tool_name", "")
        if (
            name in WRITE_TOOLS
            and getattr(call, "success", False)
            and not _is_temporary_write(call)
        ):
            last_write = index
        elif is_verification_command(call):
            # Any verification attempt counts (pass or fail): a red test already
            # gives the model the feedback it needs, and nagging again would not
            # add information.  Claiming "done" despite a red test is audited by
            # TurnGate / task_verifier, not by this reminder.
            last_verify = index
    return last_write > last_verify


VERIFICATION_REMINDER = (
    "⚠️ 你修改了文件，但之后还没有运行测试或构建来确认效果。\n"
    "请先运行与改动最相关的验证命令（优先使用仓库里已有的命令，如 pytest / npm test / "
    "go test / make），并把命令与真实输出作为证据；如果仓库没有可运行的测试，"
    "请说明原因并给出最小验证手段（如 python -m compileall）。"
)
