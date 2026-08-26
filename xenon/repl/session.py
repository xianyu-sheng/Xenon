"""
Session Manager — 会话持久化。

支持 /save 和 /load 命令，将会话状态保存到磁盘并恢复。

v0.4.0 Step 14: 新增 auto_save / get_auto_session / cleanup_expired_sessions，
支持 /resume 恢复上次会话，7 天自动过期。

会话还记录访问热度（accessed_at_ts / access_count），list_sessions 据此排序，
让常用会话不被一次性会话淹没。详见 _session_relevance_score。
"""

from __future__ import annotations

import json
import time as _time
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any

from xenon.utils.atomic_write import atomic_write_text

SESSIONS_DIR = Path.home() / ".xenon" / "sessions"
SESSION_TTL_DAYS = 7
AUTO_SESSION_NAME = "_auto"

# accessed_at_ts 缺失且 saved_at_ts 也不可用时的年龄惩罚（天）。
# 取值远大于 TTL，保证「时间戳完全不可信」的会话排在有时间戳的会话之后，
# 但仍受 access_count 加权影响，不会被硬编码钉死在末尾。
_UNKNOWN_AGE_DAYS = 3650.0

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


def _read_access_stats(filepath: Path) -> tuple[float | None, int]:
    """读取已有会话文件的访问热度，用于覆盖写时保留计数。

    覆盖保存（/save 同名、或 _auto 每轮 checkpoint）如果把 access_count 重置为 0，
    热度信息会在每次自动保存后被抹平，排序就退化回纯时间序。这里在写入前把旧
    计数捞出来延续。文件不存在/损坏时返回 (None, 0)，即视作新建。
    """
    try:
        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeError, ValueError):
        return None, 0
    if not isinstance(data, dict):
        return None, 0
    accessed = data.get("accessed_at_ts")
    if not isinstance(accessed, (int, float)) or isinstance(accessed, bool) or accessed <= 0:
        accessed = None
    count = data.get("access_count", 0)
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        count = 0
    return accessed, count


def touch_session(filepath: Path) -> None:
    """记录一次「用户真的加载了这个会话」，更新访问时间并自增访问次数。

    必须由真实的加载入口调用（load_session / /resume 按序号加载），
    不能放进 _load_and_migrate —— 后者被 list_sessions 在循环里对每个文件调用，
    放在那里会让「列一次会话表」把所有会话的计数全部加一，热度彻底失真。

    失败不抛出：热度是排序用的软信息，不该让一次统计写失败毁掉会话恢复。
    """
    try:
        with open(filepath, encoding="utf-8") as f:
            raw = json.load(f)
        data, _ = _sanitize_session_data(raw)
        if not isinstance(data, dict):
            return
        _, count = _read_access_stats(filepath)
        data["accessed_at_ts"] = _time.time()
        data["access_count"] = count + 1
        _write_session_payload(filepath, data)
    except (OSError, json.JSONDecodeError, UnicodeError, ValueError, TypeError):
        return


def _session_relevance_score(session: dict[str, Any], *, now: float) -> float:
    """会话排序评分：``access_count * 10 - age_days``（越大越靠前）。

    为什么这样加权：
    - 纯按保存时间倒序时，一堆一次性会话会把用户天天用的那个挤到列表尾部，
      用户反馈「找不到想要的历史会话」正是这个原因。
    - 每次访问折算 10 天新近度：意味着一个被加载过 1 次的会话，能压住一个
      比它新 10 天以内的、从未被再次访问过的会话。倍率取 10 是因为会话 TTL
      是 7 天，一次访问的权重刚好略强于「整个生命周期内的时间衰减」，
      既让高频会话稳定置顶，又不至于让远古的高频会话永久霸榜——
      age_days 无上限增长，久不使用的会话最终仍会自然下沉。
    - 年龄用 accessed_at_ts（最近一次访问）而不是 saved_at_ts，因为「上次用它
      是什么时候」比「上次写盘是什么时候」更接近用户找会话时的心理模型。

    旧会话缺字段时优雅降级：accessed_at_ts 缺失回退 saved_at_ts，
    两者都不可用则按 _UNKNOWN_AGE_DAYS 计年龄，access_count 缺失按 0。
    """
    accessed = session.get("accessed_at_ts")
    if not isinstance(accessed, (int, float)) or isinstance(accessed, bool) or accessed <= 0:
        accessed = session.get("saved_at_ts")
    if not isinstance(accessed, (int, float)) or isinstance(accessed, bool) or accessed <= 0:
        age_days = _UNKNOWN_AGE_DAYS
    else:
        age_days = max(0.0, (now - float(accessed)) / 86400.0)

    count = session.get("access_count", 0)
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        count = 0

    return count * 10.0 - age_days


def _ensure_sessions_dir() -> Path:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return SESSIONS_DIR


