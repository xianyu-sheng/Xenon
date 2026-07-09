"""
B-3 修复测试：模型列表按内置 priority 顺序重排，外部 API 顺序不可控。

v0.3.0 修复前：deepseek API 返回 `deepseek-v4-flash` 在 `deepseek-v4-pro` 之前，
导致 REPL 自动加载 p.models[0] 时选了 v4-flash。
v0.3.0 修复后：_sort_models_by_priority 按内置 info.models 顺序重排，
内置未列出的模型保持原顺序追加。
"""

from __future__ import annotations

import pytest

from omniagent.repl.provider_registry import _sort_models_by_priority


class TestSortModelsByPriority:
    """B-3 修复：内置 priority 决定默认模型选择顺序。"""

    def test_v4_pro_before_v4_flash(self):
        """内置 v4-pro 在前 → 拉取列表乱序时也把 v4-pro 排第一。"""
        fetched = ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat"]
        priority = ["deepseek-v4-pro", "deepseek-v4-flash", "deepseek-chat"]
        result = _sort_models_by_priority(fetched, priority)
        assert result == ["deepseek-v4-pro", "deepseek-v4-flash", "deepseek-chat"], (
            f"v4-pro 应在 v4-flash 前，实际: {result}"
        )

    def test_unlisted_models_appended(self):
        """拉取列表中有内置未列出的模型 → 保持原顺序追加在末尾。"""
        fetched = ["deepseek-beta-1", "deepseek-v4-flash", "deepseek-v4-pro"]
        priority = ["deepseek-v4-pro", "deepseek-v4-flash"]
        result = _sort_models_by_priority(fetched, priority)
        # v4-pro, v4-flash 按 priority 排，beta-1 原顺序追加
        assert result == ["deepseek-v4-pro", "deepseek-v4-flash", "deepseek-beta-1"], (
            f"未列出的 beta-1 应原顺序追加，实际: {result}"
        )

    def test_empty_priority_keeps_original(self):
        """内置 priority 为空 → 拉取列表保持原顺序。"""
        fetched = ["x", "y", "z"]
        result = _sort_models_by_priority(fetched, [])
        assert result == ["x", "y", "z"]

    def test_empty_fetched_returns_empty(self):
        """拉取列表为空 → 返回空。"""
        assert _sort_models_by_priority([], ["a", "b"]) == []

    def test_no_match_keeps_original(self):
        """拉取列表里没有 priority 中的项 → 全部原顺序。"""
        fetched = ["c", "b", "a"]
        result = _sort_models_by_priority(fetched, ["x", "y", "z"])
        assert result == ["c", "b", "a"]

    def test_partial_match(self):
        """部分匹配：拉取列表中只有部分在 priority 中。"""
        fetched = ["unknown", "v4-pro", "v4-flash", "other"]
        priority = ["v4-pro", "v4-flash"]
        result = _sort_models_by_priority(fetched, priority)
        # v4-pro, v4-flash 按 priority 排；unknown, other 原顺序追加
        assert result == ["v4-pro", "v4-flash", "unknown", "other"], (
            f"部分匹配逻辑错: {result}"
        )

    def test_preserves_priority_order_for_matches(self):
        """匹配项的顺序由 priority 决定，**不**由 fetched 决定。"""
        # priority 中 v4-flash 在 v4-pro 之前
        priority = ["v4-flash", "v4-pro"]
        fetched = ["v4-pro", "v4-flash", "chat"]
        result = _sort_models_by_priority(fetched, priority)
        # v4-flash 优先
        assert result[0] == "v4-flash"
        assert result[1] == "v4-pro"
        assert result[2] == "chat"
