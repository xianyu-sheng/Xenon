"""Policy, lifecycle management, retrieval, and audit receipts."""

from __future__ import annotations

import math
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from xenon.memory.compiler import MemoryContextCompiler, estimate_tokens
from xenon.memory.candidate import MemoryCandidateDetector
from xenon.memory.models import (
    MemoryKind,
    MemoryConflict,
    MemoryHealthIssue,
    MemoryHealthReport,
    MemoryMatch,
    MemoryReceipt,
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    content_checksum,
    utc_now,
)
from xenon.memory.registry import MemoryBackendRegistry
from xenon.memory.retrieval import LexicalMemoryRetriever, MemoryRetriever
from xenon.utils.atomic_write import atomic_write_text


@dataclass(frozen=True)
class MemoryPolicy:
    max_item_tokens: int = 300
    max_leaf_tokens: int = 1500
    max_active_tokens: int = 10_000
    max_context_tokens: int = 4000
    default_retrieval_limit: int = 8


class MemoryService:
    """The only layer allowed to apply memory write and retention policy."""

    def __init__(
        self,
        registry: MemoryBackendRegistry,
        *,
        policy: MemoryPolicy | None = None,
        compiler: MemoryContextCompiler | None = None,
        retriever: MemoryRetriever | None = None,
    ) -> None:
        self.registry = registry
        self.policy = policy or MemoryPolicy()
        self.compiler = compiler or MemoryContextCompiler()
        self.retriever = retriever or LexicalMemoryRetriever()
        self._session_records: list[MemoryRecord] = []

    def remember(
        self,
        content: str,
        *,
        scope: MemoryScope = MemoryScope.PROJECT_LOCAL,
        kind: MemoryKind = MemoryKind.FACT,
        tags: list[str] | None = None,
        source: str = "user",
        evidence: str | None = None,
        confidence: float = 0.8,
        importance: float = 0.5,
        pinned: bool = False,
    ) -> MemoryReceipt:
        value = self._validate_content(content)

        def mutate(target: list[MemoryRecord]):
            checksum = content_checksum(value)
            duplicate = next(
                (
                    item
                    for item in target
                    if item.checksum == checksum
                    and item.status == MemoryStatus.ACTIVE
                ),
                None,
            )
            if duplicate:
                duplicate.updated_at = utc_now()
                duplicate.tags = sorted(set(duplicate.tags + (tags or [])))
                duplicate.importance = max(duplicate.importance, importance)
                duplicate.confidence = max(duplicate.confidence, confidence)
                duplicate.pinned = duplicate.pinned or pinned
                return duplicate, False, [], None, []

            conflicts = self._find_conflicts_in_records(
                value,
                scope=scope,
                kind=kind,
                records=target,
            )
            record = MemoryRecord(
                content=value,
                scope=scope,
                kind=kind,
                tags=tags or [],
                source=source,
                evidence=evidence,
                confidence=confidence,
                importance=importance,
                pinned=pinned,
            )
            target.append(record)
            archived, warning = self._maintain_scope(
                scope, target, protect_id=record.id
            )
            return record, True, archived, warning, conflicts

        if scope == MemoryScope.SESSION:
            mutation = mutate(self._session_records)
        else:
            mutation = self.registry.get(scope).mutate_records(mutate, render=True)
        record, created, archived, warning, conflicts = mutation
        if scope != MemoryScope.SESSION:
            backend = self.registry.get(scope)
            warnings = [warning] if warning else []
            try:
                backend.archive_records(archived)
            except (OSError, ValueError) as exc:
                warnings.append(f"归档日志写入失败：{exc}")
            entrypoint_warning = self._entrypoint_warning(scope)
            if entrypoint_warning:
                warnings.append(entrypoint_warning)
            warning = "；".join(warnings) or None
        return MemoryReceipt(
            record=record,
            destination=self.destination_for(scope, record.kind),
            created=created,
            archived_ids=[item.id for item in archived],
            conflict_ids=[item.record.id for item in conflicts],
            warning=warning,
        )

    def retrieve(
        self,
        query: str,
        *,
        limit: int | None = None,
        token_budget: int | None = None,
    ) -> list[MemoryRecord]:
        matches = self.explain_retrieval(
            query,
            limit=limit,
            token_budget=token_budget,
        )
        selected = [match.record for match in matches]
        self._touch(selected)
        return selected

    def explain_retrieval(
        self,
        query: str,
        *,
        limit: int | None = None,
        token_budget: int | None = None,
    ) -> list[MemoryMatch]:
        """Return bounded ranking evidence without changing access counters."""
        candidates = [
            record
            for record in self.list_records(tolerate_errors=True)
            if record.status == MemoryStatus.ACTIVE
            and not self._is_expired(record)
            and not MemoryCandidateDetector.contains_secret(record.content)
        ]
        ranked = self.retriever.rank(query, candidates)
        budget = (
            token_budget
            if token_budget is not None
            else self.policy.max_context_tokens
        )
        result_limit = (
            limit
            if limit is not None
            else self.policy.default_retrieval_limit
        )
        selected: list[MemoryMatch] = []
        used = 0
        for match in ranked[:result_limit]:
            cost = estimate_tokens(match.record.content) + 12
            if used + cost > budget:
                break
            selected.append(match)
            used += cost
        return selected

    def format_for_context(
        self,
        records: list[MemoryRecord],
        *,
        context_window: int | None = None,
    ) -> str:
        budget = self.policy.max_context_tokens
        if context_window:
            budget = min(budget, max(1, int(context_window * 0.08)))
        return self.compiler.compile(records, token_budget=budget)

    def list_records(
        self,
        *,
        scope: MemoryScope | None = None,
        include_archived: bool = False,
        tolerate_errors: bool = False,
    ) -> list[MemoryRecord]:
        scopes = (scope,) if scope else (*self.registry.persistent_scopes(), MemoryScope.SESSION)
        records: list[MemoryRecord] = []
        for item_scope in scopes:
            try:
                records.extend(
                    self._records_for_scope(
                        item_scope, include_archived=include_archived
                    )
                )
            except (OSError, ValueError):
                if not tolerate_errors:
                    raise
        return records

    def get(self, memory_id: str) -> MemoryRecord | None:
        """Find an active or inactive record by stable ID."""
        return next(
            (
                record
                for record in self.list_records(
                    include_archived=True, tolerate_errors=True
                )
                if record.id == memory_id
            ),
            None,
        )

    def find_conflicts(
        self,
        content: str,
        *,
        scope: MemoryScope,
        kind: MemoryKind,
        exclude_id: str | None = None,
    ) -> list[MemoryConflict]:
        """Find conservative potential conflicts without mutating state."""
        value = self._validate_content(content)
        return self._find_conflicts_in_records(
            value,
            scope=scope,
            kind=kind,
            records=self._records_for_scope(scope, include_archived=True),
            exclude_id=exclude_id,
        )

    def replace(
        self,
        memory_id: str,
        content: str,
        *,
        kind: MemoryKind | None = None,
        source: str = "user-replacement",
    ) -> MemoryReceipt:
        """Explicitly supersede one record with a new, linked record."""
        value = self._validate_content(content)
        original = self.get(memory_id)
        if original is None or original.status != MemoryStatus.ACTIVE:
            raise ValueError(f"未找到活动记忆 [{memory_id}]")
        scope = original.scope
        replacement_kind = kind or original.kind

        def mutate(records: list[MemoryRecord]):
            current = next((item for item in records if item.id == memory_id), None)
            if current is None or current.status != MemoryStatus.ACTIVE:
                raise ValueError(f"记忆 [{memory_id}] 已被其他进程修改")
            checksum = content_checksum(value)
            duplicate = next(
                (
                    item
                    for item in records
                    if item.id != memory_id
                    and item.status == MemoryStatus.ACTIVE
                    and item.checksum == checksum
                ),
                None,
            )
            if duplicate:
                raise ValueError(f"替换内容已存在于活动记忆 [{duplicate.id}]")
            current.status = MemoryStatus.SUPERSEDED
            current.updated_at = utc_now()
            replacement = MemoryRecord(
                content=value,
                scope=scope,
                kind=replacement_kind,
                tags=list(current.tags),
                source=source,
                evidence=f"supersedes:{current.id}",
                confidence=current.confidence,
                importance=current.importance,
                pinned=current.pinned,
                supersedes=current.id,
            )
            conflicts = self._find_conflicts_in_records(
                value,
                scope=scope,
                kind=replacement_kind,
                records=records,
                exclude_id=current.id,
            )
            records.append(replacement)
            archived, warning = self._maintain_scope(
                scope, records, protect_id=replacement.id
            )
            return replacement, current, archived, warning, conflicts

        if scope == MemoryScope.SESSION:
            mutation = mutate(self._session_records)
        else:
            mutation = self.registry.get(scope).mutate_records(mutate, render=True)
        replacement, superseded, archived, warning, conflicts = mutation
        if scope != MemoryScope.SESSION:
            try:
                self.registry.get(scope).archive_records([superseded, *archived])
            except (OSError, ValueError) as exc:
                warning = self._join_warnings(
                    warning, f"归档日志写入失败：{exc}"
                )
            warning = self._join_warnings(warning, self._entrypoint_warning(scope))
        return MemoryReceipt(
            record=replacement,
            destination=self.destination_for(scope, replacement_kind),
            archived_ids=[item.id for item in archived],
            conflict_ids=[item.record.id for item in conflicts],
            warning=warning,
        )

    def rollback(self, memory_id: str) -> bool:
        """Undo a replacement by archiving it and reactivating its predecessor."""
        replacement = self.get(memory_id)
        if (
            replacement is None
            or replacement.status != MemoryStatus.ACTIVE
            or not replacement.supersedes
        ):
            return False
        scope = replacement.scope

        def mutate(records: list[MemoryRecord]) -> bool:
            current = next((item for item in records if item.id == memory_id), None)
            if current is None or not current.supersedes:
                return False
            previous = next(
                (item for item in records if item.id == current.supersedes), None
            )
            if (
                current.status != MemoryStatus.ACTIVE
                or previous is None
                or previous.status != MemoryStatus.SUPERSEDED
            ):
                return False
            now = utc_now()
            current.status = MemoryStatus.ARCHIVED
            current.updated_at = now
            previous.status = MemoryStatus.ACTIVE
            previous.updated_at = now
            return True

        if scope == MemoryScope.SESSION:
            return mutate(self._session_records)
        changed = self.registry.get(scope).mutate_records(mutate, render=True)
        if changed:
            try:
                self.registry.get(scope).archive_records([replacement])
            except (OSError, ValueError):
                pass
        return changed

    def archive(self, memory_id: str) -> bool:
        """Explicitly archive a record; physical deletion is deliberately absent."""
        existing = self.get(memory_id)
        if existing is None or existing.status != MemoryStatus.ACTIVE:
            return False
        scope = existing.scope
        archived: list[MemoryRecord] = []

        def mutate(records: list[MemoryRecord]) -> bool:
            record = next((item for item in records if item.id == memory_id), None)
            if record is None or record.status != MemoryStatus.ACTIVE:
                return False
            record.status = MemoryStatus.ARCHIVED
            record.updated_at = utc_now()
            archived.append(record)
            return True

        if scope == MemoryScope.SESSION:
            changed = mutate(self._session_records)
        else:
            changed = self.registry.get(scope).mutate_records(mutate, render=True)
        if changed and scope != MemoryScope.SESSION:
            try:
                self.registry.get(scope).archive_records(archived)
            except (OSError, ValueError):
                # metadata.json remains authoritative; archive.jsonl is an
                # auxiliary transition journal.
                pass
        return changed

    def restore(self, memory_id: str) -> bool:
        """Restore an archived record after an explicit user command."""
        existing = self.get(memory_id)
        if existing is None or existing.status != MemoryStatus.ARCHIVED:
            return False
        scope = existing.scope

        def mutate(records: list[MemoryRecord]) -> bool:
            record = next((item for item in records if item.id == memory_id), None)
            if record is None or record.status != MemoryStatus.ARCHIVED:
                return False
            now = utc_now()
            if record.supersedes:
                previous = next(
                    (item for item in records if item.id == record.supersedes), None
                )
                if previous is None:
                    raise ValueError(
                        f"记忆 [{memory_id}] 的替代链已损坏，拒绝恢复"
                    )
                if previous.status == MemoryStatus.ACTIVE:
                    previous.status = MemoryStatus.SUPERSEDED
                    previous.updated_at = now
            record.status = MemoryStatus.ACTIVE
            record.updated_at = now
            return True

        if scope == MemoryScope.SESSION:
            return mutate(self._session_records)
        return self.registry.get(scope).mutate_records(mutate, render=True)

    def set_pinned(self, memory_id: str, pinned: bool) -> bool:
        """Pin or unpin an active record under the backend transaction lock."""
        existing = self.get(memory_id)
        if existing is None or existing.status != MemoryStatus.ACTIVE:
            return False
        scope = existing.scope

        def mutate(records: list[MemoryRecord]) -> bool:
            record = next((item for item in records if item.id == memory_id), None)
            if record is None or record.status != MemoryStatus.ACTIVE:
                return False
            record.pinned = pinned
            record.updated_at = utc_now()
            return True

        if scope == MemoryScope.SESSION:
            return mutate(self._session_records)
        return self.registry.get(scope).mutate_records(mutate, render=True)

    def mark_used(self, memory_ids: list[str]) -> int:
        """Record memories included in a successfully completed answer turn."""
        wanted = set(memory_ids)
        if not wanted:
            return 0
        now = utc_now()
        changed_total = 0
        records = [
            record
            for record in self.list_records(
                include_archived=True, tolerate_errors=True
            )
            if record.id in wanted
        ]
        scopes = {record.scope for record in records}
        for scope in scopes:
            def mutate(records: list[MemoryRecord]) -> int:
                changed = 0
                for record in records:
                    if record.id in wanted and record.status == MemoryStatus.ACTIVE:
                        record.use_count += 1
                        record.last_used_at = now
                        changed += 1
                return changed

            if scope == MemoryScope.SESSION:
                changed_total += mutate(self._session_records)
            else:
                changed_total += self.registry.get(scope).mutate_records(
                    mutate, render=False
                )
        return changed_total

    def diagnose(self) -> MemoryHealthReport:
        """Inspect schema, lifecycle links, limits, and private file modes."""
        issues: list[MemoryHealthIssue] = []
        all_records: list[MemoryRecord] = []
        for scope in (*self.registry.persistent_scopes(), MemoryScope.SESSION):
            try:
                records = self._records_for_scope(scope, include_archived=True)
            except (OSError, ValueError) as exc:
                issues.append(MemoryHealthIssue("error", scope, str(exc)))
                continue
            all_records.extend(records)

            active = [item for item in records if item.status == MemoryStatus.ACTIVE]
            active_tokens = sum(estimate_tokens(item.content) for item in active)
            if active_tokens > self.policy.max_active_tokens:
                issues.append(
                    MemoryHealthIssue(
                        "warning",
                        scope,
                        f"活动记忆 {active_tokens} tokens，超过作用域阈值 "
                        f"{self.policy.max_active_tokens}",
                    )
                )
            for kind in MemoryKind:
                leaf_tokens = sum(
                    estimate_tokens(item.content)
                    for item in active
                    if item.kind == kind
                )
                if leaf_tokens > self.policy.max_leaf_tokens:
                    issues.append(
                        MemoryHealthIssue(
                            "warning",
                            scope,
                            f"{kind.value} 分类 {leaf_tokens} tokens，超过阈值 "
                            f"{self.policy.max_leaf_tokens}",
                        )
                    )

            ids = {item.id for item in records}
            active_checksums: dict[str, str] = {}
            for record in records:
                if record.checksum != content_checksum(record.content):
                    issues.append(
                        MemoryHealthIssue(
                            "error", scope, "内容校验和不匹配", record.id
                        )
                    )
                if MemoryCandidateDetector.contains_secret(record.content):
                    issues.append(
                        MemoryHealthIssue(
                            "error",
                            scope,
                            "内容疑似包含密钥、令牌或密码，不会被检索",
                            record.id,
                        )
                    )
                if record.supersedes and record.supersedes not in ids:
                    issues.append(
                        MemoryHealthIssue(
                            "error",
                            scope,
                            f"替代链指向不存在的记忆 [{record.supersedes}]",
                            record.id,
                        )
                    )
                if record.status == MemoryStatus.ACTIVE:
                    duplicate_id = active_checksums.get(record.checksum)
                    if duplicate_id:
                        issues.append(
                            MemoryHealthIssue(
                                "error",
                                scope,
                                f"与活动记忆 [{duplicate_id}] 内容重复",
                                record.id,
                            )
                        )
                    active_checksums[record.checksum] = record.id
                    if self._is_expired(record):
                        issues.append(
                            MemoryHealthIssue(
                                "warning", scope, "记忆已过期，不会被检索", record.id
                            )
                        )

            if scope != MemoryScope.SESSION:
                backend = self.registry.get(scope)
                metadata_path = getattr(backend, "metadata_path", None)
                private = getattr(backend, "private", None)
                if (
                    isinstance(metadata_path, Path)
                    and isinstance(private, bool)
                    and metadata_path.exists()
                ):
                    mode = stat.S_IMODE(metadata_path.stat().st_mode)
                    expected = 0o600 if private else 0o644
                    if mode != expected:
                        issues.append(
                            MemoryHealthIssue(
                                "warning",
                                scope,
                                f"metadata.json 权限为 {mode:o}，建议 {expected:o}",
                            )
                        )

        seen_ids: dict[str, MemoryScope] = {}
        for record in all_records:
            previous_scope = seen_ids.get(record.id)
            if previous_scope is not None and previous_scope != record.scope:
                issues.append(
                    MemoryHealthIssue(
                        "error",
                        record.scope,
                        f"ID 与 {previous_scope.value} 作用域重复",
                        record.id,
                    )
                )
            seen_ids[record.id] = record.scope

        active = [item for item in all_records if item.status == MemoryStatus.ACTIVE]
        return MemoryHealthReport(
            active_count=len(active),
            inactive_count=len(all_records) - len(active),
            active_tokens=sum(estimate_tokens(item.content) for item in active),
            issues=tuple(issues),
        )

    def destination_for(self, scope: MemoryScope, kind: MemoryKind) -> str:
        if scope == MemoryScope.SESSION:
            return "当前会话（不写入磁盘）"
        return str(self.registry.get(scope).destination_for(kind))

    def _records_for_scope(
        self, scope: MemoryScope, *, include_archived: bool
    ) -> list[MemoryRecord]:
        if scope == MemoryScope.SESSION:
            if include_archived:
                return self._session_records
            return [item for item in self._session_records if item.status == MemoryStatus.ACTIVE]
        return self.registry.get(scope).list_records(include_archived=include_archived)

    def _maintain_scope(
        self,
        scope: MemoryScope,
        records: list[MemoryRecord],
        *,
        protect_id: str | None = None,
    ) -> tuple[list[MemoryRecord], str | None]:
        active = [item for item in records if item.status == MemoryStatus.ACTIVE]
        over_leaf = any(
            sum(estimate_tokens(item.content) for item in active if item.kind == kind)
            > self.policy.max_leaf_tokens
            for kind in MemoryKind
        )
        total = sum(estimate_tokens(item.content) for item in active)
        if not over_leaf and total <= self.policy.max_active_tokens:
            return [], None
        if scope == MemoryScope.PROJECT_SHARED:
            return [], "项目共享记忆已超出建议阈值；共享规则不会被自动归档"

        archived: list[MemoryRecord] = []
        candidates = sorted(
            (item for item in active if not item.pinned and item.id != protect_id),
            key=self._retention_score,
        )
        while candidates:
            active = [item for item in records if item.status == MemoryStatus.ACTIVE]
            total = sum(estimate_tokens(item.content) for item in active)
            heavy_kinds = {
                kind for kind in MemoryKind
                if sum(estimate_tokens(item.content) for item in active if item.kind == kind)
                > self.policy.max_leaf_tokens
            }
            if total <= self.policy.max_active_tokens and not heavy_kinds:
                break
            victim_index = next(
                (i for i, item in enumerate(candidates) if not heavy_kinds or item.kind in heavy_kinds),
                None,
            )
            if victim_index is None:
                break
            victim = candidates.pop(victim_index)
            victim.status = MemoryStatus.ARCHIVED
            victim.updated_at = utc_now()
            archived.append(victim)
        warning = None
        if any(item.status == MemoryStatus.ACTIVE for item in records):
            remaining = [item for item in records if item.status == MemoryStatus.ACTIVE]
            remaining_over_leaf = any(
                sum(estimate_tokens(item.content) for item in remaining if item.kind == kind)
                > self.policy.max_leaf_tokens
                for kind in MemoryKind
            )
            if (
                sum(estimate_tokens(item.content) for item in remaining)
                > self.policy.max_active_tokens
                or remaining_over_leaf
            ):
                warning = "受保护记忆使当前作用域仍超过阈值"
        return archived, warning

    @staticmethod
    def _retention_score(record: MemoryRecord) -> float:
        age_days = 0.0
        try:
            created = datetime.fromisoformat(record.created_at)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age_days = max(0.0, (datetime.now(timezone.utc) - created).total_seconds() / 86400)
        except ValueError:
            pass
        recency = 1.0 / (1.0 + age_days / 30.0)
        return (
            record.importance * 3.0
            + record.confidence
            + math.log1p(record.use_count) * 1.5
            + math.log1p(record.retrieval_count) * 0.5
            + recency
        )

    def _touch(self, selected: list[MemoryRecord]) -> None:
        if not selected:
            return
        now = utc_now()
        selected_by_scope: dict[MemoryScope, list[MemoryRecord]] = {}
        for record in selected:
            selected_by_scope.setdefault(record.scope, []).append(record)
        for scope, selected_records in selected_by_scope.items():
            selected_map = {record.id: record for record in selected_records}

            def mutate(records: list[MemoryRecord]) -> None:
                for record in records:
                    selected_record = selected_map.get(record.id)
                    if selected_record is None or record.status != MemoryStatus.ACTIVE:
                        continue
                    record.retrieval_count += 1
                    record.last_retrieved_at = now
                    selected_record.retrieval_count = record.retrieval_count
                    selected_record.last_retrieved_at = now

            if scope == MemoryScope.SESSION:
                mutate(self._session_records)
            else:
                self.registry.get(scope).mutate_records(mutate, render=False)

    def _validate_content(self, content: str) -> str:
        value = " ".join(content.split()).strip()
        if not value:
            raise ValueError("记忆内容不能为空")
        if MemoryCandidateDetector.contains_secret(value):
            raise ValueError("检测到疑似密钥、令牌或密码，已拒绝写入记忆")
        if estimate_tokens(value) > self.policy.max_item_tokens:
            raise ValueError(
                f"单条记忆超过 {self.policy.max_item_tokens} token，请先拆分"
            )
        return value

    def _find_conflicts_in_records(
        self,
        content: str,
        *,
        scope: MemoryScope,
        kind: MemoryKind,
        records: list[MemoryRecord],
        exclude_id: str | None = None,
    ) -> list[MemoryConflict]:
        signature = self._assignment_signature(content)
        polarity = self._polarity_signature(content)
        conflicts: list[MemoryConflict] = []
        for record in records:
            if (
                record.id == exclude_id
                or record.status != MemoryStatus.ACTIVE
                or record.kind != kind
                or record.checksum == content_checksum(content)
            ):
                continue
            other_signature = self._assignment_signature(record.content)
            if (
                signature is not None
                and other_signature is not None
                and signature[0] == other_signature[0]
                and signature[1] != other_signature[1]
            ):
                conflicts.append(
                    MemoryConflict(record, "同一主题存在不同取值", 0.95)
                )
                continue
            other_polarity = self._polarity_signature(record.content)
            if (
                polarity is not None
                and other_polarity is not None
                and polarity[0] == other_polarity[0]
                and polarity[1] != other_polarity[1]
            ):
                conflicts.append(
                    MemoryConflict(record, "同一约束存在相反要求", 0.98)
                )
        return sorted(conflicts, key=lambda item: (-item.confidence, item.record.id))

    @staticmethod
    def _assignment_signature(text: str) -> tuple[str, str] | None:
        normalized = re.sub(r"[\s，。；：,:;.!！]+", " ", text.casefold()).strip()
        pattern = re.compile(
            r"^(?P<topic>.+?)(?:默认|固定|统一)?"
            r"(?:使用|采用|基于|设为|设置为|版本为|版本是|uses?|prefers?)\s+"
            r"(?P<value>.+)$",
            re.I,
        )
        match = pattern.match(normalized)
        if not match:
            return None
        topic = re.sub(r"\s+", "", match.group("topic"))
        value = re.sub(r"\s+", " ", match.group("value")).strip()
        return topic, value

    @staticmethod
    def _polarity_signature(text: str) -> tuple[str, bool] | None:
        negative = bool(re.search(r"(?:禁止|不得|不要|不允许|must\s+not)", text, re.I))
        positive = bool(re.search(r"(?:必须|务必|只能|must|required)", text, re.I))
        if not negative and not positive:
            return None
        base = re.sub(
            r"(?:禁止|不得|不要|不允许|必须|务必|只能|must\s+not|must|required)",
            "",
            text.casefold(),
            flags=re.I,
        )
        base = re.sub(r"[\s，。；：,:;.!！]+", "", base)
        return base, not negative

    @staticmethod
    def _join_warnings(*warnings: str | None) -> str | None:
        values = [warning for warning in warnings if warning]
        return "；".join(values) or None

    @staticmethod
    def _is_expired(record: MemoryRecord) -> bool:
        if not record.expires_at:
            return False
        try:
            expiry = datetime.fromisoformat(record.expires_at)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            return expiry <= datetime.now(timezone.utc)
        except ValueError:
            return False

    def _ensure_scope_entrypoint(self, scope: MemoryScope) -> None:
        if scope == MemoryScope.USER:
            path = self.registry.user_config_root / "XENON.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            marker = str(self.registry.get(scope).root / "INDEX.md")
            if path.exists():
                current = path.read_text(encoding="utf-8")
                if marker in current:
                    return
            else:
                current = "# XENON\n"
            block = (
                "\n<!-- xenon-memory-index:start -->\n"
                f"Xenon user memory index: `{marker}`\n"
                "<!-- xenon-memory-index:end -->\n"
            )
            atomic_write_text(path, current.rstrip() + "\n" + block, mode=0o600)
            return
        if scope not in (MemoryScope.PROJECT_LOCAL, MemoryScope.PROJECT_SHARED):
            return
        if self.registry.project_root is None:
            raise ValueError("当前未检测到项目，无法创建项目记忆入口")
        filename = "XENON.local.md" if scope == MemoryScope.PROJECT_LOCAL else "XENON.md"
        relative_index = (
            ".xenon/memory/local/INDEX.md"
            if scope == MemoryScope.PROJECT_LOCAL
            else ".xenon/memory/shared/INDEX.md"
        )
        path = self.registry.project_root / filename
        marker = f"@{relative_index}"
        if path.exists():
            current = path.read_text(encoding="utf-8")
            if marker in current:
                return
        else:
            current = f"# {filename.removesuffix('.md')}\n"
        block = (
            "\n<!-- xenon-memory-index:start -->\n"
            f"{marker}\n"
            "<!-- xenon-memory-index:end -->\n"
        )
        atomic_write_text(path, current.rstrip() + "\n" + block, mode=0o600 if scope == MemoryScope.PROJECT_LOCAL else 0o644)
        if scope == MemoryScope.PROJECT_LOCAL:
            self._ensure_local_git_excludes()

    def _entrypoint_warning(self, scope: MemoryScope) -> str | None:
        if scope == MemoryScope.SESSION:
            return None
        try:
            self._ensure_scope_entrypoint(scope)
            return None
        except (OSError, UnicodeError) as exc:
            return f"记忆已保存，但顶层索引入口更新失败：{exc}"

    def _ensure_local_git_excludes(self) -> None:
        """Keep private project memory out of commits without editing .gitignore."""
        if self.registry.project_root is None:
            return
        git_marker = self.registry.project_root / ".git"
        git_dir: Path | None = None
        if git_marker.is_dir():
            git_dir = git_marker
        elif git_marker.is_file():
            try:
                marker = git_marker.read_text(encoding="utf-8").strip()
                if marker.startswith("gitdir:"):
                    candidate = Path(marker.split(":", 1)[1].strip())
                    git_dir = candidate if candidate.is_absolute() else git_marker.parent / candidate
            except OSError:
                return
        if git_dir is None:
            return
        exclude = git_dir / "info" / "exclude"
        try:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
            additions = [
                entry for entry in ("/XENON.local.md", "/.xenon/memory/local/")
                if entry not in current.splitlines()
            ]
            if additions:
                updated = current.rstrip() + ("\n" if current.strip() else "")
                updated += "\n".join(additions) + "\n"
                atomic_write_text(exclude, updated)
        except OSError:
            # Memory persistence already succeeded; git exclude is best-effort.
            return
