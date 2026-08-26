"""Validation and normalization for code returned directly to the chat."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class CodeResponseCheck:
    valid: bool
    content: str
    reason: str = ""


_FENCE_RE = re.compile(r"```([\w.+-]*)\s*\n(.*?)```", re.DOTALL)
_PYTHON_REQUEST = re.compile(r"(?:python|\.py\b)", re.IGNORECASE)
_PROTOCOL_MARKER = re.compile(
    r"<\|\|DSML\|\||<tool_calls?>|\"(?:tool|action_input)\"\s*:",
    re.IGNORECASE,
)


def validate_code_response(request: str, response: str) -> CodeResponseCheck:
    """Reject truncated/protocol output and return Markdown-safe code.

    Python responses receive a real ``ast.parse`` check.  Raw, valid Python is
    wrapped in a fenced block so Rich Markdown cannot reinterpret comments,
    separators, or indentation as prose.
    """

    content = response.strip()
    if not content:
        return CodeResponseCheck(False, response, "模型返回了空代码")
    if _PROTOCOL_MARKER.search(content.replace("｜", "|")):
        return CodeResponseCheck(False, response, "回复包含未执行的工具协议")
    if content.count("```") % 2:
        return CodeResponseCheck(False, response, "Markdown 代码块未闭合")

    fenced = _FENCE_RE.findall(content)
    is_python = bool(_PYTHON_REQUEST.search(request))
    if fenced:
        preferred = [item for item in fenced if item[0].lower() in {"python", "py"}]
        language, code = max(preferred or fenced, key=lambda item: len(item[1]))
        code = code.strip()
        if not code:
            return CodeResponseCheck(False, response, "代码块为空")
        if is_python or language.lower() in {"python", "py"}:
            try:
                ast.parse(code)
            except SyntaxError as exc:
                return CodeResponseCheck(
                    False,
                    response,
                    f"Python 代码不完整（第 {exc.lineno or '?'} 行语法错误）",
                )
            return CodeResponseCheck(True, f"```python\n{code}\n```")
        return CodeResponseCheck(True, f"```{language}\n{code}\n```")

    if is_python:
        try:
            ast.parse(content)
        except SyntaxError as exc:
            return CodeResponseCheck(
                False,
                response,
                f"Python 代码不完整（第 {exc.lineno or '?'} 行语法错误）",
            )
        return CodeResponseCheck(True, f"```python\n{content}\n```")

    # For other languages, require recognizable code structure.  Syntax-level
    # validation can be added per language without weakening this boundary.
    if not re.search(
        r"[{};]|\b(?:class|function|func|fn|const|let|var|import)\b", content
    ):
        return CodeResponseCheck(False, response, "回复不像完整代码")
    return CodeResponseCheck(True, f"```\n{content}\n```")
