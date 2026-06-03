"""
Session Manager — 会话持久化。

支持 /save 和 /load 命令，将会话状态保存到磁盘并恢复。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

SESSIONS_DIR = Path.home() / ".omniagent" / "sessions"


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

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(_safe(data), f, ensure_ascii=False, indent=2)

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
