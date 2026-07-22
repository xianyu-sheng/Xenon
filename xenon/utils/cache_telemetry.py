"""Privacy-preserving prompt manifests and local cache telemetry.

The provider remains the source of truth for cache hits.  This module only
describes the shape of a request so that a returned usage record can be
attributed to a cache family without persisting prompt or tool contents.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


MANIFEST_RESPONSE_KEY = "_xenon_cache_manifest"
_SCHEMA_VERSION = 1
_SESSION_ID = secrets.token_hex(8)
_SESSION_SECRET = secrets.token_bytes(32)
_SECRET_LOCK = threading.Lock()
_PERSISTENT_SECRET_CONFIGURED = False


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _opaque_digest(value: Any) -> str:
    """Return a session-scoped HMAC, safe to persist alongside telemetry."""
    material = value if isinstance(value, str) else _canonical_json(value)
    return hmac.new(
        _SESSION_SECRET,
        material.encode("utf-8", errors="replace"),
        hashlib.sha256,
    ).hexdigest()[:24]


def configure_persistent_secret(directory: str | Path) -> None:
    """Use one private local HMAC key so cache families survive restarts."""
    global _SESSION_SECRET, _PERSISTENT_SECRET_CONFIGURED
    with _SECRET_LOCK:
        if _PERSISTENT_SECRET_CONFIGURED:
            return
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        key_path = root / "telemetry.key"
        try:
            encoded = key_path.read_text(encoding="ascii").strip()
            secret = bytes.fromhex(encoded)
            if len(secret) < 32:
                raise ValueError("telemetry key is too short")
        except FileNotFoundError:
            secret = secrets.token_bytes(32)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            try:
                descriptor = os.open(key_path, flags, 0o600)
            except FileExistsError:
                secret = bytes.fromhex(key_path.read_text(encoding="ascii").strip())
            else:
                with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                    stream.write(secret.hex())
        _SESSION_SECRET = secret
        _PERSISTENT_SECRET_CONFIGURED = True


def _content_text(message: Mapping[str, Any]) -> str:
    content = message.get("content", "")
    return content if isinstance(content, str) else _canonical_json(content)


def _estimated_tokens(text: str) -> int:
    # This number is used only as an expectation baseline, never for billing.
    return max(1, (len(text) + 3) // 4) if text else 0


def canonical_model_id(model_id: str) -> str:
    key = str(model_id or "").strip().lower()
    if not key:
        return "unknown"
    if "/" in key:
        provider, name = key.split("/", 1)
        return f"{provider}/{name}"
    if key.startswith("deepseek-"):
        return f"deepseek/{key}"
    return key


@dataclass(frozen=True)
class PromptManifest:
    """Non-reversible description of one outbound prompt."""

    schema_version: int
    session_id: str
    request_id: str
    model_id: str
    engine: str
    phase: str
    project_hash: str
    context_epoch: int
    cache_family: str
    stable_prefix_hash: str
    history_prefix_hash: str
    prompt_hash: str
    tool_schema_hash: str
    message_count: int
    stable_message_count: int
    estimated_prompt_tokens: int
    expected_cacheable_tokens: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_prompt_manifest(
    model_id: str,
    messages: Sequence[Mapping[str, Any]],
    *,
    tools: Sequence[Mapping[str, Any]] | None = None,
    request_shape: Mapping[str, Any] | None = None,
    cache_context: Mapping[str, Any] | None = None,
) -> PromptManifest:
    """Build a manifest while keeping all original request text in memory only."""
    context = dict(cache_context or {})
    engine = str(context.get("engine") or "utility").strip().lower()
    phase = str(context.get("phase") or "request").strip().lower()
    project_hash = str(context.get("project_hash") or "")
    try:
        context_epoch = max(0, int(context.get("context_epoch") or 0))
    except (TypeError, ValueError):
        context_epoch = 0

    normalized_messages = [
        {"role": str(message.get("role", "")), "content": _content_text(message)}
        for message in messages
    ]
    stable_messages: list[dict[str, str]] = []
    for message in normalized_messages:
        if message["role"] != "system":
            break
        stable_messages.append(message)

    prefix_messages = normalized_messages[:-1] if normalized_messages else []
    prompt_text = _canonical_json(normalized_messages)
    prefix_text = _canonical_json(prefix_messages)
    stable_text = _canonical_json(stable_messages)
    tool_contract = {
        "tools": list(tools or []),
        "request_shape": dict(request_shape or {}),
    }
    tool_text = _canonical_json(tool_contract)
    canonical_model = canonical_model_id(model_id)

    stable_hash = _opaque_digest(stable_text)
    history_hash = _opaque_digest(prefix_text)
    tool_hash = _opaque_digest(tool_text) if (tools or request_shape) else ""
    family_material = {
        "model": canonical_model,
        "engine": engine,
        "phase": phase,
        "project_hash": project_hash,
        "context_epoch": context_epoch,
        "stable_prefix_hash": stable_hash,
        "tool_schema_hash": tool_hash,
    }
    return PromptManifest(
        schema_version=_SCHEMA_VERSION,
        session_id=_SESSION_ID,
        request_id=secrets.token_hex(8),
        model_id=canonical_model,
        engine=engine,
        phase=phase,
        project_hash=project_hash,
        context_epoch=context_epoch,
        cache_family=_opaque_digest(family_material),
        stable_prefix_hash=stable_hash,
        history_prefix_hash=history_hash,
        prompt_hash=_opaque_digest(prompt_text),
        tool_schema_hash=tool_hash,
        message_count=len(normalized_messages),
        stable_message_count=len(stable_messages),
        estimated_prompt_tokens=_estimated_tokens(prompt_text),
        expected_cacheable_tokens=_estimated_tokens(prefix_text),
    )


@dataclass(frozen=True)
class CacheEvent:
    """One locally attributed provider response; contains no prompt content."""

    schema_version: int
    timestamp: float
    session_id: str
    request_id: str
    cache_family: str
    model_id: str
    engine: str
    phase: str
    project_hash: str
    context_epoch: int
    stable_prefix_hash: str
    tool_schema_hash: str
    prompt_tokens: int
    completion_tokens: int
    cache_hit_tokens: int
    cache_miss_tokens: int
    cache_fields_present: bool
    cache_field_coverage: float
    expected_cacheable_ratio: float
    prefix_efficiency: float | None
    family_call: int
    state: str
    cause: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class CacheEventStore:
    """Bounded JSONL store for privacy-safe cache events."""

    def __init__(
        self,
        directory: str | Path | None = None,
        *,
        max_events: int = 500,
    ) -> None:
        configured = os.getenv("XENON_CACHE_DIR")
        self.directory = Path(directory or configured or (Path.home() / ".xenon" / "cache"))
        self.path = self.directory / "events.jsonl"
        self.max_events = max(10, int(max_events))
        self._lock = threading.Lock()

    def append(self, event: CacheEvent) -> None:
        payload = _canonical_json(event.as_dict())
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                self.path,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT,
                0o600,
            )
            with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
                stream.write(payload + "\n")
            self.path.chmod(0o600)
            self._trim_locked()

    def load(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            records = self._load_locked()
        return records[-limit:] if limit is not None else records

    def _load_locked(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    records.append(value)
        except OSError:
            return []
        return records

    def _trim_locked(self) -> None:
        records = self._load_locked()
        if len(records) <= self.max_events:
            return
        kept = records[-self.max_events:]
        temporary = self.path.with_suffix(".jsonl.tmp")
        temporary.write_text(
            "".join(_canonical_json(record) + "\n" for record in kept),
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, self.path)


def build_cache_event(
    manifest: Mapping[str, Any] | None,
    *,
    model_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    cache_hit_tokens: int,
    cache_miss_tokens: int,
    cache_fields_present: bool,
    family_call: int,
    previous_event: CacheEvent | None = None,
) -> CacheEvent:
    """Combine a request manifest with the provider's actual usage fields."""
    data = dict(manifest or {})
    prompt = max(0, int(prompt_tokens))
    hit = max(0, int(cache_hit_tokens))
    miss = max(0, int(cache_miss_tokens))
    covered = hit + miss
    coverage = min(1.0, covered / prompt) if prompt else 0.0
    expected = max(0, int(data.get("expected_cacheable_tokens") or 0))
    expected_ratio = min(1.0, expected / prompt) if prompt else 0.0
    efficiency = min(1.0, hit / expected) if expected else None

    if not cache_fields_present:
        state, cause = "unavailable", "cache_fields_unavailable"
    elif hit > 0:
        state, cause = "warm", "cache_hit"
    elif family_call <= 1:
        state, cause = "cold", "cold_family"
    elif family_call == 2:
        state, cause = "warming", "warming"
    else:
        state, cause = "miss", "provider_best_effort_miss"

    if cache_fields_present and not hit and family_call == 1 and previous_event is not None:
        if previous_event.model_id != canonical_model_id(str(data.get("model_id") or model_id)):
            cause = "model_switch"
        elif previous_event.engine != str(data.get("engine") or "unknown"):
            cause = "engine_switch"
        elif previous_event.phase != str(data.get("phase") or "request"):
            cause = "phase_switch"
        elif previous_event.tool_schema_hash != str(data.get("tool_schema_hash") or ""):
            cause = "toolset_changed"
        elif previous_event.project_hash != str(data.get("project_hash") or ""):
            cause = "project_changed"
        elif previous_event.context_epoch != int(data.get("context_epoch") or 0):
            cause = "context_compacted"
        elif previous_event.stable_prefix_hash != str(data.get("stable_prefix_hash") or ""):
            cause = "stable_prefix_changed"

    canonical_model = canonical_model_id(str(data.get("model_id") or model_id))
    return CacheEvent(
        schema_version=_SCHEMA_VERSION,
        timestamp=time.time(),
        session_id=str(data.get("session_id") or _SESSION_ID),
        request_id=str(data.get("request_id") or secrets.token_hex(8)),
        cache_family=str(data.get("cache_family") or _opaque_digest({
            "model": canonical_model,
            "legacy": True,
        })),
        model_id=canonical_model,
        engine=str(data.get("engine") or "unknown"),
        phase=str(data.get("phase") or "request"),
        project_hash=str(data.get("project_hash") or ""),
        context_epoch=max(0, int(data.get("context_epoch") or 0)),
        stable_prefix_hash=str(data.get("stable_prefix_hash") or ""),
        tool_schema_hash=str(data.get("tool_schema_hash") or ""),
        prompt_tokens=prompt,
        completion_tokens=max(0, int(completion_tokens)),
        cache_hit_tokens=hit,
        cache_miss_tokens=miss,
        cache_fields_present=bool(cache_fields_present),
        cache_field_coverage=round(coverage, 4),
        expected_cacheable_ratio=round(expected_ratio, 4),
        prefix_efficiency=round(efficiency, 4) if efficiency is not None else None,
        family_call=max(1, int(family_call)),
        state=state,
        cause=cause,
    )
