"""
v0.4.0: Routing History — 路由决策记录与查看。

提供线程安全的内存环形缓冲区，可选 JSON 持久化。
用于 /history 命令和调度审计。
"""

from __future__ import annotations

import json
import threading
from collections import Counter
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from xenon.utils.atomic_write import atomic_write_text


@dataclass
class RoutingRecord:
    """单次路由决策记录。"""

    timestamp: float
    user_input_preview: str  # 前 120 字符
    intent: str | None
    complexity: float
    requires_reasoning: bool
    requires_code_generation: bool
    requires_tools: bool
    estimated_tokens: int
    task_tier: int | None  # Step 10 填充
    selected_models: list[str]  # model_id 列表，按分数降序
    scores: list[float]  # 对应的 _score 值


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
            for item in data[-self._ring.maxsize :]:
                record = RoutingRecord(**item)
                self._ring.append(record)
        except Exception:
            pass


@dataclass
class RoutingDecisionRecord:
    """A privacy-preserving intelligent-router decision."""

    timestamp: float
    user_input_preview: str
    features: dict[str, Any]
    matched_rule: str | None
    recommended_paradigm: str
    confidence: float
    current_paradigm: str
    switched: bool


class DecisionHistory:
    """Thread-safe decision ring buffer with optional JSONL persistence."""

    def __init__(self, maxsize: int = 200, persist_path: str | None = None):
        self._maxsize = maxsize
        self._records: list[RoutingDecisionRecord] = []
        self._persist_path = Path(persist_path) if persist_path else None
        self._lock = threading.Lock()
        self._load()

    def record(self, record: RoutingDecisionRecord) -> None:
        with self._lock:
            if len(self._records) >= self._maxsize:
                self._records.pop(0)
            self._records.append(record)
        self._save()

    def recent(self, n: int = 10) -> list[RoutingDecisionRecord]:
        with self._lock:
            return list(self._records[-n:])

    def all(self) -> list[RoutingDecisionRecord]:
        with self._lock:
            return list(self._records)

    def stats(self) -> dict[str, Any]:
        records = self.all()
        return {
            "total_decisions": len(records),
            "switches": sum(record.switched for record in records),
            "rule_hits": dict(
                Counter(record.matched_rule or "unmatched" for record in records)
            ),
            "paradigms": dict(
                Counter(record.recommended_paradigm for record in records)
            ),
            "recent": records[-10:],
        }

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
        if self._persist_path and self._persist_path.exists():
            try:
                self._persist_path.unlink()
            except OSError:
                pass

    def _save(self) -> None:
        if not self._persist_path:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            content = "\n".join(
                json.dumps(asdict(record), ensure_ascii=False)
                for record in self.all()
            )
            self._persist_path.write_text(
                content + ("\n" if content else ""), encoding="utf-8"
            )
        except Exception:
            pass  # logging must never affect the routing path

    def _load(self) -> None:
        if not self._persist_path or not self._persist_path.exists():
            return
        try:
            lines = self._persist_path.read_text(encoding="utf-8").splitlines()
            for line in lines[-self._maxsize :]:
                if line.strip():
                    self._records.append(
                        RoutingDecisionRecord(**json.loads(line))
                    )
        except Exception:
            self._records.clear()


@dataclass
class OverrideRecord:
    """One manual paradigm override: router recommended X, user chose Y."""

    timestamp: float
    user_input_preview: str
    signature: str
    recommended: str
    chosen: str


class ParadigmPreferences:
    """Learn paradigm preferences from manual overrides (pure frequency count).

    Thread-safe, bounded, with optional JSON persistence. Every I/O failure is
    swallowed: a corrupt or unwritable file must never break the main loop.
    """

    MIN_SAMPLES = 3  # overrides needed before a preference is trusted
    BOOST = 0.1  # small, fixed confidence bonus

    def __init__(self, maxsize: int = 200, persist_path: str | None = None):
        self._maxsize = maxsize
        self._records: list[OverrideRecord] = []
        self._persist_path = Path(persist_path) if persist_path else None
        self._lock = threading.Lock()
        self._load()

    def record(self, record: OverrideRecord) -> None:
        with self._lock:
            if len(self._records) >= self._maxsize:
                self._records.pop(0)
            self._records.append(record)
        self._save()

    def all(self) -> list[OverrideRecord]:
        with self._lock:
            return list(self._records)

    def preferred(self, signature: str) -> str | None:
        """Most frequently chosen paradigm for this signature, if trusted."""
        counts = Counter(r.chosen for r in self.all() if r.signature == signature)
        if not counts:
            return None
        paradigm, n = counts.most_common(1)[0]
        return paradigm if n >= self.MIN_SAMPLES else None

    def weight(self, signature: str, paradigm: str, confidence: float) -> float:
        """Add a small boost to the preferred paradigm; never exceeds 1.0."""
        if self.preferred(signature) == paradigm:
            return min(1.0, confidence + self.BOOST)
        return confidence

    def _save(self) -> None:
        if not self._persist_path:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            data = [asdict(r) for r in self.all()]
            # atomic: a crash mid-write can never leave a torn preferences file
            atomic_write_text(self._persist_path, json.dumps(data, ensure_ascii=False))
        except Exception:
            pass  # preference persistence must never affect the main loop

    def _load(self) -> None:
        if not self._persist_path or not self._persist_path.exists():
            return
        try:
            data = json.loads(self._persist_path.read_text(encoding="utf-8"))
            self._records = [OverrideRecord(**item) for item in data[-self._maxsize :]]
        except Exception:
            self._records.clear()  # corrupt file -> start empty
