"""One place that decides which paths are sensitive (#449).

Three subsystems used to keep their own answer and the answers drifted:

* ``xenon.nodes.tool_node._SENSITIVE_PATHS`` — the **write blacklist**, a
  security boundary (writes there are refused);
* ``xenon.frontend.queries`` — the dashboard **attention projection** (#446):
  a read of ``.env`` or ``/etc/passwd`` is promoted instead of folded away;
* ``xenon.nodes.tool_security`` — prompt-injection **phrase** detection.  Its
  regexes match natural-language requests ("read the .env", "send the
  credentials") in tool output, not paths, so they are deliberately *not*
  derived from this module.

The lists live here; ``tool_node`` and ``queries`` reference them.  This module
only classifies strings: no file-system access, no ``resolve()``, no imports
beyond the standard library, so the frontend can use it without loading the
nodes layer.

Classification here is for attention and audit.  It is **not** the security
boundary: the write blacklist check in ``tool_node`` keeps its own matching
logic, and nothing in :func:`classify_path` may be used to allow a write.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import PurePath

__all__ = [
    "CREDENTIAL_NAME_PATTERNS",
    "SYSTEM_PATHS",
    "SYSTEM_WRITE_BLACKLIST",
    "classify_path",
    "matches_credential_name",
]

#: Prefixes the write path refuses (security boundary — only ever grow this).
#: Kept in the original spelling: ``tool_node`` normalises them itself.
SYSTEM_WRITE_BLACKLIST: tuple[str, ...] = (
    "c:\\windows",
    "c:\\program files",
    "c:\\programdata",
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/boot",
    "/dev",
    "/proc",
    "/sys",
    "/var/log",
    "/root/.ssh",
    "/root/.gnupg",
)

#: Prefixes that are a *boundary event* when touched at all.  A superset of the
#: write blacklist: reading anywhere under ``/root`` deserves attention, but
#: refusing every write there would break containers that work in ``/root``.
SYSTEM_PATHS: tuple[str, ...] = SYSTEM_WRITE_BLACKLIST + ("/root",)

#: Credential-looking names, matched against a normalised (``/``, lower-case)
#: path or bare file name.  A credential read is a privacy event wherever the
#: file lives, so these win over the system / outside classes.
CREDENTIAL_NAME_PATTERNS: tuple[str, ...] = (
    r"(^|/)\.env(\.|$|/)",
    r"(^|/)id_(rsa|dsa|ecdsa|ed25519)(\.|$)",
    r"\.(pem|key|p12|pfx|jks)$",
    r"(^|/)(credentials|secrets?|passwords?|[._]?netrc)(\.|$|/)",
    r"(^|/)\.(aws|ssh|gnupg|kube|docker)(/|$)",
    r"(^|/)\.git-credentials$",
)
_CREDENTIAL_RE = tuple(re.compile(pattern) for pattern in CREDENTIAL_NAME_PATTERNS)

_DRIVE_RE = re.compile(r"^[a-z]:(/|$)")


def _normalise(path: str | PurePath | None) -> str:
    """Forward slashes, lower case, ``.``/``..`` folded; no I/O."""

    text = str(path or "").strip().replace("\\", "/").lower()
    if not text:
        return ""
    # ``//server/share`` must keep its leading pair; normpath does that.
    folded = posixpath.normpath(text)
    return "" if folded == "." else folded


def _is_absolute(text: str) -> bool:
    return text.startswith(("/", "~")) or bool(_DRIVE_RE.match(text))


def _under(text: str, prefix: str) -> bool:
    root = _normalise(prefix).rstrip("/")
    return text == root or text.startswith(root + "/")


def matches_credential_name(name: str | PurePath) -> bool:
    """Whether a file name (or path) looks like it holds credentials."""

    text = _normalise(name)
    return bool(text) and any(pattern.search(text) for pattern in _CREDENTIAL_RE)


def classify_path(
    path: str | PurePath, *, workspace: str | PurePath | None = None
) -> str:
    """返回 "" | "credential" | "system" | "outside"。

    Order matters: a credential is reported as such even inside the workspace
    or under a system directory.  System prefixes are matched by path
    component from the root (``/usr/x`` yes, ``/home/u/usr/x`` and ``/usrx``
    no).  ``outside`` needs a workspace; a relative path is taken to be
    relative to it, unless it climbs out with ``..``.  Windows and POSIX
    spellings are both accepted and compared case-insensitively.
    """

    text = _normalise(path)
    if not text:
        return ""
    if matches_credential_name(text):
        return "credential"
    if any(_under(text, prefix) for prefix in SYSTEM_PATHS):
        return "system"
    if workspace is not None:
        root = _normalise(workspace).rstrip("/")
        if root:
            if not _is_absolute(text):
                if text == ".." or text.startswith("../"):
                    return "outside"
                return ""
            if not _under(text, root):
                return "outside"
    return ""
