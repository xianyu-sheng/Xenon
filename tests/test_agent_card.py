"""
AgentCard 协议测试 — 名片加载、发现、工具裁剪、扩展验证。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from omniagent.engine.agent_card import (
    AgentCard,
    AgentCardRegistry,
    _SEED_CARDS,
    get_card_registry,
)


@pytest.fixture
def tmp_registry():
    """创建临时目录的 AgentCardRegistry。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        reg = AgentCardRegistry(cards_dir=Path(tmpdir))
        reg.ensure_seeded()
        reg.load()
        yield reg


class TestAgentCard:
    """AgentCard 数据类测试。"""

    def test_card_properties(self):
        """名片属性正确解析。"""
        card = AgentCard(
            name="test-card",
            display_name="测试名片",
            description="测试用",
            capabilities={"tools": ["read_file", "grep"], "read_only": True},
            constraints={"max_iterations": 5, "timeout": 60},
        )
        assert card.is_read_only is True
        assert card.tool_list == ["read_file", "grep"]
        assert card.max_iterations == 5
        assert card.timeout == 60

    def test_empty_tools_means_inherit_all(self):
        """空工具列表 = 继承全部。"""
        card = AgentCard(name="full", display_name="Full", description="",
                         capabilities={"tools": [], "read_only": False})
        all_tools = {"read": {}, "write": {}, "grep": {}}
        resolved = card.resolve_tools(all_tools)
        assert resolved == all_tools

    def test_tool_resolution_filters_unknown(self):
        """不可用的工具被过滤。"""
        card = AgentCard(name="partial", display_name="P", description="",
                         capabilities={"tools": ["read_file", "nonexistent"]})
        all_tools = {"read_file": {}, "write_file": {}}
        resolved = card.resolve_tools(all_tools)
        assert "read_file" in resolved
        assert "nonexistent" not in resolved
        assert "write_file" not in resolved


class TestAgentCardRegistry:
    """AgentCardRegistry 测试。"""

    def test_seed_creates_cards(self, tmp_registry):
        """首次初始化写入默认名片。"""
        cards = tmp_registry.discover()
        assert len(cards) == len(_SEED_CARDS)
        names = {c.name for c in cards}
        assert "code-explorer" in names
        assert "file-writer" in names
        assert "test-runner" in names
        assert "general-purpose" in names

    def test_get_returns_card(self, tmp_registry):
        """get() 返回正确名片。"""
        card = tmp_registry.get("code-explorer")
        assert card is not None
        assert card.is_read_only is True
        assert "read_file" in card.tool_list

    def test_get_nonexistent(self, tmp_registry):
        """不存在的名片返回 None。"""
        assert tmp_registry.get("nonexistent") is None

    def test_list_names(self, tmp_registry):
        """list_names 返回排序的名称列表。"""
        names = tmp_registry.list_names()
        assert names == sorted(names)
        assert "code-explorer" in names

    def test_register_new_card(self, tmp_registry):
        """注册新名片。"""
        card = AgentCard(
            name="security-auditor",
            display_name="安全审计器",
            description="只读安全审计",
            capabilities={"tools": ["read_file", "grep"], "read_only": True},
        )
        tmp_registry.register(card)
        assert tmp_registry.get("security-auditor") is not None
        # 验证持久化
        card_path = tmp_registry.cards_dir / "security-auditor.yaml"
        assert card_path.exists()

    def test_unregister_card(self, tmp_registry):
        """注销名片。"""
        assert tmp_registry.unregister("code-explorer") is True
        assert tmp_registry.get("code-explorer") is None
        assert not (tmp_registry.cards_dir / "code-explorer.yaml").exists()

    def test_reload_picks_up_new_file(self, tmp_registry):
        """手动添加 YAML 后 reload 生效。"""
        yaml_content = """name: custom-agent
display_name: 自定义 Agent
description: 手动创建
capabilities:
  tools: [command]
  read_only: false
constraints:
  max_iterations: 3
version: '1.0'
"""
        (tmp_registry.cards_dir / "custom-agent.yaml").write_text(yaml_content, encoding="utf-8")
        tmp_registry.reload()
        card = tmp_registry.get("custom-agent")
        assert card is not None
        assert card.tool_list == ["command"]

    def test_resolve_tools_for_card(self, tmp_registry):
        """通过 registry 裁剪工具集。"""
        all_tools = {"read_file": {}, "write_file": {}, "grep": {}, "command": {}}
        resolved = tmp_registry.resolve_tools("code-explorer", all_tools)
        # code-explorer 应该只有只读工具
        assert "read_file" in resolved
        assert "grep" in resolved
        assert "write_file" not in resolved
        assert "command" not in resolved

    def test_resolve_tools_unknown_card_returns_all(self, tmp_registry):
        """未知名片返回全部工具。"""
        all_tools = {"read_file": {}, "write_file": {}}
        resolved = tmp_registry.resolve_tools("nonexistent", all_tools)
        assert resolved == all_tools
