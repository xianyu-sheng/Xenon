"""Deterministic model-facing projection of a tool result.

The evidence/tracker view remains the original observation.  This module only
builds the shorter, structured text injected into the model prompt, with a
``result_id`` that points at the full result cache.
"""

from __future__ import annotations

import re
from typing import Any

MAX_MODEL_CHARS = 2400
_ERROR_RE = re.compile(
    r"(?:FAILED|ERROR|Traceback|AssertionError|E\s+|fatal:|permission denied|"
    r"错误|失败|异常|not found|不存在)",
    re.IGNORECASE,
)


def _clip_lines(text: str, *, head: int = 8, tail: int = 12) -> str:
    lines = str(text or "").splitlines()
    if len(lines) <= head + tail:
        return "\n".join(lines)
    return "\n".join(lines[:head] + ["…（中间已省略）…"] + lines[-tail:])


def _command_view(raw: dict[str, Any], evidence: str, result_id: str) -> str:
    stdout = str(raw.get("stdout") or raw.get("content") or "")
    stderr = str(raw.get("stderr") or "")
    text = stdout or stderr or evidence
    lines = text.splitlines()
    error_lines = [
        line.strip()
        for line in lines
        if _ERROR_RE.search(line) and line.strip()
    ][:12]
    body = _clip_lines(text, head=8, tail=12)
    exit_code = raw.get("returncode", raw.get("exit_code", "?"))
    success = bool(raw.get("success", False))
    parts = [
        f"[command] {'success' if success else 'failed'} | exit={exit_code}",
        f"result_id: {result_id}",
    ]
    if error_lines:
        parts.append("关键错误:")
        parts.extend(f"- {line[:220]}" for line in error_lines)
    parts.append("输出摘要:")
    parts.append(body)
    return "\n".join(parts)


def _read_view(raw: dict[str, Any], evidence: str, result_id: str) -> str:
    path = str(raw.get("file_path") or raw.get("path") or "")
    content = str(raw.get("content") or evidence or "")
    total = raw.get("total_lines")
    start = raw.get("from_line")
    end = raw.get("to_line")
    header = f"[read_file] {path}"
    if total is not None:
        header += f" | lines {start or 1}-{end or total}/{total}"
    header += f" | result_id: {result_id}"
    return f"{header}\n{_clip_lines(content, head=12, tail=12)}"


def _search_view(raw: dict[str, Any], evidence: str, result_id: str) -> str:
    matches = raw.get("matches") or []
    lines = []
    for item in matches[:30]:
        if isinstance(item, dict):
            lines.append(
                f"{item.get('file', '?')}:{item.get('line', '?')}: "
                f"{str(item.get('content', ''))[:180]}"
            )
        else:
            lines.append(str(item)[:180])
    if not lines:
        lines = [str(evidence or "（无匹配）")[:MAX_MODEL_CHARS]]
    return (
        f"[search_files] matches={raw.get('match_count', len(matches))} "
        f"returned={raw.get('returned_count', len(matches))} "
        f"truncated={bool(raw.get('truncated'))} | result_id: {result_id}\n"
        + "\n".join(lines[:30])
    )


def _list_view(raw: dict[str, Any], evidence: str, result_id: str) -> str:
    files = raw.get("files") or []
    lines = [str(item) for item in files[:50]]
    if not lines:
        lines = [str(evidence or "（空目录）")[:MAX_MODEL_CHARS]]
    return (
        f"[list_files] count={raw.get('count', len(files))} "
        f"returned={raw.get('returned_count', len(files))} "
        f"truncated={bool(raw.get('truncated'))} | result_id: {result_id}\n"
        + "\n".join(lines)
    )


def build_model_observation(
    tool: str,
    raw: Any,
    evidence: str,
    result_id: str,
) -> str | None:
    """Return a bounded structured view, or ``None`` to keep legacy text."""

    if not result_id:
        return None
    payload = raw if isinstance(raw, dict) else {}
    if tool in {"command", "run_script", "run_tests"}:
        view = _command_view(payload, evidence, result_id)
    elif tool == "read_file":
        view = _read_view(payload, evidence, result_id)
    elif tool == "search_files":
        view = _search_view(payload, evidence, result_id)
    elif tool == "list_files":
        view = _list_view(payload, evidence, result_id)
    elif tool in {"git", "github_fetch", "web_fetch", "docs_fetch"}:
        view = _command_view(payload, evidence, result_id)
    else:
        # Unknown tools keep the original observation; the cache is still
        # available through tool_result_read.
        return None
    suffix = "\n\n完整结果可按需读取：tool_result_read(result_id=...)"
    if len(view) > MAX_MODEL_CHARS:
        view = view[:MAX_MODEL_CHARS] + "\n…（模型视图已截断，完整内容在缓存）"
    return view + suffix
