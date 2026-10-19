"""Deterministic compression for tool-result context.

No model is used here.  Tool blocks are reduced to stable summaries plus
``result_id`` pointers; the full result remains in ToolResultStore.
"""

from __future__ import annotations

import json
import re
from typing import Any

from xenon.repl.context_strategies import ToolOutputClassifier

_RESULT_ID_RE = re.compile(r"\btr_[A-Za-z0-9_:-]+\b")
_FAILURE_RE = re.compile(
    r"(?:failed|failure|error|traceback|assertionerror|失败|错误|异常|exit\s*[=:]\s*[1-9])",
    re.IGNORECASE,
)
_MAX_LINES = 40
_MAX_LINE_CHARS = 360


def extract_result_id(text: str) -> str:
    match = _RESULT_ID_RE.search(str(text or ""))
    return match.group(0) if match else ""


def summarize_tool_content(tool: str, content: str, *, max_chars: int = 260) -> str:
    """Return a bounded deterministic summary of one tool result."""

    text = str(content or "")
    result_id = extract_result_id(text)
    failed = bool(_FAILURE_RE.search(text))
    status = "FAIL" if failed else "OK"
    # Prefer the existing deterministic per-tool classifier; it has no LLM
    # dependency and already preserves signatures/exit codes/paths.
    summary = ToolOutputClassifier.compress(tool, text, max_chars=max_chars)
    summary = " ".join(str(summary).split())
    if len(summary) > _MAX_LINE_CHARS:
        summary = summary[:_MAX_LINE_CHARS] + "…"
    pointer = f" result_id={result_id}" if result_id else ""
    return f"- {tool} [{status}]{pointer}: {summary}"


def summarize_native_block(block: list[dict[str, Any]]) -> list[str]:
    """Summarize one native ``assistant(tool_calls) + tool`` protocol block."""

    if not block:
        return []
    first = block[0]
    names: list[str] = []
    for call in first.get("tool_calls") or []:
        function = call.get("function", {}) if isinstance(call, dict) else {}
        name = str(function.get("name", "") or "")
        if name:
            names.append(name)
    if not names:
        names = ["tool"]
    tool_messages = [
        message for message in block if str(message.get("role", "")) == "tool"
    ]
    lines: list[str] = []
    for index, message in enumerate(tool_messages):
        tool = names[min(index, len(names) - 1)]
        content = message.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, default=str)
        lines.append(summarize_tool_content(tool, content))
    return lines


def summarize_dropped_blocks(dropped: list[list[dict[str, Any]]]) -> str:
    """Build a deterministic replacement for old native tool blocks."""

    lines: list[str] = []
    for block in dropped:
        lines.extend(summarize_native_block(block))
        if len(lines) >= _MAX_LINES:
            break
    omitted = max(0, len(dropped) - len(lines))
    header = (
        "[历史已压缩 · 结构化摘要] 为控制上下文，已省略较早的工具原文；"
        "以下保留工具名、状态、result_id 与关键摘要，完整结果可按 result_id 回查。"
    )
    if omitted:
        lines.append(f"- …另有 {omitted} 个工具块未展开；按需用 result_id 回查。")
    return header + "\n" + ("\n".join(lines) if lines else "- （无工具结果）")


def summarize_observation_text(text: str, *, max_chars: int = 500) -> str:
    """Compress a legacy ``Observation: [tool] ...`` message deterministically."""

    raw = str(text or "")
    match = re.match(r"^Observation:\s*(?:\[([^\]]+)\]\s*)?(.*)", raw, re.DOTALL)
    if match:
        tool = (match.group(1) or "tool").strip()
        body = match.group(2)
    else:
        tool = "tool"
        body = raw
    summary = summarize_tool_content(tool, body, max_chars=max_chars)
    return f"Observation: {summary}"
