"""
Session Manager — 会话持久化。

支持 /save 和 /load 命令，将会话状态保存到磁盘并恢复。

v0.4.0 Step 14: 新增 auto_save / get_auto_session / cleanup_expired_sessions，
支持 /resume 恢复上次会话，7 天自动过期。
"""

from __future__ import annotations

import json
import time as _time
from datetime import datetime
from pathlib import Path
from typing import Any

from xenon.utils.atomic_write import atomic_write_text

SESSIONS_DIR = Path.home() / ".xenon" / "sessions"
SESSION_TTL_DAYS = 7
AUTO_SESSION_NAME = "_auto"

_SENSITIVE_SESSION_KEYS = frozenset({
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "token",
    "secret",
    "client_secret",
    "password",
    "authorization",
    "cookie",
    "private_key",
})


def _is_sensitive_session_key(key: object) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    return normalized in _SENSITIVE_SESSION_KEYS


def _sanitize_session_data(obj: Any) -> tuple[Any, bool]:
    """Remove credentials from persisted session structures.

    Conversation text remains byte-for-byte intact.  Only values stored under
    explicit credential field names are removed, including nested provider or
    MCP configuration.  The boolean reports whether a legacy payload changed.
    """
    if isinstance(obj, dict):
        cleaned: dict[str, Any] = {}
        changed = False
        for raw_key, value in obj.items():
            key = str(raw_key)
            if _is_sensitive_session_key(key):
                changed = True
                continue
            safe_value, child_changed = _sanitize_session_data(value)
            cleaned[key] = safe_value
            changed = changed or child_changed or key != raw_key
        return cleaned, changed
    if isinstance(obj, (list, tuple)):
        cleaned_items: list[Any] = []
        changed = isinstance(obj, tuple)
        for value in obj:
            safe_value, child_changed = _sanitize_session_data(value)
            cleaned_items.append(safe_value)
            changed = changed or child_changed
        return cleaned_items, changed
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj, False
    return str(obj), True


def _write_session_payload(filepath: Path, data: dict[str, Any]) -> None:
    safe_data, _ = _sanitize_session_data(data)
    content = json.dumps(safe_data, ensure_ascii=False, indent=2)
    atomic_write_text(filepath, content, mode=0o600)


def _load_and_migrate(filepath: Path) -> dict[str, Any]:
    """Load a session and atomically scrub credentials from legacy files."""
    with open(filepath, encoding="utf-8") as f:
        raw = json.load(f)
    data, changed = _sanitize_session_data(raw)
    if not isinstance(data, dict):
        raise ValueError(f"无效会话格式: {filepath}")
    if changed:
        _write_session_payload(filepath, data)
    return data


def _ensure_sessions_dir() -> Path:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return SESSIONS_DIR


def save_session(
    name: str,
    history: list[dict[str, Any]],
    context_store: dict[str, Any],
    model_config: dict[str, Any],
    *,
    extra: dict[str, Any] | None = None,
) -> Path:
    """
    保存当前会话到磁盘。

    Args:
        name: 会话名称。
        history: 对话历史（已序列化为 dict 列表）。
        context_store: AgentContext 的当前状态。
        model_config: 当前模型配置。
        extra: 额外元信息（如 paradigm）。

    Returns:
        保存的文件路径。
    """
    sessions_dir = _ensure_sessions_dir()
    filepath = sessions_dir / f"{name}.json"

    data = {
        "version": "2.1",
        "name": name,
        "saved_at": datetime.now().isoformat(),
        "saved_at_ts": _time.time(),
        "history": history,
        "context": context_store,
        "model_config": model_config,
        "extra": extra or {},
    }

    # A9 原子写入 + A10 chmod 0600；凭证字段在落盘前统一删除。
    _write_session_payload(filepath, data)

    return filepath


def load_session(name: str) -> dict[str, Any]:
    """
    从磁盘加载会话。

    Args:
        name: 会话名称。

    Returns:
        会话数据字典。

    Raises:
        FileNotFoundError: 会话文件不存在。
    """
    sessions_dir = _ensure_sessions_dir()
    filepath = sessions_dir / f"{name}.json"

    if not filepath.exists():
        raise FileNotFoundError(f"会话 '{name}' 不存在: {filepath}")

    return _load_and_migrate(filepath)


