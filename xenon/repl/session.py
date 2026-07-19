"""
Session Manager — 会话持久化。

支持 /save 和 /load 命令，将会话状态保存到磁盘并恢复。

v0.4.0 Step 14: 新增 auto_save / get_auto_session / cleanup_expired_sessions，
支持 /resume 恢复上次会话，7 天自动过期。
"""

from __future__ import annotations

import json
import os
import time as _time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from xenon.utils.atomic_write import atomic_write_text

SESSIONS_DIR = Path.home() / ".xenon" / "sessions"
SESSION_TTL_DAYS = 7
AUTO_SESSION_NAME = "_auto"


def _ensure_sessions_dir() -> Path:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return SESSIONS_DIR


def save_session(
    name: str,
    history: list[dict[str, Any]],
    context_store: dict[str, Any],
    model_config: dict[str, Any],
) -> Path:
    """
    保存当前会话到磁盘。

    Args:
        name: 会话名称。
        history: 对话历史（已序列化为 dict 列表）。
        context_store: AgentContext 的当前状态。
        model_config: 当前模型配置。

    Returns:
        保存的文件路径。
    """
    sessions_dir = _ensure_sessions_dir()
    filepath = sessions_dir / f"{name}.json"

    data = {
        "version": "1.0",
        "name": name,
        "saved_at": datetime.now().isoformat(),
        "history": history,
        "context": context_store,
        "model_config": model_config,
    }

    def _safe(obj: Any) -> Any:
        """将不可序列化的对象转为字符串。"""
        if isinstance(obj, (str, int, float, bool, type(None))):
            return obj
        if isinstance(obj, dict):
            return {str(k): _safe(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_safe(v) for v in obj]
        return str(obj)

    # A9 原子写入 + A10 chmod 0600（会话可能含对话历史等敏感内容）
    content = json.dumps(_safe(data), ensure_ascii=False, indent=2)
    atomic_write_text(filepath, content, mode=0o600)

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

    with open(filepath, encoding="utf-8") as f:
        return json.load(f)


def list_sessions() -> list[dict[str, str]]:
    """
    列出所有保存的会话。

    Returns:
        会话信息列表 [{"name": ..., "saved_at": ..., "path": ...}]
    """
    sessions_dir = _ensure_sessions_dir()
    sessions = []

    for f in sorted(sessions_dir.glob("*.json")):
        try:
            with open(f, encoding="utf-8") as fp:
                data = json.load(fp)
            sessions.append({
                "name": data.get("name", f.stem),
                "saved_at": data.get("saved_at", "unknown"),
                "path": str(f),
                "messages": len(data.get("history", [])),
            })
        except (json.JSONDecodeError, KeyError):
            continue

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
    """退出时自动保存当前会话状态。

    Returns:
        保存的文件路径，失败返回 None。
    """
    try:
        data = {
            "version": "2.0",
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

        def _safe(obj: Any) -> Any:
            if isinstance(obj, (str, int, float, bool, type(None))):
                return obj
            if isinstance(obj, dict):
                return {str(k): _safe(v) for k, v in obj.items()}
            if isinstance(obj, (list, tuple)):
                return [_safe(v) for v in obj]
            return str(obj)

        content = json.dumps(_safe(data), ensure_ascii=False, indent=2)
        atomic_write_text(filepath, content, mode=0o600)
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
        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)

        ts = data.get("saved_at_ts", 0)
        if ts > 0 and (_time.time() - ts) > SESSION_TTL_DAYS * 86400:
            filepath.unlink()
            return None

        return data
    except (json.JSONDecodeError, KeyError):
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
        except (json.JSONDecodeError, KeyError, OSError):
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
    if ts <= 0:
        return None

    elapsed = _time.time() - ts
    if elapsed < 3600:
        mins = int(elapsed / 60)
        return f"{mins} 分钟前" if mins > 0 else "刚刚"
    elif elapsed < 86400:
        return f"{int(elapsed / 3600)} 小时前"
    else:
        return f"{int(elapsed / 86400)} 天前"
