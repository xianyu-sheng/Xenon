"""Deterministic prompt compilation for provider cache-friendly requests."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


class CacheTier(str, Enum):
    STATIC = "static"
    SESSION_STABLE = "session_stable"
    HISTORY = "history"
    VOLATILE = "volatile"
    CURRENT = "current"


@dataclass(frozen=True)
class PromptSegment:
    index: int
    role: str
    tier: CacheTier
    estimated_tokens: int


@dataclass(frozen=True)
class CompiledPrompt:
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None
    segments: tuple[PromptSegment, ...]
    stable_prefix_messages: int
    expected_cacheable_tokens: int
    warnings: tuple[str, ...]

    def layout(self) -> dict[str, Any]:
        tier_tokens: dict[str, int] = {tier.value: 0 for tier in CacheTier}
        for segment in self.segments:
            tier_tokens[segment.tier.value] += segment.estimated_tokens
        return {
            "stable_prefix_messages": self.stable_prefix_messages,
            "expected_cacheable_tokens": self.expected_cacheable_tokens,
            "tier_token_estimates": tier_tokens,
            "warnings": list(self.warnings),
        }


_DYNAMIC_SYSTEM_PATTERN = re.compile(
    r"(?:\bcurrent\s+(?:time|date)\b|\btoday\b|\bnow\b|"
    r"当前(?:时间|日期)|今天是|现在是|"
    r"\b20\d{2}[-/]\d{1,2}[-/]\d{1,2}\b|"
    r"\b\d{1,2}:\d{2}(?::\d{2})?\b)",
    re.IGNORECASE,
)


def _content_text(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)


def _estimated_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4) if text else 0


def canonicalize_request_value(value: Any) -> Any:
    """Recursively stabilize mapping key order while preserving list semantics."""
    if isinstance(value, dict):
        return {
            str(key): canonicalize_request_value(value[key])
            for key in sorted(value, key=str)
        }
    if isinstance(value, list):
        return [canonicalize_request_value(item) for item in value]
    if isinstance(value, tuple):
        return [canonicalize_request_value(item) for item in value]
    return value


def _tool_name(tool: dict[str, Any]) -> str:
    function = tool.get("function", tool)
    return str(function.get("name", "")).lower()


def compile_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Return one canonical tool order independent of registry insertion order."""
    if not tools:
        return None
    canonical = [canonicalize_request_value(copy.deepcopy(tool)) for tool in tools]
    canonical.sort(
        key=lambda tool: (
            _tool_name(tool),
            json.dumps(tool, ensure_ascii=False, sort_keys=True, default=str),
        )
    )
    return canonical


def compile_prompt(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
) -> CompiledPrompt:
    """Compile without reordering messages or changing their semantic content."""
    compiled_messages = copy.deepcopy(list(messages))
    compiled_tools = compile_tools(tools)
    stable_prefix_messages = 0
    for message in compiled_messages:
        if message.get("role") != "system":
            break
        stable_prefix_messages += 1

    segments: list[PromptSegment] = []
    warnings: list[str] = []
    last_index = len(compiled_messages) - 1
    for index, message in enumerate(compiled_messages):
        role = str(message.get("role", "user"))
        content = _content_text(message)
        if index == last_index:
            tier = CacheTier.CURRENT
        elif index == 0 and role == "system":
            tier = CacheTier.STATIC
        elif index < stable_prefix_messages:
            tier = CacheTier.SESSION_STABLE
        elif role == "system":
            tier = CacheTier.VOLATILE
        else:
            tier = CacheTier.HISTORY
        if index < stable_prefix_messages and _DYNAMIC_SYSTEM_PATTERN.search(content):
            warnings.append(f"dynamic_stable_system:{index}")
        segments.append(PromptSegment(
            index=index,
            role=role,
            tier=tier,
            estimated_tokens=_estimated_tokens(content),
        ))

    expected_cacheable = sum(
        segment.estimated_tokens
        for segment in segments
        if segment.tier is not CacheTier.CURRENT
    )
    return CompiledPrompt(
        messages=compiled_messages,
        tools=compiled_tools,
        segments=tuple(segments),
        stable_prefix_messages=stable_prefix_messages,
        expected_cacheable_tokens=expected_cacheable,
        warnings=tuple(warnings),
    )
