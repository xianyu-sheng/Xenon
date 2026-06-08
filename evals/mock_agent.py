"""Deterministic mock agent used by CI evals."""

from __future__ import annotations

from typing import Any


def estimate_tokens(text: str) -> int:
    """Small deterministic token estimate for eval reporting."""
    ascii_words = len([part for part in text.split() if part])
    cjk_chars = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return max(1, ascii_words + int(cjk_chars * 1.5))


class MockAgent:
    """A stable agent stand-in that always uses the expected tools."""

    model_name = "mock-agent"

    def run_task(self, task: dict[str, Any]) -> dict[str, Any]:
        expected_tools = list(task.get("expected_tools", []))
        prompt = str(task.get("prompt", ""))
        token_count = estimate_tokens(prompt) + 80 + len(expected_tools) * 12
        return {
            "task_id": task["id"],
            "category": task["category"],
            "success": True,
            "model": self.model_name,
            "token_count": token_count,
            "tool_calls": len(expected_tools),
            "tool_failures": 0,
            "tools_used": expected_tools,
            "notes": "Mock agent used expected tools deterministically.",
        }
