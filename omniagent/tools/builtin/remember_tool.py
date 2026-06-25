"""
Remember tool — Agent 自主持久化工具。

Agent 可以调用此工具将学习到的模式、用户偏好、项目约定等
写入系统提示词文件夹（PromptStore），供后续会话使用。
"""

from __future__ import annotations

import logging
from typing import Any

from omniagent.engine.context import AgentContext
from omniagent.tools.builtin.base import BaseTool

logger = logging.getLogger(__name__)


class RememberTool(BaseTool):
    """持久化一条长期记忆或模式到系统提示词文件夹。"""

    name = "remember"
    description = (
        "持久化一条长期记忆/模式到系统提示词文件夹，供后续会话使用。"
        "当用户表达偏好（习惯、偏好、不喜欢）、发现项目特定约定、"
        "用户纠正你的错误并确认了正确做法时，应主动调用此工具。"
    )
    params = {
        "content": "要持久化的学习内容（1-3 句话即可）",
        "tags": "用于相关性匹配的标签列表，如 ['python', 'testing']",
        "category": "记忆分类: user-prefs（用户偏好）| project-rules（项目规则）| learned-patterns（经验教训）",
    }

    def execute(self, context: AgentContext) -> dict[str, Any]:
        """执行记忆持久化。

        Args:
            context: AgentContext，需包含 prompt_store 引用

        Returns:
            标准结果字典
        """
        content = self._extra.get("content", "").strip()
        tags = self._extra.get("tags", [])
        category = self._extra.get("category", "learned-patterns")

        if not content:
            return {
                "success": False,
                "error": "content 参数不能为空 — 请提供要持久化的学习内容（1-3 句话）",
            }

        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]

        if category not in ("user-prefs", "project-rules", "learned-patterns"):
            category = "learned-patterns"

        prompt_store = getattr(context, "prompt_store", None)
        if prompt_store is None:
            return {
                "success": False,
                "error": "PromptStore 未初始化，无法持久化记忆。请确认 REPL 已正确启动。",
            }

        try:
            entry = prompt_store.add_memory(
                name=category,
                content=content,
                tags=tags or [],
                priority="medium",
            )
            logger.info("Agent 通过 remember 工具持久化: %s → %s", category, content[:80])
            return {
                "success": True,
                "message": f"已持久化到 {entry.path}（{entry.token_estimate} tokens）",
                "category": category,
                "path": entry.path,
            }
        except Exception as e:
            logger.error("remember 工具执行失败: %s", e)
            return {
                "success": False,
                "error": f"持久化失败: {e}",
            }
