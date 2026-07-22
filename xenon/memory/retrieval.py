"""Explainable retrieval boundary for Xenon memory."""

from __future__ import annotations

import re
from typing import Protocol

from xenon.memory.models import MemoryKind, MemoryMatch, MemoryRecord, MemoryScope


class MemoryRetriever(Protocol):
    """Pluggable ranking contract; vector search can implement this later."""

    def rank(self, query: str, records: list[MemoryRecord]) -> list[MemoryMatch]: ...


class LexicalMemoryRetriever:
    """Deterministic mixed Chinese/English ranker with human-readable reasons."""

    _SCOPE_BOOST = {
        MemoryScope.PROJECT_LOCAL: 0.30,
        MemoryScope.PROJECT_SHARED: 0.25,
        MemoryScope.USER: 0.15,
        MemoryScope.SESSION: 0.35,
    }

    def rank(self, query: str, records: list[MemoryRecord]) -> list[MemoryMatch]:
        terms = self.terms(query)
        matches: list[MemoryMatch] = []
        for record in records:
            haystack = f"{record.content} {' '.join(record.tags)}".casefold()
            matched = sorted(term for term in terms if term in haystack)
            always_relevant = record.pinned or record.kind == MemoryKind.CONSTRAINT
            if not matched and not always_relevant:
                continue
            reasons: list[str] = []
            if matched:
                preview = "、".join(matched[:4])
                reasons.append(f"命中关键词: {preview}")
            if record.pinned:
                reasons.append("已固定")
            if record.kind == MemoryKind.CONSTRAINT:
                reasons.append("确定性约束")
            reasons.append(f"范围加权: {record.scope.value}")
            score = (
                len(matched)
                + self._SCOPE_BOOST[record.scope]
                + record.importance * 0.35
                + record.confidence * 0.15
                + (0.4 if always_relevant else 0.0)
            )
            matches.append(MemoryMatch(record, score, tuple(reasons)))
        matches.sort(
            key=lambda item: (
                -item.score,
                -item.record.use_count,
                -item.record.retrieval_count,
                item.record.id,
            )
        )
        return matches

    @staticmethod
    def terms(text: str) -> set[str]:
        terms = {word.casefold() for word in re.findall(r"[A-Za-z0-9_./-]{2,}", text)}
        for segment in re.findall(r"[\u4e00-\u9fff]+", text):
            for size in (2, 3, 4):
                terms.update(
                    segment[index:index + size]
                    for index in range(len(segment) - size + 1)
                )
        return terms
