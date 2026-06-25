"""
PromptMemoryManager 测试 — 自主持久化决策逻辑。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from omniagent.repl.prompt_store import PromptStore
from omniagent.repl.prompt_memory import (
    PromptMemoryManager,
    _error_signature,
    _similarity,
    reset_error_memory,
)


@pytest.fixture
def mem_mgr():
    """创建带有临时 PromptStore 的 PromptMemoryManager。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir) / "prompts"
        store = PromptStore(project_dir=base, user_dir=base)
        store.ensure_initialized()
        mgr = PromptMemoryManager(store)
        reset_error_memory()
        yield mgr


# ═══════════════════════════════════════════════════════════════
# TestDetection — 触发条件检测
# ═══════════════════════════════════════════════════════════════


class TestDetection:
    """触发条件检测测试。"""

    def test_detects_user_preference_chinese(self, mem_mgr):
        """中文偏好关键词触发检测。"""
        candidates = mem_mgr.evaluate(
            history=[],
            user_input="我习惯用 pytest 而不是 unittest",
            assistant_output="好的，已了解你的偏好",
        )
        prefs = [c for c in candidates if c["category"] == "user-prefs"]
        assert len(prefs) >= 1

    def test_detects_user_preference_english(self, mem_mgr):
        """英文偏好关键词触发检测。"""
        candidates = mem_mgr.evaluate(
            history=[],
            user_input="I always prefer type hints in Python code",
            assistant_output="Understood, will use type hints",
        )
        prefs = [c for c in candidates if c["category"] == "user-prefs"]
        assert len(prefs) >= 1

    def test_detects_correction(self, mem_mgr):
        """用户纠正 + 助手确认触发检测。"""
        candidates = mem_mgr.evaluate(
            history=[],
            user_input="不对，你应该用 pathlib 而不是 os.path",
            assistant_output="抱歉，明白了，我会改用 pathlib",
        )
        corrs = [c for c in candidates if c["category"] == "learned-patterns"]
        assert len(corrs) >= 1

    def test_correction_without_ack_not_detected(self, mem_mgr):
        """用户纠正但助手未确认 → 不触发。"""
        candidates = mem_mgr.evaluate(
            history=[],
            user_input="不对，你搞错了",
            assistant_output="这是代码输出：print('hello')",
        )
        corrs = [c for c in candidates if c["category"] == "learned-patterns"]
        # 助手输出没有确认关键词 → 不应触发纠正检测
        assert len(corrs) == 0

    def test_repeated_error_detected(self, mem_mgr):
        """同一错误出现 2 次触发检测。"""
        error_output = "Traceback: File '<path>', line <N> ValueError: invalid literal"

        # 第一次 — 不触发
        candidates1 = mem_mgr.evaluate([], "", error_output)
        errs1 = [
            c for c in candidates1
            if c["category"] == "learned-patterns" and "错误模式" in c["content"]
        ]
        assert len(errs1) == 0

        # 第二次 — 触发
        candidates2 = mem_mgr.evaluate([], "", error_output)
        errs2 = [
            c for c in candidates2
            if c["category"] == "learned-patterns" and "错误模式" in c["content"]
        ]
        assert len(errs2) >= 1

    def test_normal_input_no_candidates(self, mem_mgr):
        """普通输入不产生持久化候选。"""
        candidates = mem_mgr.evaluate(
            history=[],
            user_input="今天天气怎么样",
            assistant_output="今天天气晴朗，适合出门",
        )
        assert len(candidates) == 0


# ═══════════════════════════════════════════════════════════════
# TestPersistence — 持久化写入
# ═══════════════════════════════════════════════════════════════


class TestPersistence:
    """持久化写入测试。"""

    def test_persist_writes_to_store(self, mem_mgr):
        """持久化确实写入 PromptStore。"""
        entry = mem_mgr.persist(
            "user-prefs",
            "用户偏好: 使用 pytest 和不使用 unittest",
            tags=["testing"],
        )
        assert entry is not None
        assert entry.category == "memory"

        memories = mem_mgr.store.list_memories()
        assert any("pytest" in m.content for m in memories)

    def test_deduplicate_exact_same(self, mem_mgr):
        """完全相同的记忆被去重。"""
        content = "用户偏好: 使用黑色主题"
        entry1 = mem_mgr.persist("user-prefs", content, tags=["theme"])
        assert entry1 is not None

        entry2 = mem_mgr.persist("user-prefs", content, tags=["theme"])
        assert entry2 is None  # 重复

    def test_deduplicate_similar_content(self, mem_mgr):
        """相似内容被去重（相似度 > 65%）。"""
        mem_mgr.persist("user-prefs", "用户偏好: 使用 pytest 运行测试", tags=["test"])
        entry2 = mem_mgr.persist(
            "user-prefs",
            "用户偏好: 使用 pytest 运行所有测试用例",
            tags=["test"],
        )
        assert entry2 is None  # 相似度 > 65%


# ═══════════════════════════════════════════════════════════════
# TestUtilities — 工具函数
# ═══════════════════════════════════════════════════════════════


class TestUtilities:
    """工具函数测试。"""

    def test_error_signature_normalizes_paths(self):
        """错误签名规范化文件路径和行号。"""
        original = 'File "D:\\project\\app.py", line 42, in main\nValueError'
        sig = _error_signature(original)
        assert "D:\\project\\app.py" not in sig
        assert "line 42" not in sig
        assert "<path>" in sig
        assert "<N>" in sig

    def test_error_signature_normalizes_timestamps(self):
        """错误签名规范化时间戳。"""
        original = "2026-06-25T14:30:00 ERROR something failed"
        sig = _error_signature(original)
        assert "2026-06-25" not in sig
        assert "<timestamp>" in sig

    def test_similarity_identical(self):
        """完全相同文本相似度为 1.0。"""
        assert _similarity("hello world", "hello world") == 1.0

    def test_similarity_different(self):
        """完全不同文本相似度接近 0。"""
        sim = _similarity("hello world", "completely different text here")
        assert sim < 0.5
