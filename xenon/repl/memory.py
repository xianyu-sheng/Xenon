"""
Memory Store — 跨会话记忆系统。

将用户偏好、项目知识、错误经验等持久化存储，
在对话时自动检索相关记忆注入上下文。

存储位置: ~/.xenon/memory.json
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from xenon.utils.atomic_write import atomic_write_text

logger = logging.getLogger(__name__)

_MEMORY_PATH = Path.home() / ".xenon" / "memory.json"
MAX_MEMORIES = 200


@dataclass
class Memory:
    """单条记忆。"""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    type: str = "fact"          # fact | project | error | preference
    content: str = ""
    tags: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_accessed: str = field(default_factory=lambda: datetime.now().isoformat())
    access_count: int = 0


class MemoryStore:
    """跨会话记忆存储。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _MEMORY_PATH
        self.memories: list[Memory] = []
        self._load()

    def _load(self) -> None:
        """从磁盘加载记忆。"""
        if not self.path.exists():
            self.memories = []
            return

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.memories = [Memory(**m) for m in data.get("memories", [])]
            logger.info(f"加载了 {len(self.memories)} 条记忆")
        except Exception as e:
            logger.warning(f"加载记忆失败: {e}")
            self.memories = []

    def _save(self) -> None:
        """保存记忆到磁盘。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": "1.0",
            "count": len(self.memories),
            "memories": [asdict(m) for m in self.memories],
        }
        atomic_write_text(self.path, json.dumps(data, ensure_ascii=False, indent=2))  # A9 原子写
        logger.info(f"保存了 {len(self.memories)} 条记忆")

    def add(
        self,
        content: str,
        type: str = "fact",
        tags: list[str] | None = None,
    ) -> Memory:
        """添加一条记忆。"""
        memory = Memory(
            type=type,
            content=content,
            tags=tags or [],
        )
        self.memories.append(memory)

        # 超出上限时淘汰
        if len(self.memories) > MAX_MEMORIES:
            self._evict()

        self._save()
        logger.info(f"添加记忆: [{type}] {content[:50]}")
        return memory

    def search(self, query: str, type_filter: str | None = None, limit: int = 10) -> list[Memory]:
        """搜索记忆。"""
        query_lower = query.lower()
        results = []

        for m in self.memories:
            if type_filter and m.type != type_filter:
                continue

            # 关键词匹配
            score = 0
            if query_lower in m.content.lower():
                score += 3
            for tag in m.tags:
                if query_lower in tag.lower():
                    score += 1

            if score > 0:
                m.last_accessed = datetime.now().isoformat()
                m.access_count += 1
                results.append((score, m))

        results.sort(key=lambda x: (-x[0], -x[1].access_count))
        return [m for _, m in results[:limit]]

    def get_relevant(self, context_text: str, limit: int = 5) -> list[Memory]:
        """根据上下文文本获取相关记忆。"""
        # 从上下文中提取关键词（支持中文：按空格和常用标点分词 + 2-gram）
        words = set()
        # 按空格分词
        for word in context_text.split():
            word = word.strip(".,!?;:()[]{}\"'，。！？；：（）【】「」")
            if len(word) >= 2:
                words.add(word.lower())

        # 中文 2-gram 提取（连续 2-4 个字符作为关键词）
        import re
        chinese_chars = re.findall(r'[一-鿿]+', context_text)
        for segment in chinese_chars:
            for n in (2, 3, 4):
                for i in range(len(segment) - n + 1):
                    words.add(segment[i:i + n].lower())

        scored: list[tuple[int, Memory]] = []
        for m in self.memories:
            score = 0
            content_lower = m.content.lower()
            for w in words:
                if w in content_lower:
                    score += 1
            for tag in m.tags:
                tag_lower = tag.lower()
                for w in words:
                    if w in tag_lower:
                        score += 1

            if score > 0:
                scored.append((score, m))

        scored.sort(key=lambda x: -x[0])
        return [m for _, m in scored[:limit]]

    def list_all(self, type_filter: str | None = None) -> list[Memory]:
        """列出所有记忆。"""
        if type_filter:
            return [m for m in self.memories if m.type == type_filter]
        return list(self.memories)

    def delete(self, memory_id: str) -> bool:
        """删除一条记忆。"""
        for i, m in enumerate(self.memories):
            if m.id == memory_id:
                self.memories.pop(i)
                self._save()
                return True
        return False

    def clear(self) -> int:
        """清空所有记忆。"""
        count = len(self.memories)
        self.memories.clear()
        self._save()
        return count

    def _evict(self) -> None:
        """淘汰最老、最少访问的记忆。"""
        # 按 access_count 和时间排序，保留最多的
        self.memories.sort(key=lambda m: (m.access_count, m.created_at), reverse=True)
        self.memories = self.memories[:MAX_MEMORIES]

    def format_for_context(self, memories: list[Memory] | None = None) -> str:
        """将记忆格式化为可注入上下文的文本。"""
        items = memories or self.memories
        if not items:
            return ""

        lines = ["[记忆] 以下是你之前记住的信息:"]
        for m in items[:10]:
            type_emoji = {"fact": "📌", "project": "📁", "error": "⚠️", "preference": "⭐"}.get(m.type, "📝")
            lines.append(f"  {type_emoji} [{m.type}] {m.content}")
        return "\n".join(lines)
