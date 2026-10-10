"""Parameter hallucination validation (extracted from tool_executor).

Deterministic checks on LLM-provided tool parameters (function signatures,
illegal filename chars, code fragments mistaken for file content, ...).
"""

from __future__ import annotations

import re
from typing import Any

from xenon.nodes.tool_classify import _TOOL_CONTENT_PARAMS
from xenon.repl.system_config import get_config

_RE_FUNC_SIG = re.compile(r"\)\s*->\s*:|def\s+\w+\s*\([^)]*\)\s*:")
_RE_WIN_ILLEGAL = re.compile(r"[<>|*?\"]")
_RE_TRAILING_ILLEGAL = re.compile(r"[])}\"']+$")


def _chinese_ratio(s: str) -> float:
    if not s:
        return 0.0
    cjk = sum(1 for c in s if "一" <= c <= "鿿")
    return cjk / len(s)


def _code_structure_ratio(s: str) -> float:
    """代码结构字符（;{}()=<>）占比。"""
    if not s:
        return 0.0
    code_chars = sum(1 for c in s if c in ";{}()=<>")
    return code_chars / len(s)


def _balanced(s: str) -> bool:
    """括号/方括号是否配平。"""
    pairs = {"(": ")", "[": "]"}
    stack: list[str] = []
    for c in s:
        if c in pairs:
            stack.append(c)
        elif c in pairs.values():
            if not stack or pairs[stack.pop()] != c:
                return False
    return not stack


def _validate_param_value(name: str, value: Any) -> list[str]:
    """对单个非 content 参数做 7 类检查，返回命中的条件描述列表。

    v0.5.3: 放宽 shell here-doc、Python -c 和长命令的检查。
    """
    if not isinstance(value, str) or not value:
        return []

    # v0.5.3: 检测 shell here-doc 和 Python -c 模式，这些是合法的命令形式
    _looks_like_heredoc = bool(re.search(r"<<\s*['\"]?\w+['\"]?", value))
    _looks_like_python_c = bool(re.search(r"python\d*\s+-c\s", value))
    _is_shell_payload = _looks_like_heredoc or _looks_like_python_c

    hits: list[str] = []
    # v0.5.3: here-doc 和 python -c 中的代码不应被标记为函数签名
    if _RE_FUNC_SIG.search(value) and not _is_shell_payload:
        hits.append("疑似函数签名")
    if not _balanced(value):
        hits.append("括号不配平")
    if name in ("file_path", "path", "dir", "directory") and _RE_WIN_ILLEGAL.search(
        value
    ):
        hits.append("Windows 非法字符")
    # Shell commands commonly end in a quoted argument (for example
    # ``echo "done"``). A trailing quote is not evidence of a path
    # hallucination for command/action parameters; ToolNode's security parser
    # performs the real shell and dangerous-operation checks.
    if _RE_TRAILING_ILLEGAL.search(value) and name not in ("command", "action"):
        hits.append("末尾非法字符")
    # v0.5.3: command/action 放宽到 2000 字符（shell 命令和 Python -c 可以很长）
    # Complete diagnostic pipelines can legitimately exceed 200 characters.
    # Keep a bounded ceiling, but do not reject them before the command safety
    # layer gets a chance to inspect the actual operations.
    if name in ("command", "action"):
        cmd_max_len = 4096
    else:
        cmd_max_len = 2000 if _is_shell_payload else 200
    if name in ("file_path", "path", "command", "action") and len(value) > cmd_max_len:
        hits.append(f"超长(>{cmd_max_len})")
    if name in ("file_path", "path", "command") and _chinese_ratio(value) > 0.5:
        hits.append("中文占比过高")
    if _code_structure_ratio(value) > 0.3 and len(value) > 50:
        hits.append("纯代码结构")
    return hits


def validate_tool_params(
    params: dict[str, Any],
    tool_name: str | None = None,
    tool_gate: Any = None,
) -> tuple[bool, str, str]:
    """参数幻觉校验：三级判定，content 白名单豁免。

    合法的长 shell 命令 / heredoc / 含代码的参数容易命中 2 条结构性特征，
    旧的「≥2 即拦」会误杀并导致 LLM 收到拒绝后反复重试，故放宽为分级：

    - 命中 ≥3 条 → ``(False, reason, "block")``  拦截
    - 命中 ==2 条 → ``(True, reason, "warn")``   记日志但放行
    - 其余         → ``(True, "", "pass")``

    校验级别由 ToolGate 决定（优先级：工具级覆盖 > 全局配置 > validation.strict）：
    - strict: ≥2 条命中即 block
    - moderate: ≥3 条命中才 block（默认）
    - lenient: 不拦截，仅 warn

    Args:
        params: 工具参数字典
        tool_name: 工具名称（用于查询工具级配置）
        tool_gate: ToolGate 实例（None 时自动创建）

    Returns:
        (ok, reason, level) — level ∈ {"pass", "warn", "block"}。
    """
    # 获取校验级别
    if tool_gate is None:
        from xenon.engine.tool_gate import ToolGate

        tool_gate = ToolGate.from_config(get_config())

    if tool_name:
        level_enum = tool_gate.get_param_validation_level(tool_name)
        level_str = level_enum.value
    else:
        # 兜底：从全局配置读取
        strict = get_config().validation.strict
        level_str = "strict" if strict else "moderate"

    # 根据级别确定阈值
    if level_str == "strict":
        block_threshold = 2
    elif level_str == "lenient":
        block_threshold = 999  # 永不拦截
    else:  # moderate
        block_threshold = 3

    warn: tuple[bool, str, str] | None = None
    for name, value in params.items():
        if name in _TOOL_CONTENT_PARAMS:
            continue
        hits = _validate_param_value(name, value)
        if len(hits) >= block_threshold:
            return (
                False,
                f"参数 '{name}' 疑似 LLM 幻觉（命中: {'; '.join(hits)}）",
                "block",
            )
        if len(hits) >= 2 and warn is None:
            warn = (True, f"参数 '{name}' 参数可疑（命中: {'; '.join(hits)}）", "warn")
    if warn is not None:
        return warn
    return True, "", "pass"


# ── 错误分类 ───────────────────────────────────────────────
