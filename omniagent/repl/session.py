"""Session persistence.

Two layers intentionally coexist:
1. Legacy named snapshots under ``~/.omniagent/sessions/*.json`` for /save and
   /load compatibility.
2. Runtime sessions under ``.omniagent/sessions/<session_id>/`` with
   ``thread.jsonl`` and ``notes.md`` for durable agent context.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

SESSIONS_DIR = Path.home() / ".omniagent" / "sessions"
PROJECT_SESSIONS_DIR = Path(".omniagent") / "sessions"


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


# ── Runtime session store ──────────────────────────────────

def new_session_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"sess-{stamp}-{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class RuntimeSession:
    id: str
    title: str
    created_at: str
    updated_at: str
    root: Path

    @property
    def thread_path(self) -> Path:
        return self.root / self.id / "thread.jsonl"

    @property
    def notes_path(self) -> Path:
        return self.root / self.id / "notes.md"

    @property
    def meta_path(self) -> Path:
        return self.root / self.id / "meta.json"

    @property
    def runs_dir(self) -> Path:
        return self.root / self.id / "runs"


class RuntimeSessionStore:
    """Project-local session/thread/notes persistence."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else PROJECT_SESSIONS_DIR

    def create(self, *, title: str = "", session_id: str | None = None) -> RuntimeSession:
        sid = session_id or new_session_id()
        now = datetime.now().astimezone().isoformat(timespec="milliseconds")
        session_dir = self.root / sid
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "runs").mkdir(exist_ok=True)
        meta = {
            "version": "1.0",
            "id": sid,
            "title": title or "OmniAgent session",
            "created_at": now,
            "updated_at": now,
        }
        (session_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        notes = session_dir / "notes.md"
        if not notes.exists():
            notes.write_text("# Session Notes\n\n", encoding="utf-8")
        thread = session_dir / "thread.jsonl"
        thread.touch(exist_ok=True)
        return self._from_meta(meta)

    def get(self, session_id: str) -> RuntimeSession:
        meta_path = self.root / session_id / "meta.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"runtime session not found: {session_id}")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return self._from_meta(meta)

    def list(self, limit: int = 20) -> list[RuntimeSession]:
        if not self.root.exists():
            return []
        sessions: list[RuntimeSession] = []
        for meta_path in self.root.glob("*/meta.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                sessions.append(self._from_meta(meta))
            except Exception:
                continue
        sessions.sort(key=lambda s: s.updated_at, reverse=True)
        return sessions[:limit]

    def append_message(
        self,
        session_id: str,
        *,
        role: str,
        content: str,
        run_id: str | None = None,
        model_used: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        session = self.get(session_id)
        event = {
            "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "role": role,
            "content": content,
            "run_id": run_id,
            "model_used": model_used,
            "metadata": metadata or {},
        }
        with session.thread_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(_safe(event), ensure_ascii=False, sort_keys=True) + "\n")
        self._touch(session_id)
        return event

    def read_thread(self, session_id: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        session = self.get(session_id)
        if not session.thread_path.exists():
            return []
        entries: list[dict[str, Any]] = []
        for line in session.thread_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries[-limit:] if limit else entries

    def read_notes(self, session_id: str) -> str:
        session = self.get(session_id)
        if not session.notes_path.exists():
            return ""
        return session.notes_path.read_text(encoding="utf-8", errors="replace")

    def append_note(self, session_id: str, note: str) -> Path:
        session = self.get(session_id)
        stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
        with session.notes_path.open("a", encoding="utf-8") as f:
            f.write(f"\n## {stamp}\n\n{note.strip()}\n")
        self._touch(session_id)
        return session.notes_path

    def _touch(self, session_id: str) -> None:
        meta_path = self.root / session_id / "meta.json"
        if not meta_path.exists():
            return
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["updated_at"] = datetime.now().astimezone().isoformat(timespec="milliseconds")
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def _from_meta(self, meta: dict[str, Any]) -> RuntimeSession:
        return RuntimeSession(
            id=str(meta["id"]),
            title=str(meta.get("title") or "OmniAgent session"),
            created_at=str(meta.get("created_at") or ""),
            updated_at=str(meta.get("updated_at") or ""),
            root=self.root,
        )


def _safe(obj: Any) -> Any:
    """Return a JSON-safe representation."""
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    if isinstance(obj, dict):
        return {str(k): _safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_safe(v) for v in obj]
    return str(obj)
