"""
v0.4.0: Routing History — 路由决策记录与查看。

提供线程安全的内存环形缓冲区，可选 JSON 持久化。
用于 /history 命令和调度审计。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


@dataclass
class RoutingRecord:
    """单次路由决策记录。"""

    timestamp: float
    user_input_preview: str          # 前 120 字符
    intent: str | None
    complexity: float
    requires_reasoning: bool
    requires_code_generation: bool
    requires_tools: bool
    estimated_tokens: int
    task_tier: int | None            # Step 10 填充
    selected_models: list[str]       # model_id 列表，按分数降序
    scores: list[float]              # 对应的 _score 值


class RingBuffer:
    """线程安全的固定大小环形缓冲区。"""

    def __init__(self, maxsize: int = 100):
        self._maxsize = maxsize
        self._buffer: list[RoutingRecord] = []
        self._lock = threading.Lock()

    @property
    def maxsize(self) -> int:
        return self._maxsize

    def append(self, record: RoutingRecord) -> None:
        with self._lock:
            if len(self._buffer) >= self._maxsize:
                self._buffer.pop(0)
            self._buffer.append(record)

    def recent(self, n: int = 10) -> list[RoutingRecord]:
        with self._lock:
            return list(self._buffer[-n:])

    def all(self) -> list[RoutingRecord]:
        with self._lock:
            return list(self._buffer)

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._buffer)


class RoutingHistory:
    """路由历史管理器，带可选的 JSON 持久化。"""

    def __init__(self, maxsize: int = 100, persist_path: str | None = None):
        self._ring = RingBuffer(maxsize)
        self._persist_path = Path(persist_path) if persist_path else None
        self._load()

    def record(self, record: RoutingRecord) -> None:
        self._ring.append(record)
        self._save()

    def recent(self, n: int = 10) -> list[RoutingRecord]:
        return self._ring.recent(n)

    def all(self) -> list[RoutingRecord]:
        return self._ring.all()

    def clear(self) -> None:
        self._ring.clear()
        if self._persist_path and self._persist_path.exists():
            self._persist_path.unlink()

    def _save(self) -> None:
        if not self._persist_path:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            data = [asdict(r) for r in self._ring.recent(self._ring.maxsize)]
            self._persist_path.write_text(json.dumps(data, ensure_ascii=False))
        except Exception:
            pass  # 持久化失败不影响运行

    def _load(self) -> None:
        if not self._persist_path or not self._persist_path.exists():
            return
        try:
            data = json.loads(self._persist_path.read_text())
            for item in data[-self._ring.maxsize:]:
                record = RoutingRecord(**item)
                self._ring.append(record)
        except Exception:
            pass