def list_sessions() -> list[dict[str, Any]]:
    """列出所有保存的会话（按时间倒序）。

    Returns:
        会话信息列表，按 saved_at_ts 降序排列。
        每个元素包含 name, saved_at, saved_at_ts, messages 字段。
    """
    sessions_dir = _ensure_sessions_dir()
    sessions = []

    for f in sessions_dir.glob("*.json"):
        try:
            data = _load_and_migrate(f)
            history = data.get("history")
            # 跳过空会话
            if not isinstance(history, list) or not history:
                continue
            extra = data.get("extra", {})
            if not isinstance(extra, dict):
                extra = {}
            saved_at = data.get("saved_at", "unknown")
            if not isinstance(saved_at, str):
                saved_at = str(saved_at)
            saved_at_ts = data.get("saved_at_ts", 0)
            if not isinstance(saved_at_ts, (int, float)):
                saved_at_ts = 0
            sessions.append({
                "name": data.get("name", f.stem),
                "saved_at": saved_at,
                "saved_at_ts": saved_at_ts,
                "path": str(f),
                "messages": len(history),
                "paradigm": extra.get("paradigm", ""),
            })
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError, UnicodeError):
            continue

    sessions.sort(key=lambda s: s["saved_at_ts"], reverse=True)
    return sessions


def delete_session(name: str) -> bool:
    """删除一个保存的会话。"""
    filepath = SESSIONS_DIR / f"{name}.json"
    if filepath.exists():
        filepath.unlink()
        return True
    return False


# ── v0.4.0 Step 14: 自动保存/恢复 ──────────────────────

def auto_save(
    history: list[dict[str, Any]],
    context_store: dict[str, Any],
    model_config: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> Path | None:
    """Atomically checkpoint the current session to the stable ``_auto`` slot.

    Returns:
        保存的文件路径，失败返回 None。
    """
    try:
        data = {
            "version": "2.1",
            "name": AUTO_SESSION_NAME,
            "saved_at": datetime.now().isoformat(),
            "saved_at_ts": _time.time(),
            "history": history,
            "context": context_store,
            "model_config": model_config,
            "extra": extra or {},
        }
        sessions_dir = _ensure_sessions_dir()
        filepath = sessions_dir / f"{AUTO_SESSION_NAME}.json"

        _write_session_payload(filepath, data)
        return filepath
    except Exception:
        return None


def get_auto_session() -> dict[str, Any] | None:
    """获取最近的自动保存会话。

    检查 _auto.json 是否存在且未过期（7 天内）。
    过期则自动删除并返回 None。
    """
    filepath = SESSIONS_DIR / f"{AUTO_SESSION_NAME}.json"
    if not filepath.exists():
        return None

    try:
        data = _load_and_migrate(filepath)

        ts = data.get("saved_at_ts", 0)
        if ts > 0 and (_time.time() - ts) > SESSION_TTL_DAYS * 86400:
            filepath.unlink()
            return None

        return data
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError, UnicodeError):
        return None


def cleanup_expired_sessions() -> int:
    """清理所有过期的自动保存会话。

    Returns:
        删除的文件数量。
    """
    threshold = _time.time() - SESSION_TTL_DAYS * 86400
    deleted = 0

    if not SESSIONS_DIR.exists():
        return 0

    for f in SESSIONS_DIR.glob("_auto*.json"):
        try:
            with open(f, encoding="utf-8") as fp:
                data = json.load(fp)
            ts = data.get("saved_at_ts", 0)
            if ts > 0 and ts < threshold:
                f.unlink()
                deleted += 1
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError, UnicodeError):
            # 损坏的文件也清理
            try:
                f.unlink()
                deleted += 1
            except OSError:
                pass

    return deleted


def get_session_age(data: dict[str, Any]) -> str | None:
    """返回会话的人类可读年龄描述。"""
    ts = data.get("saved_at_ts", 0)
    if not isinstance(ts, (int, float)) or ts <= 0:
        return None

    elapsed = _time.time() - ts
    if elapsed < 3600:
        mins = int(elapsed / 60)
        return f"{mins} 分钟前" if mins > 0 else "刚刚"
    elif elapsed < 86400:
        return f"{int(elapsed / 3600)} 小时前"
    else:
        return f"{int(elapsed / 86400)} 天前"
