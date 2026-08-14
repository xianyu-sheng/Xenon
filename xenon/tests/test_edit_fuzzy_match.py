"""Tests for edit_file fuzzy matching (v0.8.3) — 解决 LLM 锚点匹配失败问题。

背景：SWE-bench 实测发现 LLM 生成的 old_text 常因缩进/换行/行尾空白
与文件实际内容有细微差异，导致 edit_file 精确匹配失败（「未找到匹配
文本」），验证循环的修复链因此断裂。本测试验证空白归一化匹配与
附近上下文提示。
"""

from __future__ import annotations

from xenon.nodes.tool_families.file_mutation import (
    _nearby_context,
    _normalize_ws,
    _normalized_match,
)


# ── _normalized_match ───────────────────────────────────────────

class TestNormalizedMatch:
    def test_exact_match_returns_same_text(self):
        content = "def foo():\n    return 1\n"
        assert _normalized_match(content, "    return 1") == "    return 1"

    def test_whitespace_diff_still_matches(self):
        # LLM 用 4 空格，文件用 2 空格 → 精确匹配失败，归一化应成功
        content = "def foo():\n  return 1\n"
        old = "    return 1"
        hit = _normalized_match(content, old)
        assert hit is not None
        # 返回文件中的原始文本（保留原缩进）
        assert hit == "  return 1"

    def test_newline_diff_still_matches(self):
        # LLM 把多行压成一行（或反之）
        content = "if a:\n    x = 1\n    y = 2\n"
        old = "if a: x = 1 y = 2"
        hit = _normalized_match(content, old)
        assert hit is not None
        assert hit == "if a:\n    x = 1\n    y = 2"

    def test_multiple_matches_returns_none(self):
        content = "x = 1\nx = 1\n"
        assert _normalized_match(content, "x = 1") is None

    def test_no_match_returns_none(self):
        content = "a = 1\n"
        assert _normalized_match(content, "b = 2") is None

    def test_tabs_vs_spaces(self):
        content = "def f():\n\treturn 1\n"
        old = "def f():    return 1"  # 4 空格代替 tab
        hit = _normalized_match(content, old)
        assert hit is not None
        assert hit == "def f():\n\treturn 1"

    def test_empty_old_text_returns_none(self):
        assert _normalized_match("x = 1\n", "   ") is None


# ── _nearby_context ─────────────────────────────────────────────

class TestNearbyContext:
    def test_returns_similar_snippet(self):
        content = "\n".join(f"line {i}" for i in range(10))
        hint = _nearby_context(content, "line 5")
        assert "line" in hint
        assert "相似度" in hint
        assert "old_text" in hint

    def test_returns_empty_for_empty_old(self):
        assert _nearby_context("abc", "   ") == ""


# ── _normalize_ws ───────────────────────────────────────────────

class TestNormalizeWs:
    def test_collapses_whitespace(self):
        assert _normalize_ws("  a\n\t b  ") == "a b"

    def test_strips_edges(self):
        assert _normalize_ws("  hello  ") == "hello"
