"""Lazy access to the session-scoped full tool-result cache."""

from __future__ import annotations

import re
from typing import Any

_DEFAULT_MAX_CHARS = 4000


class ToolResultToolsMixin:
    """Mixin for ``tool_result_read``; never reads arbitrary workspace paths."""

    def _tool_result_read(self, context: Any) -> dict[str, Any]:
        store = context.get("_tool_result_store")
        if store is None:
            session_id = context.get("_session_id") or context.get("_repl_session_id")
            if session_id:
                from xenon.session.tool_results import ToolResultStore

                store = ToolResultStore(str(session_id))
                context.set("_tool_result_store", store)
        if store is None:
            return {
                "action_type": "tool_result_read",
                "success": False,
                "content": "",
                "error": "没有可用的工具结果缓存（未绑定会话）",
            }

        result_id = str(getattr(self, "result_id", "") or "").strip()
        mode = str(getattr(self, "mode", "summary") or "summary").strip().lower()
        if not result_id:
            return {
                "action_type": "tool_result_read",
                "success": False,
                "content": "",
                "error": "tool_result_read 需要 result_id",
            }
        meta = store.read_meta(result_id)
        if meta is None:
            return {
                "action_type": "tool_result_read",
                "success": False,
                "content": "",
                "error": f"缓存不存在或已过期: {result_id}",
            }

        if mode in {"summary", "meta"}:
            content = self._summary(meta)
        else:
            raw = store.read_raw(result_id)
            if raw is None:
                return {
                    "action_type": "tool_result_read",
                    "success": False,
                    "content": "",
                    "error": f"缓存缺少 raw 内容: {result_id}",
                }
            content = raw
            if mode in {"search", "grep"}:
                content = self._search(raw, str(getattr(self, "query", "") or ""))
            elif mode in {"range", "excerpt"}:
                content = self._range(
                    raw,
                    start_line=getattr(self, "start_line", None),
                    max_lines=getattr(self, "max_lines", None),
                )
            if len(content) > _DEFAULT_MAX_CHARS:
                content = content[:_DEFAULT_MAX_CHARS] + "\n…（按需读取已截断）"

        return {
            "action_type": "tool_result_read",
            "result_id": result_id,
            "mode": mode,
            "content": content,
            "success": True,
            "truncated": bool(meta.get("raw_truncated")),
            "raw_sha256": meta.get("raw_sha256", ""),
            "raw_bytes": meta.get("raw_bytes", 0),
            "metadata": {
                "tool": meta.get("tool"),
                "success": meta.get("success"),
                "status": meta.get("status"),
            },
        }

    @staticmethod
    def _summary(meta: dict[str, Any]) -> str:
        return (
            f"result_id: {meta.get('result_id')}\n"
            f"tool: {meta.get('tool')} | success={meta.get('success')} "
            f"| status={meta.get('status')}\n"
            f"summary: {meta.get('summary') or '（无摘要）'}\n"
            f"raw_bytes: {meta.get('raw_bytes', 0)} "
            f"| raw_sha256: {meta.get('raw_sha256', '')}"
        )

    @staticmethod
    def _range(raw: str, *, start_line: Any, max_lines: Any) -> str:
        lines = raw.splitlines()
        try:
            start = max(1, int(start_line or 1))
        except (TypeError, ValueError):
            start = 1
        try:
            limit = max(1, min(int(max_lines or 120), 1000))
        except (TypeError, ValueError):
            limit = 120
        page = lines[start - 1 : start - 1 + limit]
        return "\n".join(page)

    @staticmethod
    def _search(raw: str, query: str) -> str:
        query = query.strip()
        if not query:
            return raw
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error:
            pattern = re.compile(re.escape(query), re.IGNORECASE)
        matches = [
            f"{number}: {line}"
            for number, line in enumerate(raw.splitlines(), 1)
            if pattern.search(line)
        ]
        return "\n".join(matches[:100]) or "（无匹配行）"
