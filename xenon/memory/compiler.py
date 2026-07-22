"""Compile retrieved memories into a small, provenance-aware context block."""

from __future__ import annotations

from xenon.memory.models import MemoryKind, MemoryRecord


def estimate_tokens(text: str) -> int:
    """Cheap conservative estimate that works for mixed Chinese/English text."""
    ascii_count = sum(1 for char in text if ord(char) < 128)
    non_ascii_count = len(text) - ascii_count
    return non_ascii_count + (ascii_count + 3) // 4


class MemoryContextCompiler:
    """Bound memory injection independently from storage volume."""

    def compile(self, records: list[MemoryRecord], *, token_budget: int = 4000) -> str:
        if not records or token_budget <= 0:
            return ""
        lines = [
            "[Xenon 相关记忆] 仅在与当前任务相关时使用；如与用户当前指令冲突，以当前指令为准。"
        ]
        used = estimate_tokens(lines[0])
        icons = {
            MemoryKind.PREFERENCE: "偏好",
            MemoryKind.FACT: "事实",
            MemoryKind.DECISION: "决策",
            MemoryKind.CONSTRAINT: "约束",
            MemoryKind.LESSON: "经验",
        }
        for record in records:
            safe_content = " ".join(record.content.split())
            line = (
                f"- [{record.scope.value}/{icons[record.kind]} id={record.id}] "
                f"{safe_content}"
            )
            cost = estimate_tokens(line)
            if used + cost > token_budget:
                break
            lines.append(line)
            used += cost
        return "\n".join(lines) if len(lines) > 1 else ""
