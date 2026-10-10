"""LLM transport errors (extracted from llm_client)."""

from __future__ import annotations


class ResponseTruncatedError(RuntimeError):
    """LLM 响应因 max_tokens 上限被截断，且续写次数耗尽仍不完整。"""

