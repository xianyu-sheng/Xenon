"""Persistent tool-result cache with hash metadata.

This is the storage half of the structured-context design:

* every tool call gets a stable ``result_id``;
* the full raw result is written under the session directory (0600);
* a small ``meta.json`` records hashes, sizes, status and redacted params;
* prompt compression can later reference ``result_id`` and avoid loading raw
  output until the model or the user explicitly asks for it.

The cache is deliberately separate from the evidence ledger.  It is a
read-back copy, not an authority: provenance and the original tool call remain
the evidence.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TTL_DAYS = 7
DEFAULT_MAX_RAW_BYTES = 5 * 1024 * 1024


def _safe_session_component(session_id: str) -> str:
    """Keep a readable session name while preventing path traversal."""

    raw = str(session_id or "adhoc").strip() or "adhoc"
    if (
        raw not in {".", ".."}
        and "/" not in raw
        and "\\" not in raw
        and all(part not in {".", ".."} for part in Path(raw).parts)
    ):
        return raw[:120]
    return "session-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _atomic_write(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    """Write a cache file without exposing a partially written result."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _redacted_params(params: Any) -> Any:
    """Redact credential-shaped keys; keep the shape for debugging."""

    if not isinstance(params, dict):
        return params
    sensitive = {
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "token",
        "secret",
        "client_secret",
        "password",
        "passwd",
        "authorization",
        "cookie",
        "private_key",
    }
    safe: dict[str, Any] = {}
    for key, value in params.items():
        if str(key).lower().replace("-", "_") in sensitive:
            safe[str(key)] = "[REDACTED]"
        elif isinstance(value, dict):
            safe[str(key)] = _redacted_params(value)
        elif isinstance(value, list):
            safe[str(key)] = [
                _redacted_params(item) if isinstance(item, dict) else item
                for item in value[:20]
            ]
        else:
            text = str(value)
            safe[str(key)] = text[:500]
    return safe


@dataclass
class ToolResultMeta:
    """Small, stable manifest entry; raw content lives in a sibling file."""

    result_id: str
    session_id: str
    tool: str
    success: bool
    status: str = ""
    error: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    created_at: float = field(default_factory=time.time)
    raw_file: str | None = None
    raw_bytes: int = 0
    raw_sha256: str = ""
    raw_truncated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ToolResultStore:
    """Session-scoped full-result store used for later lazy retrieval."""

    def __init__(
        self,
        session_id: str,
        *,
        root: str | os.PathLike[str] | None = None,
        enabled: bool | None = None,
        ttl_days: float = DEFAULT_TTL_DAYS,
        max_raw_bytes: int = DEFAULT_MAX_RAW_BYTES,
    ) -> None:
        self.session_id = str(session_id or "adhoc")
        self.ttl_days = max(0.0, float(ttl_days))
        self.max_raw_bytes = max(1, int(max_raw_bytes))
        if enabled is None:
            enabled = os.environ.get("XENON_TOOL_RESULT_CACHE", "1") != "0"
        self.enabled = bool(enabled)
        self.root = (
            Path(root)
            if root is not None
            else Path.home()
            / ".xenon"
            / "sessions"
            / _safe_session_component(self.session_id)
            / "tool_results"
        ).resolve()

    # ── paths ──────────────────────────────────────────────
    def _result_dir(self, result_id: str) -> Path | None:
        """Resolve a generated result id only; never accept a user path."""

        if not result_id.startswith("tr_") or "/" in result_id or "\\" in result_id:
            return None
        path = (self.root / result_id).resolve()
        try:
            path.relative_to(self.root)
        except ValueError:
            return None
        return path

    # ── write ──────────────────────────────────────────────
    def record(
        self,
        execution_id: str,
        *,
        tool: str,
        params: Any,
        success: bool,
        observation: str = "",
        raw: Any = None,
        error: str | None = None,
        status: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ToolResultMeta | None:
        """Persist one result; failure is best-effort and never breaks a tool call."""

        if not self.enabled:
            return None
        result_id = f"tr_{str(execution_id or hash(time.time()))}"
        result_dir = self._result_dir(result_id)
        if result_dir is None:
            return None

        raw_text = _serialize_raw(raw, observation)
        raw_bytes = raw_text.encode("utf-8")
        raw_truncated = len(raw_bytes) > self.max_raw_bytes
        stored_bytes = raw_bytes[: self.max_raw_bytes] if raw_truncated else raw_bytes
        digest = hashlib.sha256(raw_bytes).hexdigest()
        raw_file: str | None = None
        try:
            result_dir.mkdir(parents=True, exist_ok=True)
            if stored_bytes:
                raw_file = "raw.txt"
                _atomic_write(result_dir / raw_file, stored_bytes)
            meta = ToolResultMeta(
                result_id=result_id,
                session_id=self.session_id,
                tool=str(tool or "unknown"),
                success=bool(success),
                status=str(status or ("succeeded" if success else "failed")),
                error=str(error) if error else None,
                params=_redacted_params(params or {}),
                summary=str(observation or "")[:2000],
                raw_file=raw_file,
                raw_bytes=len(raw_bytes),
                raw_sha256=digest,
                raw_truncated=raw_truncated,
                metadata=dict(metadata or {}),
            )
            _atomic_write(
                result_dir / "meta.json",
                json.dumps(meta.to_dict(), ensure_ascii=False, indent=2).encode(
                    "utf-8"
                ),
            )
            return meta
        except OSError as exc:
            logger.debug("工具结果缓存写入失败（已忽略）: %s", exc)
            return None

    # ── read ────────────────────────────────────────────────
    def read_meta(self, result_id: str) -> dict[str, Any] | None:
        result_dir = self._result_dir(result_id)
        if result_dir is None:
            return None
        try:
            data = json.loads((result_dir / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            return None
        return data if isinstance(data, dict) else None

    def read_raw(self, result_id: str, *, max_bytes: int | None = None) -> str | None:
        result_dir = self._result_dir(result_id)
        if result_dir is None:
            return None
        meta = self.read_meta(result_id)
        if not meta or not meta.get("raw_file"):
            return None
        path = result_dir / str(meta["raw_file"])
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if max_bytes is not None:
            data = data[: max(0, int(max_bytes))]
        return data.decode("utf-8", errors="replace")

    def list_results(self) -> list[dict[str, Any]]:
        """Return shallow manifest entries, newest first."""

        if not self.root.exists():
            return []
        rows: list[dict[str, Any]] = []
        for child in self.root.iterdir():
            if not child.is_dir():
                continue
            meta = self.read_meta(child.name)
            if meta:
                rows.append(meta)
        rows.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)
        return rows

    def cleanup(self, *, now: float | None = None) -> int:
        """Remove expired result directories; returns the number removed."""

        if not self.root.exists():
            return 0
        cutoff = (now or time.time()) - self.ttl_days * 86400
        removed = 0
        for child in self.root.iterdir():
            if not child.is_dir():
                continue
            try:
                mtime = child.stat().st_mtime
            except OSError:
                continue
            if mtime >= cutoff:
                continue
            try:
                shutil.rmtree(child)
                removed += 1
            except OSError:
                continue
        return removed


def _serialize_raw(raw: Any, observation: str) -> str:
    """Prefer the full raw result; fall back to the model observation."""

    if raw is None:
        return str(observation or "")
    if isinstance(raw, str):
        return raw
    try:
        return json.dumps(raw, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(raw)
