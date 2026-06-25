"""discover_agents 工具 — 动态发现可用的子 Agent 类型。

主 Agent 通过此工具查询 AgentCard 注册中心，
了解当前可用的子 Agent 类型及其能力范围。
"""

from __future__ import annotations

import logging
from typing import Any

from omniagent.engine.agent_card import get_card_registry
from omniagent.engine.context import AgentContext
from omniagent.tools.builtin.base import BaseTool

logger = logging.getLogger(__name__)


class DiscoverAgentsTool(BaseTool):
    """发现可用的子 Agent 类型。

    Agent 应先调用此工具了解当前有哪些子 Agent 类型、
    各自的能力范围（工具集、只读/可写）、以及约束条件，
    然后选择合适的 capability 调用 spawn_agent。
    """

    name = "discover_agents"
    description = (
        "发现可用的子 Agent 类型及其能力描述。"
        "返回每个子 Agent 的名称、可用的工具列表、是否只读、最大迭代次数等。"
        "主 Agent 应先调用此工具了解可用类型，再选择合适的 capability 调用 spawn_agent。"
    )
    params = {
        "name": "指定 AgentCard 名称查看详情（可选，不传则列出全部）",
    }

    def execute(self, context: AgentContext) -> dict[str, Any]:
        name = str(self._extra.get("name", "")).strip()
        card_registry = get_card_registry()

        if name:
            card = card_registry.get(name)
            if not card:
                return {
                    "success": False,
                    "error": f"未找到 AgentCard: {name}。可用: {card_registry.list_names()}",
                }
            return {
                "success": True,
                "card": {
                    "name": card.name,
                    "display_name": card.display_name,
                    "description": card.description,
                    "tools": card.tool_list if card.tool_list else ["(全部工具)"],
                    "read_only": card.is_read_only,
                    "max_iterations": card.max_iterations,
                    "timeout": card.timeout,
                    "version": card.version,
                },
            }

        # 列出全部
        cards = card_registry.discover()
        if not cards:
            return {
                "success": True,
                "cards": [],
                "message": "暂无可用子 Agent 类型。将自动写入默认名片。",
            }

        result_cards = []
        for card in cards:
            result_cards.append({
                "name": card.name,
                "display_name": card.display_name,
                "description": card.description,
                "tools": card.tool_list if card.tool_list else ["(全部工具)"],
                "read_only": card.is_read_only,
                "max_iterations": card.max_iterations,
            })

        return {
            "success": True,
            "cards": result_cards,
            "total": len(result_cards),
            "hint": "使用 spawn_agent(goal=..., capability=<name>) 选择合适的子 Agent 类型",
        }