def _session_path(name: str, sessions_dir: Path) -> Path:
    """把用户提供的会话名解析成 sessions 目录内的安全文件路径。

    会话名是 REPL 用户输入（/save <name>），直接拼路径会让 ``../x``
    之类的输入逃逸出 sessions 目录（写到 ``~/.xenon/`` 甚至更上层），
    而 ``/save a/b`` 因中间目录不存在直接抛 FileNotFoundError。
    这里统一做三段防护：路径分隔符/``.`` 分量 → ValueError；
    空名（含纯空白）→ ValueError；resolve 后二次确认仍在目录内。
    """
    stripped = name.strip()
    if not stripped:
        raise ValueError("会话名不能为空")
    if name != stripped:
        raise ValueError(f"会话名首尾不能含空白字符: {name!r}")
    parts = Path(name).parts
    if (
        Path(name).is_absolute()
        or any(part in ("..",) for part in parts)
        or any(sep in name for sep in ("/", "\\"))
    ):
        raise ValueError(f"会话名不能包含路径分隔符或 '..': {name!r}")
    filepath = sessions_dir / f"{name}.json"
    resolved = filepath.resolve()
    if resolved.parent != sessions_dir.resolve():
        raise ValueError(f"会话名解析后超出会话目录: {name!r}")
    return filepath


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
    filepath = _session_path(name, sessions_dir)

    now = _time.time()
    # 覆盖保存时延续既有热度；新建时 accessed_at_ts == saved_at_ts、access_count == 0。
    prev_accessed, prev_count = _read_access_stats(filepath)

    data = {
        # 版本号保持 "2.1"：新增的 accessed_at_ts / access_count 是纯附加字段，
        # 所有读取路径都对缺失做了降级，升版本只会让旧版 Xenon 无谓地拒绝加载。
        "version": "2.1",
        "name": name,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "saved_at_ts": now,
        "accessed_at_ts": prev_accessed if prev_accessed is not None else now,
        "access_count": prev_count,
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
    filepath = _session_path(name, sessions_dir)

    if not filepath.exists():
        raise FileNotFoundError(f"会话 '{name}' 不存在: {filepath}")

    data = _load_and_migrate(filepath)
    # 这里是真实的「用户加载了会话」路径，计一次访问。
    touch_session(filepath)
    return data


def list_sessions() -> list[dict[str, Any]]:
    """列出所有保存的会话（按访问热度 + 新近度综合排序）。

    排序键见 _session_relevance_score：``access_count * 10 - age_days``，
    常用会话优先，纯时间序仅作为从未访问过的会话之间的次序。

    Returns:
        会话信息列表。每个元素包含 name, saved_at, saved_at_ts,
        accessed_at_ts, access_count, path, messages, paradigm 字段。
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
            if not isinstance(saved_at_ts, (int, float)) or isinstance(saved_at_ts, bool):
                saved_at_ts = 0
            # 旧会话（v0.8.5 之前写盘）没有这两个字段，按 saved_at_ts / 0 降级。
            accessed_at_ts = data.get("accessed_at_ts")
            if (
                not isinstance(accessed_at_ts, (int, float))
                or isinstance(accessed_at_ts, bool)
                or accessed_at_ts <= 0
            ):
                accessed_at_ts = saved_at_ts
            access_count = data.get("access_count", 0)
            if not isinstance(access_count, int) or isinstance(access_count, bool) or access_count < 0:
                access_count = 0
            sessions.append({
                "name": data.get("name", f.stem),
                "saved_at": saved_at,
                "saved_at_ts": saved_at_ts,
                "accessed_at_ts": accessed_at_ts,
                "access_count": access_count,
                "path": str(f),
                "messages": len(history),
                "paradigm": extra.get("paradigm", ""),
            })
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError, UnicodeError):
            continue

    # 单次调用内固定 now，避免逐个会话取时间导致排序键不自洽。
    sessions.sort(key=partial(_session_relevance_score, now=_time.time()), reverse=True)
    return sessions


def delete_session(name: str) -> bool:
    """删除一个保存的会话。"""
    filepath = _session_path(name, _ensure_sessions_dir())
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
        sessions_dir = _ensure_sessions_dir()
        filepath = sessions_dir / f"{AUTO_SESSION_NAME}.json"

        now = _time.time()
        # _auto 每轮对话都被覆盖写，热度必须延续，否则计数永远是 0。
        prev_accessed, prev_count = _read_access_stats(filepath)

        data = {
            "version": "2.1",
            "name": AUTO_SESSION_NAME,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "saved_at_ts": now,
            "accessed_at_ts": prev_accessed if prev_accessed is not None else now,
            "access_count": prev_count,
            "history": history,
            "context": context_store,
            "model_config": model_config,
            "extra": extra or {},
        }

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


def cleanup_orphaned_multislot_files() -> int:
    """清理被回退的多槽位实现留下的孤儿会话文件。

    历史背景：
    - commit 0e3a7ca 引入了多槽位会话（_auto_<timestamp>_<id>.json）
    - commit 05096bb 回退该实现，恢复单一 _auto.json
    - 但遗留了大量 _auto_*.json 孤儿文件无法通过 /resume 序号访问

    此函数删除所有 _auto_<timestamp>_*.json 格式的孤儿文件，
    保留主文件 _auto.json 和所有命名会话。

    Returns:
        删除的文件数量
    """
    sessions_dir = _ensure_sessions_dir()
    deleted = 0

    for f in sessions_dir.glob("_auto_*.json"):
        # 只删除时间戳格式的孤儿文件：_auto_<digits>_<hex>.json
        # 保护可能的命名会话（如 _auto_backup.json）
        stem = f.stem  # "_auto_1787463448_2c28"
        parts = stem.split("_")  # ["", "auto", "1787463448", "2c28"]

        if len(parts) >= 4 and parts[1] == "auto" and parts[2].isdigit():
            try:
                f.unlink()
                deleted += 1
            except OSError:
                pass

    return deleted

