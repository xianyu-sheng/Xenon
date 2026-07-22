"""Data contracts for Xenon's second-generation memory system."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_now() -> str:
    """Return a stable, timezone-aware timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalized_content(content: str) -> str:
    """Normalize content for exact deduplication without changing its display."""
    return " ".join(content.casefold().split())


def content_checksum(content: str) -> str:
    return hashlib.sha256(normalized_content(content).encode("utf-8")).hexdigest()[:16]


class MemoryScope(str, Enum):
    USER = "user"
    PROJECT_LOCAL = "project-local"
    PROJECT_SHARED = "project-shared"
    SESSION = "session"


class MemoryKind(str, Enum):
    PREFERENCE = "preference"
    FACT = "fact"
    DECISION = "decision"
    CONSTRAINT = "constraint"
    LESSON = "lesson"


class MemoryStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    SUPERSEDED = "superseded"


@dataclass
class MemoryRecord:
    """One persistent memory plus maintenance and provenance metadata."""

    content: str
    scope: MemoryScope = MemoryScope.PROJECT_LOCAL
    kind: MemoryKind = MemoryKind.FACT
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    tags: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    last_retrieved_at: str | None = None
    retrieval_count: int = 0
    last_used_at: str | None = None
    use_count: int = 0
    importance: float = 0.5
    confidence: float = 0.8
    pinned: bool = False
    expires_at: str | None = None
    source: str = "user"
    evidence: str | None = None
    supersedes: str | None = None
    status: MemoryStatus = MemoryStatus.ACTIVE
    checksum: str = ""

    def __post_init__(self) -> None:
        if not self.checksum:
            self.checksum = content_checksum(self.content)
        self.importance = min(1.0, max(0.0, float(self.importance)))
        self.confidence = min(1.0, max(0.0, float(self.confidence)))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["scope"] = self.scope.value
        data["kind"] = self.kind.value
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryRecord":
        values = dict(data)
        values["scope"] = MemoryScope(values.get("scope", MemoryScope.PROJECT_LOCAL))
        values["kind"] = MemoryKind(values.get("kind", MemoryKind.FACT))
        values["status"] = MemoryStatus(values.get("status", MemoryStatus.ACTIVE))
        return cls(**values)


@dataclass
class MemoryProposal:
    """A not-yet-persisted memory candidate visible to the user."""

    content: str
    reason: str
    scope: MemoryScope = MemoryScope.PROJECT_LOCAL
    kind: MemoryKind = MemoryKind.FACT
    confidence: float = 0.0
    explicit: bool = False


@dataclass
class MemoryReceipt:
    """Auditable result returned after a memory mutation."""

    record: MemoryRecord
    destination: str
    created: bool = True
    archived_ids: list[str] = field(default_factory=list)
    conflict_ids: list[str] = field(default_factory=list)
    warning: str | None = None


@dataclass(frozen=True)
class MemoryMatch:
    """One explainable retrieval result."""

    record: MemoryRecord
    score: float
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemoryConflict:
    """A conservative potential conflict; Xenon never resolves it silently."""

    record: MemoryRecord
    reason: str
    confidence: float


@dataclass(frozen=True)
class MemoryHealthIssue:
    """A diagnostic finding produced without mutating memory."""

    severity: str
    scope: MemoryScope
    message: str
    memory_id: str | None = None


@dataclass(frozen=True)
class MemoryHealthReport:
    """Read-only health summary for the memory inspector."""

    active_count: int
    inactive_count: int
    active_tokens: int
    issues: tuple[MemoryHealthIssue, ...] = ()

    @property
    def healthy(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)
