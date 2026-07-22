"""Storage interface and the JSON + Markdown reference implementation."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, Protocol, TypeVar

from xenon.memory.locking import InterProcessFileLock
from xenon.memory.models import MemoryKind, MemoryRecord, MemoryScope, MemoryStatus
from xenon.utils.atomic_write import atomic_write_text


_T = TypeVar("_T")


class MemoryBackend(Protocol):
    """Interface implemented by all persistent memory backends."""

    scope: MemoryScope
    root: Path

    def list_records(self, *, include_archived: bool = False) -> list[MemoryRecord]: ...
    def save_records(self, records: list[MemoryRecord], *, render: bool = True) -> None: ...
    def mutate_records(
        self,
        mutator: Callable[[list[MemoryRecord]], _T],
        *,
        render: bool = True,
    ) -> _T: ...
    def archive_records(self, records: list[MemoryRecord]) -> None: ...
    def destination_for(self, kind: MemoryKind) -> Path: ...


_KIND_FILES = {
    MemoryKind.PREFERENCE: "preferences.md",
    MemoryKind.FACT: "project.md",
    MemoryKind.DECISION: "decisions.md",
    MemoryKind.CONSTRAINT: "conventions.md",
    MemoryKind.LESSON: "lessons.md",
}


class JsonMarkdownBackend:
    """Keep machine state in JSON and generate small, inspectable Markdown views."""

    VERSION = 2

    def __init__(self, root: Path, scope: MemoryScope, *, private: bool = True) -> None:
        if scope == MemoryScope.SESSION:
            raise ValueError("session memory is intentionally not persistent")
        self.root = root
        self.scope = scope
        self.private = private
        self.metadata_path = root / "metadata.json"
        self.archive_path = root / "archive.jsonl"
        self.lock_path = root / ".metadata.lock"

    @property
    def _mode(self) -> int:
        return 0o600 if self.private else 0o644

    def destination_for(self, kind: MemoryKind) -> Path:
        return self.root / _KIND_FILES[kind]

    def list_records(self, *, include_archived: bool = False) -> list[MemoryRecord]:
        records = self._read_records()
        if include_archived:
            return records
        return [record for record in records if record.status == MemoryStatus.ACTIVE]

    def _read_records(self) -> list[MemoryRecord]:
        if not self.metadata_path.exists():
            return []
        try:
            payload = json.loads(self.metadata_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("顶层必须是 JSON 对象")
            version = payload.get("version")
            if not isinstance(version, int) or version < 1 or version > self.VERSION:
                raise ValueError(f"不支持的元数据版本: {version!r}")
            stored_scope = payload.get("scope")
            if stored_scope is not None and stored_scope != self.scope.value:
                raise ValueError(
                    f"作用域不匹配: 文件为 {stored_scope!r}，期望 {self.scope.value!r}"
                )
            items = payload.get("items", [])
            if not isinstance(items, list):
                raise TypeError("items 必须是列表")
            for item in items:
                if not isinstance(item, dict):
                    raise TypeError("每条记忆必须是 JSON 对象")
                if not isinstance(item.get("id"), str) or not item["id"].strip():
                    raise TypeError("记忆 id 必须是非空字符串")
                if not isinstance(item.get("content"), str):
                    raise TypeError("记忆 content 必须是字符串")
                if not isinstance(item.get("tags", []), list) or any(
                    not isinstance(tag, str) for tag in item.get("tags", [])
                ):
                    raise TypeError("记忆 tags 必须是字符串列表")
                for counter in ("retrieval_count", "use_count"):
                    value = item.get(counter, 0)
                    if not isinstance(value, int) or value < 0:
                        raise TypeError(f"记忆 {counter} 必须是非负整数")
            records = [MemoryRecord.from_dict(item) for item in items]
            ids = [record.id for record in records]
            if len(ids) != len(set(ids)):
                raise ValueError("存在重复的记忆 ID")
            if any(record.scope != self.scope for record in records):
                raise ValueError("记录 scope 与所在存储不一致")
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError(f"无法读取记忆元数据 {self.metadata_path}: {exc}") from exc
        return records

    def save_records(self, records: list[MemoryRecord], *, render: bool = True) -> None:
        self._ensure_root()
        with InterProcessFileLock(self.lock_path):
            self._save_records_unlocked(records, render=render)

    def mutate_records(
        self,
        mutator: Callable[[list[MemoryRecord]], _T],
        *,
        render: bool = True,
    ) -> _T:
        """Apply a complete read-modify-write transaction under one lock."""
        self._ensure_root()
        with InterProcessFileLock(self.lock_path):
            records = self._read_records()
            result = mutator(records)
            self._save_records_unlocked(records, render=render)
            return result

    def _save_records_unlocked(
        self, records: list[MemoryRecord], *, render: bool
    ) -> None:
        payload = {
            "version": self.VERSION,
            "scope": self.scope.value,
            "items": [record.to_dict() for record in records],
        }
        atomic_write_text(
            self.metadata_path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            mode=self._mode,
        )
        if render:
            self._render_views(records)

    def archive_records(self, records: list[MemoryRecord]) -> None:
        if not records:
            return
        self._ensure_root()
        with InterProcessFileLock(self.lock_path):
            existing = ""
            if self.archive_path.exists():
                existing = self.archive_path.read_text(encoding="utf-8")
            additions = "".join(
                json.dumps(record.to_dict(), ensure_ascii=False) + "\n"
                for record in records
            )
            atomic_write_text(self.archive_path, existing + additions, mode=self._mode)

    def _render_views(self, records: list[MemoryRecord]) -> None:
        active = [record for record in records if record.status == MemoryStatus.ACTIVE]
        grouped: dict[MemoryKind, list[MemoryRecord]] = {kind: [] for kind in MemoryKind}
        for record in active:
            grouped[record.kind].append(record)

        for kind, filename in _KIND_FILES.items():
            path = self.root / filename
            items = sorted(grouped[kind], key=lambda item: item.updated_at, reverse=True)
            title = kind.value.replace("-", " ").title()
            lines = [f"# {title}", "", "<!-- Generated by Xenon. Edit through Xenon memory commands. -->", ""]
            for item in items:
                pin = " 📌" if item.pinned else ""
                safe_content = item.content.replace("<", "&lt;").replace(">", "&gt;")
                lines.extend([
                    f"<!-- xenon-memory-id: {item.id} -->",
                    f"- {safe_content}{pin}",
                    "",
                ])
            atomic_write_text(path, "\n".join(lines).rstrip() + "\n", mode=self._mode)

        index_lines = [
            "# Xenon Memory Index",
            "",
            "<!-- Generated by Xenon. Compact entry point; metadata.json is authoritative. -->",
            "",
        ]
        for kind, filename in _KIND_FILES.items():
            count = len(grouped[kind])
            if count:
                index_lines.append(f"- [{kind.value.title()}]({filename}) — {count} active")
        if len(index_lines) == 4:
            index_lines.append("- No active memories.")
        atomic_write_text(
            self.root / "INDEX.md",
            "\n".join(index_lines) + "\n",
            mode=self._mode,
        )

    def _ensure_root(self) -> None:
        mode = 0o700 if self.private else 0o755
        self.root.mkdir(parents=True, exist_ok=True, mode=mode)
        try:
            os.chmod(self.root, mode)
        except OSError:
            pass
