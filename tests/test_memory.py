"""
Memory Store 跨会话记忆测试。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from xenon.repl.memory import MemoryStore, Memory


class TestMemoryStore:
    """测试 MemoryStore 的核心功能。"""

    def test_add_and_list(self):
        """测试添加和列出记忆。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "memory.json")
            store.add("我喜欢用 Python", type="preference", tags=["python"])

            assert len(store.memories) == 1
            assert store.memories[0].content == "我喜欢用 Python"
            assert store.memories[0].type == "preference"

    def test_search(self):
        """测试搜索记忆。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "memory.json")
            store.add("Python 是我的主力语言", type="fact")
            store.add("项目使用 FastAPI 框架", type="project", tags=["fastapi"])
            store.add("遇到过 pip 安装超时的问题", type="error")

            results = store.search("Python")
            assert len(results) >= 1
            assert any("Python" in m.content for m in results)

    def test_search_by_tag(self):
        """测试按标签搜索。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "memory.json")
            store.add("FastAPI 项目", type="project", tags=["fastapi", "python"])
            store.add("Django 项目", type="project", tags=["django"])

            results = store.search("fastapi")
            assert len(results) == 1
            assert "FastAPI" in results[0].content

    def test_get_relevant(self):
        """测试根据上下文获取相关记忆。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "memory.json")
            store.add("项目使用 PostgreSQL 数据库", type="project")
            store.add("喜欢用 VS Code 编辑器", type="preference")

            relevant = store.get_relevant("帮我写一个数据库查询")
            assert len(relevant) >= 1
            assert any("数据库" in m.content for m in relevant)

    def test_delete(self):
        """测试删除记忆。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "memory.json")
            m = store.add("测试记忆", type="fact")
            assert len(store.memories) == 1

            assert store.delete(m.id) is True
            assert len(store.memories) == 0

    def test_delete_nonexistent(self):
        """测试删除不存在的记忆。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "memory.json")
            assert store.delete("nonexistent") is False

    def test_clear(self):
        """测试清空记忆。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "memory.json")
            store.add("记忆1")
            store.add("记忆2")
            count = store.clear()
            assert count == 2
            assert len(store.memories) == 0

    def test_persistence(self):
        """测试记忆持久化。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "memory.json"
            store1 = MemoryStore(path)
            store1.add("持久化测试", type="fact", tags=["test"])

            # 重新加载
            store2 = MemoryStore(path)
            assert len(store2.memories) == 1
            assert store2.memories[0].content == "持久化测试"

    def test_max_memories_eviction(self):
        """测试记忆上限淘汰。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "memory.json")
            # 添加超过上限的记忆
            for i in range(205):
                store.add(f"记忆 {i}")

            assert len(store.memories) <= 200

    def test_format_for_context(self):
        """测试格式化为上下文文本。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MemoryStore(Path(tmpdir) / "memory.json")
            store.add("Python 是最好的语言", type="fact")
            store.add("项目地址: /home/project", type="project")

            text = store.format_for_context()
            assert "记忆" in text
            assert "Python" in text

    def test_memory_dataclass(self):
        """测试 Memory 数据类。"""
        m = Memory(content="test", type="fact", tags=["a", "b"])
        assert m.content == "test"
        assert m.type == "fact"
        assert len(m.tags) == 2
        assert m.access_count == 0
        assert len(m.id) == 8
