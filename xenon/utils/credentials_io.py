"""One reader/writer for the credentials document.

The file used to be plaintext YAML, and six modules read it directly.  Adding
encryption in one place would have protected one sixth of the paths, so every
reader goes through this module and the format is decided once.

Formats (first line is a YAML comment, so an unaware reader still parses the
plaintext case):

* ``# xenon-credentials: dpapi/v1`` — Windows DPAPI ciphertext, base64.  The
  key is bound to the current user and machine; the file is useless elsewhere.
* ``# xenon-credentials: keyring/v1`` — the document lives in the OS keyring;
  the file only marks that fact.
* ``# xenon-credentials: plaintext/v1`` — fallback.  Mode 0600, no encryption;
  :func:`describe` reports it, and the file says so too.

Migration is automatic: reading a plaintext document on a platform with a real
cipher rewrites it encrypted.  No plaintext ``.bak`` is left behind.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import sys
from pathlib import Path
from typing import Any

import yaml

from xenon.utils.atomic_write import atomic_write_text

logger = logging.getLogger(__name__)

MAGIC = "# xenon-credentials: "
_KEYRING_SERVICE = "xenon-credentials"


def default_credentials_path() -> Path:
    override = os.environ.get("XENON_CREDENTIALS_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".xenon" / "credentials.yaml"


# ── Windows DPAPI ────────────────────────────────────────────────────────────


def _dpapi(protect: bool, blob: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    buffer = ctypes.create_string_buffer(blob, len(blob))
    source = DATA_BLOB(
        len(blob), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))
    )
    target = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not function(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)
    ):
        raise OSError(ctypes.GetLastError(), "DPAPI call failed")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


# ── keyring (optional) ───────────────────────────────────────────────────────


def _keyring() -> Any | None:
    try:
        import keyring  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 — optional dependency
        return None
    return keyring


def _keyring_user(path: Path) -> str:
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
    return digest[:16]


# ── format detection / selection ─────────────────────────────────────────────


def _preferred_cipher() -> str:
    if sys.platform == "win32":
        return "dpapi"
    if _keyring() is not None:
        return "keyring"
    return "plaintext"


def _split(text: str) -> tuple[str, str]:
    first, separator, rest = text.partition("\n")
    if first.startswith(MAGIC):
        return first[len(MAGIC) :].strip().split("/", 1)[0], rest
    return "plaintext", text


def _decode(cipher: str, body: str, path: Path) -> str:
    if cipher == "dpapi":
        raw = base64.b64decode(body.strip() or b"")
        return _dpapi(False, raw).decode("utf-8", "replace")
    if cipher == "keyring":
        keyring = _keyring()
        if keyring is None:
            raise RuntimeError("keyring is not installed")
        return str(
            keyring.get_password(_KEYRING_SERVICE, _keyring_user(path)) or ""
        )
    return body


# ── public API ───────────────────────────────────────────────────────────────


def load_document(path: str | Path | None = None) -> dict[str, Any]:
    """Read the credentials mapping; missing or unreadable means empty."""

    target = Path(path) if path is not None else default_credentials_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        logger.warning("凭证文件读取失败，按空处理: %s (%s)", target, exc)
        return {}

    cipher, body = _split(text)
    try:
        decoded = _decode(cipher, body, target)
        data = yaml.safe_load(decoded) or {}
    except Exception as exc:  # noqa: BLE001 — a bad file must not crash startup
        logger.warning("凭证文件无法解析，按空处理: %s (%s)", target, exc)
        return {}

    if not isinstance(data, dict):
        logger.warning("凭证文件不是 YAML 映射，忽略其内容: %s", target)
        return {}

    # Opportunistic migration: a plaintext document is rewritten encrypted when
    # this platform has a real cipher.  Best effort -- a read must still work.
    if cipher == "plaintext" and _preferred_cipher() != "plaintext":
        try:
            save_document(target, data)
        except Exception:  # noqa: BLE001
            logger.debug("凭证加密迁移失败（保持可读）", exc_info=True)
    return data


def save_document(
    path: str | Path | None, data: dict[str, Any]
) -> str:
    """Write the credentials mapping; returns the cipher that was used."""

    target = Path(path) if path is not None else default_credentials_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.dump(data, allow_unicode=True, default_flow_style=False)
    cipher = _preferred_cipher()

    if cipher == "dpapi":
        encrypted = base64.b64encode(_dpapi(True, text.encode("utf-8"))).decode(
            "ascii"
        )
        content = f"{MAGIC}dpapi/v1\n{encrypted}\n"
    elif cipher == "keyring":
        keyring = _keyring()
        try:
            assert keyring is not None
            keyring.set_password(
                _KEYRING_SERVICE, _keyring_user(target), text
            )
            content = f"{MAGIC}keyring/v1\n\n"
        except Exception:  # noqa: BLE001 — headless keyring is common
            logger.warning("keyring 不可用，凭证将以文件权限保护")
            cipher = "plaintext"
            content = f"{MAGIC}plaintext/v1\n{text}"
    else:
        content = f"{MAGIC}plaintext/v1\n{text}"

    atomic_write_text(target, content, mode=0o600)
    return cipher


def describe(path: str | Path | None = None) -> dict[str, Any]:
    """What protects the file right now (used by ``/doctor`` and docs)."""

    target = Path(path) if path is not None else default_credentials_path()
    if not target.exists():
        return {"path": str(target), "cipher": "missing", "encrypted": False}
    try:
        first = target.read_text(encoding="utf-8").split("\n", 1)[0]
    except OSError:
        return {"path": str(target), "cipher": "unreadable", "encrypted": False}
    cipher, _body = _split(first + "\n")
    return {
        "path": str(target),
        "cipher": cipher,
        "encrypted": cipher in {"dpapi", "keyring"},
    }


__all__ = [
    "MAGIC",
    "default_credentials_path",
    "describe",
    "load_document",
    "save_document",
]
