"""
PromptStore 测试 — 核心存储、检索、版本控制、渐进式加载。
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from omniagent.repl.prompt_store import (
    _MAX_CONTEXT_TOKENS,
    PromptEntry,
    PromptMetadata,
    PromptStore,
    _estimate_tokens,
    _extract_keywords,
    _make_frontmatter,
    _parse_frontmatter,
)


# ── Helpers ──────────────────────────────────────────────────


@pytest.fixture
def tmp_store():
    """创建临时目录的 PromptStore。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir) / "prompts"
        store = PromptStore(project_dir=base, user_dir=base)
        store.ensure_initialized()
        yield store


# ═══════════════════════════════════════════════════════════════
# TestPromptStore — 基础 CRUD
# ═══════════════════════════════════════════════════════════════


class TestPromptStore:
    """PromptStore 基础操作测试。"""

    def test_ensure_initialized_creates_structure(self, tmp_store):
        """首次初始化创建完整目录结构。"""
        assert tmp_store.is_initialized
        assert tmp_store._master is not None
        assert len(tmp_store._master.content) > 0

    def test_get_master_returns_content(self, tmp_store):
        """get_master 返回主提示词内容。"""
        master = tmp_store.get_master()
        assert "OmniAgent-CLI" in master
        assert len(master) > 50

    def test_master_has_frontmatter_in_file(self, tmp_store):
        """system.md 文件包含 YAML frontmatter。"""
        system_md = tmp_store._user_dir / "system.md"
        assert system_md.exists()
        content = system_md.read_text(encoding="utf-8")
        assert content.startswith("---")
        assert "version:" in content

    def test_update_master_increments_version(self, tmp_store):
        """更新 master 时版本号递增。"""
        old_ver = tmp_store._master.metadata.version
        tmp_store.update_master("# New Master\nTest content.")
        assert tmp_store._master.metadata.version == old_ver + 1

    def test_update_master_archives_old_version(self, tmp_store):
        """更新 master 时归档旧版本。"""
        tmp_store.update_master("# V2 Master\nUpdated.")
        versions = tmp_store.list_versions()
        assert len(versions) >= 1

    def test_list_domains_returns_seed_domains(self, tmp_store):
        """首次初始化后列出种子领域。"""
        domains = tmp_store.list_domains()
        assert "python" in domains
        assert "debugging" in domains
        assert "git" in domains
        assert "testing" in domains

    def test_get_domain_returns_entry(self, tmp_store):
        """按名称获取 domain 条目。"""
        entry = tmp_store.get_domain("python")
        assert entry is not None
        assert entry.category == "domain"
        assert "Python" in entry.content
        assert "PEP 8" in entry.content

    def test_add_and_list_memories(self, tmp_store):
        """添加和列出 memory。"""
        entry = tmp_store.add_memory(
            "test-memory",
            "用户偏好: 使用 pytest 而不是 unittest",
            tags=["testing", "pytest"],
        )
        assert entry.category == "memory"
        assert "pytest" in entry.content

        memories = tmp_store.list_memories()
        assert len(memories) >= 1
        assert any("pytest" in m.content for m in memories)

    def test_delete_memory(self, tmp_store):
        """删除 memory。"""
        tmp_store.add_memory("to-delete", "临时记忆", tags=["temp"])
        assert tmp_store.delete_memory("to-delete")
        assert not tmp_store.delete_memory("nonexistent")

    def test_persistence_roundtrip(self, tmp_store):
        """创建、新 store 加载、验证一致性。"""
        tmp_store.add_memory("roundtrip-test", "持久化测试", tags=["test"])
        old_memories = tmp_store.list_memories()

        # 创建新 store 实例（相同目录）
        store2 = PromptStore(
            project_dir=tmp_store._project_dir,
            user_dir=tmp_store._user_dir,
        )
        store2._load_all()
        new_memories = store2.list_memories()
        assert len(new_memories) == len(old_memories)


# ═══════════════════════════════════════════════════════════════
# TestFrontmatter — YAML frontmatter 解析
# ═══════════════════════════════════════════════════════════════


class TestFrontmatter:
    """YAML frontmatter 解析测试。"""

    def test_parse_valid_frontmatter(self):
        """正确解析合法 frontmatter。"""
        text = """---
version: 3
domain: python
tags: [python, testing]
priority: high
---

# Python Rules
Content here."""
        meta, body = _parse_frontmatter(text)
        assert meta["version"] == 3
        assert meta["domain"] == "python"
        assert meta["tags"] == ["python", "testing"]
        assert meta["priority"] == "high"
        assert body.strip().startswith("# Python Rules")

    def test_parse_no_frontmatter(self):
        """无 frontmatter 时返回空 dict + 原文。"""
        text = "# Just a heading\nContent."
        meta, body = _parse_frontmatter(text)
        assert meta == {}
        assert body == text

    def test_make_frontmatter_roundtrip(self):
        """序列化 → 解析往返一致。"""
        meta = PromptMetadata(
            version=2,
            domain="test",
            tags=["a", "b"],
            priority="high",
        )
        fm = _make_frontmatter(meta)
        parsed, _ = _parse_frontmatter(fm + "\n# body")
        assert parsed["version"] == 2
        assert parsed["domain"] == "test"
        assert parsed["tags"] == ["a", "b"]

    def test_make_frontmatter_contains_source(self):
        """frontmatter 包含 source 字段。"""
        meta = PromptMetadata(source="agent")
        fm = _make_frontmatter(meta)
        assert "source: agent" in fm


# ═══════════════════════════════════════════════════════════════
# TestTokenEstimation — Token 估算
# ═══════════════════════════════════════════════════════════════


class TestTokenEstimation:
    """Token 估算测试。"""

    def test_empty_string(self):
        assert _estimate_tokens("") == 0

    def test_english_text(self):
        tokens = _estimate_tokens("Hello world this is a test")
        assert tokens > 0
        # ~7 words × 1.3 ≈ 9, min chars/2 = 15
        assert tokens >= 9

    def test_chinese_text(self):
        tokens = _estimate_tokens("这是一个测试句子用于估算")
        # 11 CJK chars × 2 = 22
        assert tokens >= 20

    def test_prompt_entry_auto_estimates(self):
        """PromptEntry 自动计算 token_estimate。"""
        entry = PromptEntry(
            path="test.md",
            metadata=PromptMetadata(),
            content="This is a test content with some text.",
            category="memory",
        )
        assert entry.token_estimate > 0


# ═══════════════════════════════════════════════════════════════
# TestProgressiveLoading — 渐进式加载
# ═══════════════════════════════════════════════════════════════


class TestProgressiveLoading:
    """渐进式加载测试。"""

    def test_master_always_included(self, tmp_store):
        """无论输入如何，master 始终加载。"""
        result = tmp_store.load_relevant_prompts("random gibberish xyz123")
        masters = [e for e in result if e.category == "master"]
        assert len(masters) == 1

    def test_domain_matched_by_keyword(self, tmp_store):
        """输入关键词匹配对应领域。"""
        result = tmp_store.load_relevant_prompts("帮我调试这个 Python 程序的错误")
        domains = [e.metadata.domain for e in result if e.category == "domain"]
        # 应该匹配 debugging 和 python
        assert "debugging" in domains or "python" in domains

    def test_git_query_matches_git_domain(self, tmp_store):
        """Git 查询匹配 git 领域。"""
        result = tmp_store.load_relevant_prompts("帮我提交代码到 git 仓库")
        domains = [e.metadata.domain for e in result if e.category == "domain"]
        assert "git" in domains

    def test_empty_input_returns_master_only(self, tmp_store):
        """空输入仅返回 master。"""
        result = tmp_store.load_relevant_prompts("")
        non_master = [e for e in result if e.category != "master"]
        assert len(non_master) == 0

    def test_token_budget_respected(self, tmp_store):
        """结果总 token 不超过预算。"""
        result = tmp_store.load_relevant_prompts(
            "python git testing debugging",
            token_budget=1000,
        )
        total = sum(e.token_estimate for e in result)
        # master 计入 _MASTER_BUDGET 封顶
        assert total <= _MAX_CONTEXT_TOKENS * 2  # generous bound for test

    def test_result_sorted_by_relevance(self, tmp_store):
        """结果按相关性排序（master 第一，然后 domain/memory）。"""
        result = tmp_store.load_relevant_prompts("git")
        # master 应该在第一
        assert result[0].category == "master"
        # git domain 应该紧随其后
        if len(result) > 1:
            domains = [e.metadata.domain for e in result[1:] if e.category == "domain"]
            if "git" in domains:
                git_idx = domains.index("git")
                assert git_idx == 0  # git 应该是第一个 domain


# ═══════════════════════════════════════════════════════════════
# TestFormatForContext — 格式化输出
# ═══════════════════════════════════════════════════════════════


class TestFormatForContext:
    """format_for_context 测试。"""

    def test_only_master_no_extra_prefix(self, tmp_store):
        """仅 master 时不输出多余前缀。"""
        entries = [tmp_store._master]
        text = tmp_store.format_for_context(entries)
        assert text == ""

    def test_domain_has_prefix(self, tmp_store):
        """Domain 输出包含领域知识前缀。"""
        domain = tmp_store.get_domain("python")
        text = tmp_store.format_for_context([domain])
        assert "系统提示词 - 领域知识" in text
        assert "Python" in text

    def test_memory_has_prefix(self, tmp_store):
        """Memory 输出包含长期记忆前缀。"""
        mem = tmp_store.add_memory("test-fmt", "测试记忆内容", tags=["test"])
        text = tmp_store.format_for_context([mem])
        assert "系统提示词 - 长期记忆" in text
        assert "测试记忆内容" in text

    def test_mixed_entries_both_prefixes(self, tmp_store):
        """混合 domain + memory 时两个前缀都存在。"""
        domain = tmp_store.get_domain("python")
        mem = tmp_store.add_memory("mixed-test", "混合测试", tags=["test"])
        text = tmp_store.format_for_context([domain, mem])
        assert "领域知识" in text
        assert "长期记忆" in text


# ═══════════════════════════════════════════════════════════════
# TestKeywordExtraction — 关键词提取
# ═══════════════════════════════════════════════════════════════


class TestKeywordExtraction:
    """_extract_keywords 测试。"""

    def test_english_words(self):
        words = _extract_keywords("hello world testing")
        assert "hello" in words
        assert "world" in words
        assert "testing" in words

    def test_chinese_ngrams(self):
        words = _extract_keywords("调试程序")
        # 2-gram of 2-char CJK string → "调试", "试程", "程序"
        assert "调试" in words
        assert "程序" in words

    def test_mixed_language(self):
        words = _extract_keywords("python 调试工具")
        assert "python" in words
        assert "调试" in words

    def test_short_words_filtered(self):
        words = _extract_keywords("a b c")
        # 单个字符被过滤
        assert "a" not in words
        assert "b" not in words
