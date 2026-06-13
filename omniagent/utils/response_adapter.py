"""
LLM 响应适配器中间件
===================
将 LLM 的各种 JSON 输出格式统一转换为引擎期望的标准结构。
引擎只面向标准结构编程，不再关心 LLM 输出细节。
"""
from __future__ import annotations

import json
import re
from typing import Any


# ── 标准结构定义 ──────────────────────────────────────────────
def _step_template() -> dict:
    return {
        "id": 0,
        "task": "",
        "tool": None,
        "params": {},
        "description": "",
    }


def _analysis_template() -> dict:
    return {
        "analysis": "",
        "task": "",
        "summary": "",
        "steps": [],  # list[_step_template]
        "goal": "",
        "background": "",
    }


def _react_template() -> dict:
    return {
        "thought": "",
        "action": "",
        "action_input": {},
        "final_answer": "",
        "question": "",
        "options": [],
    }


def _reflection_template() -> dict:
    return {
        "pass": True,
        "score": 8,
        "feedback": "",
        "issues": [],
        "suggestions": [],
    }


def _reflection_plan_template() -> dict:
    return {
        "is_sufficient": False,
        "completeness_score": 0,
        "missing": [],
        "filled_plan": _analysis_template(),
    }


# ── 字段名映射表 ──────────────────────────────────────────────
# key: 标准字段名, value: 可能出现的别名列表（优先级从高到低）
_PLAN_FIELD_ALIASES = {
    "analysis": ["analysis", "task", "summary", "goal", "background", "description"],
    "task":     ["task", "analysis", "summary", "description"],
    "goal":     ["goal", "objective", "target"],
    "background": ["background", "context", "premise"],
}

_STEP_FIELD_ALIASES = {
    "id":          ["id", "step_number", "step_id", "num", "number", "index"],
    "task":        ["task", "description", "step", "action", "instruction", "content", "name"],
    "tool":        ["tool", "action", "tool_name", "command", "function", "method"],
    "params":      ["params", "parameters", "args", "arguments", "input", "kwargs"],
    "description": ["description", "detail", "details", "explain", "note"],
}

_REACT_FIELD_ALIASES = {
    "thought":       ["thought", "thinking", "reasoning", "reason", "analysis"],
    "action":        ["action", "tool", "command", "function", "method", "operation"],
    "action_input":  ["action_input", "input", "args", "parameters", "params", "arguments"],
    "final_answer":  ["final_answer", "answer", "result", "output", "response", "conclusion"],
    "question":      ["question", "query", "ask"],
    "options":       ["options", "choices", "alternatives"],
}

_REVIEW_FIELD_ALIASES = {
    "pass":         ["pass", "passed", "is_pass", "approved", "ok", "sufficient"],
    "score":        ["score", "rating", "grade", "points", "quality"],
    "feedback":     ["feedback", "comment", "review", "opinion", "comments", "suggestion"],
    "issues":       ["issues", "problems", "errors", "defects", "bugs"],
    "suggestions":  ["suggestions", "improvements", "recommendations", "fixes"],
    "is_sufficient": ["is_sufficient", "sufficient", "complete", "enough", "ready"],
    "completeness_score": ["completeness_score", "completeness", "score", "coverage"],
    "missing":      ["missing", "gaps", "lacks", "needed", "deficiencies"],
    "filled_plan":  ["filled_plan", "plan", "completed_plan", "full_plan", "result"],
}


# ── 核心工具函数 ──────────────────────────────────────────────
def _pick(data: dict, aliases: dict[str, list[str]], strict: bool = False) -> dict[str, Any]:
    """从 data 中按别名表提取字段，返回标准字段名→值的字典。

    Args:
        data: 原始 JSON dict
        aliases: {标准字段名: [别名1, 别名2, ...]}
        strict: 若为 True，只从 aliases 中指定的 key 提取；为 False 则保留 data 中所有 key
    """
    result = {}
    for std_name, alias_list in aliases.items():
        for alias in alias_list:
            if alias in data and data[alias] is not None:
                result[std_name] = data[alias]
                break
    if not strict:
        # 保留原始 data 中未被映射覆盖的字段
        mapped_values = set(aliases.keys())
        for k, v in data.items():
            if k not in result and k not in mapped_values:
                result[k] = v
    return result


def _normalize_step(step: Any, index: int) -> dict:
    """将单个步骤标准化为 _step_template 格式。"""
    if isinstance(step, str):
        return {**_step_template(), "id": index + 1, "task": step}
    if not isinstance(step, dict):
        return {**_step_template(), "id": index + 1, "task": str(step)}
    normalized = _pick(step, _STEP_FIELD_ALIASES)
    result = _step_template()
    result["id"] = normalized.get("id", index + 1)
    result["task"] = normalized.get("task", "")
    result["tool"] = normalized.get("tool")
    result["params"] = normalized.get("params", {})
    result["description"] = normalized.get("description", result["task"])
    return result


# ── JSON 提取 ─────────────────────────────────────────────────

def _repair_json(text: str) -> str | None:
    """尝试修复被截断或格式不完整的 JSON。

    策略：
    1. 截断未完成的值（去掉尾部不完整的 key/value）
    2. 关闭所有未闭合的 { 和 [
    3. 去掉尾部多余的逗号
    """
    if not text or not text.strip():
        return None

    text = text.strip()

    # 1. 分析引号状态，找到最后一个未闭合的字符串
    in_string = False
    escape_next = False
    last_quote_pos = -1
    for i, c in enumerate(text):
        if escape_next:
            escape_next = False
            continue
        if c == '\\':
            escape_next = True
            continue
        if c == '"':
            in_string = not in_string
            last_quote_pos = i

    # 2. 如果字符串未闭合，说明被截断了
    if in_string and last_quote_pos >= 0:
        prefix = text[:last_quote_pos]

        # 去掉不完整的字符串
        text = prefix.rstrip().rstrip(',')

        # 检查是否留下了一个孤立的 key: （值被截断的情况）
        # 如果去掉不完整字符串后，末尾是 ":"，说明截断了值，需要连 key 一起去掉
        # 例如: ...,"search_pattern": → 应该去掉整个 "search_pattern":
        stripped = text.rstrip()
        if stripped.endswith(':'):
            # 去掉冒号和前面的 key
            text = stripped[:-1].rstrip().rstrip(',')
            # 如果 key 带引号，去掉引号
            if text.endswith('"'):
                # 找到这个引号的匹配引号
                key_end = len(text) - 1
                key_start = text.rfind('"', 0, key_end)
                if key_start != -1:
                    text = text[:key_start].rstrip().rstrip(',')

    # 3. 去掉尾部逗号
    text = text.rstrip().rstrip(',')

    # 4. 用栈追踪未闭合的括号（保持正确的嵌套顺序）
    bracket_stack: list[str] = []
    in_str = False
    esc = False
    for c in text:
        if esc:
            esc = False
            continue
        if c == '\\':
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == '{':
            bracket_stack.append('{')
        elif c == '[':
            bracket_stack.append('[')
        elif c == '}':
            if bracket_stack and bracket_stack[-1] == '{':
                bracket_stack.pop()
        elif c == ']' and bracket_stack and bracket_stack[-1] == '[':
            bracket_stack.pop()

    # 5. 按正确的逆序关闭未闭合的括号
    close_map = {'{': '}', '[': ']'}
    for bracket in reversed(bracket_stack):
        text += close_map.get(bracket, '')

    return text


def _strip_model_markers(text: str) -> str:
    """剥离模型特有的非 JSON 标记（DSML、思考标签等）。"""
    # DeepSeek DSML 标记: DSML｜｜invoke>, DSML｜｜thought>, 等等
    text = re.sub(r"DSML[｜|]\s*[｜|]\s*\w+\s*>?", "", text)
    # DeepSeek 思考标记
    text = re.sub(r"<\|DSML\|>", "", text)
    # 常见的模型前缀/后缀噪声
    text = re.sub(r"^(Assistant|助手|AI)[：:]\s*", "", text, flags=re.MULTILINE)
    # XML 风格的思考块
    text = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL)
    text = re.sub(r"<thought>.*?</thought>", "", text, flags=re.DOTALL)
    return text.strip()


def _try_parse_json(text: str) -> dict | None:
    """尝试多种方式解析 JSON 文本，返回 dict 或 None。"""
    # 直接解析
    try:
        result = json.loads(text, strict=False)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, ValueError):
        pass

    # 尝试修复截断的 JSON
    repaired = _repair_json(text)
    if repaired:
        try:
            result = json.loads(repaired, strict=False)
            if isinstance(result, dict):
                return result
        except (json.JSONDecodeError, ValueError):
            pass

    return None


def _extract_json(text: str) -> dict | None:
    """从 LLM 输出中提取 JSON 对象 — 多策略回退提取。

    策略顺序（每个策略内部先尝试直接解析，再尝试修复）：
    1. 剥离 DSML/模型标记 → 提取 ```json 代码块
    2. 提取 ``` 代码块（无 json 标记）
    3. 剥离 DSML → 直接解析全文
    4. 全文 bbrace 提取（第一个 { 到最后一个 }）
    5. 从第一个 { 开始 — 截断修复
    6. 逐行查找含 "action" 或 "final_answer" 的 JSON 片段
    """
    text = text.strip()
    clean = _strip_model_markers(text)

    # ── 策略 1: ```json ... ``` 代码块 ──
    for source in (text, clean):
        m = re.search(r"```json\s*(.*?)\s*```", source, re.DOTALL)
        if m:
            result = _try_parse_json(m.group(1))
            if result:
                return result

    # ── 策略 2: ``` ... ``` 代码块 ──
    for source in (text, clean):
        for m in re.finditer(r"```\s*(\{.*?\})\s*```", source, re.DOTALL):
            result = _try_parse_json(m.group(1))
            if result:
                return result

    # ── 策略 3: 全文直接解析 ──
    result = _try_parse_json(clean)
    if result:
        return result

    # ── 策略 4: 第一个 { 到最后一个 } ──
    for source in (clean, text):
        brace_start = source.find("{")
        brace_end = source.rfind("}")
        if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
            candidate = source[brace_start:brace_end + 1]
            result = _try_parse_json(candidate)
            if result:
                return result

    # ── 策略 5: 从第一个 { 开始，修复截断 ──
    for source in (clean, text):
        brace_start = source.find("{")
        if brace_start != -1:
            candidate = source[brace_start:]
            repaired = _repair_json(candidate)
            if repaired:
                result = _try_parse_json(repaired)
                if result:
                    return result

    # ── 策略 6: 逐行查找 JSON 片段 ──
    for source in (clean, text):
        for line in source.split("\n"):
            line = line.strip()
            if not line:
                continue
            # 寻找独立的花括号 JSON（小片段）
            if line.startswith("{") and line.endswith("}"):
                result = _try_parse_json(line)
                if result and any(k in result for k in ("action", "final_answer", "thought")):
                    return result
            # 寻找花括号 JSON 在行中间
            if "{" in line and "}" in line:
                s = line.find("{")
                e = line.rfind("}") + 1
                fragment = line[s:e]
                result = _try_parse_json(fragment)
                if result and any(k in result for k in ("action", "final_answer", "thought")):
                    return result

    return None


# ── 公开 API ──────────────────────────────────────────────────
def parse_plan(raw: str) -> dict[str, Any]:
    """解析 LLM 输出为标准计划结构。

    Returns:
        {
            "analysis": str,       # 任务分析
            "task": str,           # 任务描述（同 analysis，兼容）
            "summary": str,        # 摘要
            "steps": [             # 标准化步骤列表
                {"id": int, "task": str, "tool": str|None, "params": dict, "description": str}
            ],
            "goal": str,
            "background": str,
        }
    """
    data = _extract_json(raw)
    if data is None:
        return {**_analysis_template(), "analysis": raw, "task": raw}

    result = _pick(data, _PLAN_FIELD_ALIASES)

    # 统一 analysis 字段
    if not result.get("analysis"):
        result["analysis"] = result.get("task", "") or result.get("summary", "")

    # 标准化 steps
    raw_steps = result.get("steps", [])
    if not isinstance(raw_steps, list):
        raw_steps = []
    result["steps"] = [_normalize_step(s, i) for i, s in enumerate(raw_steps)]

    # 确保模板字段存在
    for key, default in _analysis_template().items():
        result.setdefault(key, default)

    return result


def parse_react(raw: str) -> dict[str, Any]:
    """解析 LLM 输出为标准 ReAct 结构。

    增强策略:
    - JSON 提取失败时，返回 parse_error 标记（而非静默作为 final_answer）
    - 检测 DSML 格式的工具调用意图
    - 对模糊输出进行启发式判断

    Returns:
        {
            "thought": str,        # 思考过程
            "action": str,         # 工具名（空字符串=最终回答）
            "action_input": dict,  # 工具参数
            "final_answer": str,   # 最终回答
            "question": str,
            "options": list,
            "parse_error": bool,   # True 表示无法解析 JSON（引擎应要求重试）
        }
    """
    data = _extract_json(raw)
    if data is None:
        # ── JSON 提取完全失败 ──
        # 尝试检测 DSML 风格的工具调用意图
        dsml_action = _detect_dsml_action(raw)
        if dsml_action:
            return {
                **_react_template(),
                "thought": raw[:500],
                "action": dsml_action["action"],
                "action_input": dsml_action.get("action_input", {}),
            }

        # 尝试从非 JSON 文本中检测工具调用关键词
        tool_hint = _detect_tool_intent(raw)
        if tool_hint:
            return {
                **_react_template(),
                "thought": raw[:500],
                "action": tool_hint["action"],
                "action_input": tool_hint.get("action_input", {}),
            }

        # 完全无法解析 → 标记 parse_error，让引擎要求重试
        return {
            **_react_template(),
            "thought": raw[:1000],
            "parse_error": True,
            "raw": raw[:2000],
        }

    result = _pick(data, _REACT_FIELD_ALIASES)

    # 只为已存在但类型错误的字段提供类型修正
    if "action_input" in result and not isinstance(result["action_input"], dict):
        result["action_input"] = {}
    if "options" in result and not isinstance(result["options"], list):
        result["options"] = []

    # 兼容: 某些模型用 "tool" 或 "command" 字段代替 action
    if "action" not in result or not result.get("action"):
        for alt in ("tool", "command", "function"):
            if data.get(alt):
                result["action"] = str(data[alt])
                if "action_input" not in result:
                    result["action_input"] = data.get("args", data.get("params", data.get("input", {})))
                    if not isinstance(result["action_input"], dict):
                        result["action_input"] = {}
                break

    return result


def _detect_dsml_action(raw: str) -> dict | None:
    """从 DSML 格式输出中检测工具调用意图。

    DeepSeek 有时输出:
      DSML｜｜invoke> {"action": "command", "action_input": {...}}
      或
      我需要执行命令
      DSML｜｜invoke> command: git clone ...
    """
    # 尝试从 DSML 标记后提取 JSON
    m = re.search(r"DSML[｜|]\s*[｜|]\s*\w+\s*>?\s*(\{.+\})", raw, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1), strict=False)
            if isinstance(data, dict) and "action" in data:
                return data
        except (json.JSONDecodeError, ValueError):
            pass

    # DSML 后跟纯文本工具调用: tool_name: args
    m = re.search(r"DSML[｜|]\s*[｜|]\s*\w+\s*>?\s*(\w+)\s*[:：]\s*(.+)", raw)
    if m:
        tool_name = m.group(1).strip()
        tool_args = m.group(2).strip()
        if tool_name in _KNOWN_TOOL_NAMES:
            return {"action": tool_name, "action_input": {"command": tool_args}}

    return None


def _detect_tool_intent(raw: str) -> dict | None:
    """从非 JSON 文本中检测工具调用意图。

    当 LLM 输出类似 "我需要用 command 工具执行 git clone" 时，
    尝试提取工具名和参数。
    """
    raw_lower = raw.lower()
    for tool_name in _KNOWN_TOOL_NAMES:
        if tool_name in raw_lower:
            # 查找该工具的已知参数名
            tool_params = _TOOL_PARAM_HINTS.get(tool_name, {})
            extracted = {}
            for param_name in tool_params:
                # 尝试匹配 "param_name: value" 或 "param_name=value" 模式
                m = re.search(
                    rf'{param_name}\s*[:=]\s*["\']?([^"\'\,\n]+)["\']?',
                    raw, re.IGNORECASE,
                )
                if m:
                    extracted[param_name] = m.group(1).strip()
            if extracted:
                return {"action": tool_name, "action_input": extracted}
            # 即使没有提取到参数，也返回工具名让引擎尝试
            return {"action": tool_name, "action_input": {}}
    return None


# 已知工具名集合（用于 DSML 检测和意图检测）
_KNOWN_TOOL_NAMES: set[str] = {
    "command", "read_file", "write_file", "list_files", "search_files",
    "git", "web_fetch", "edit_file", "create_directory", "file_move",
    "file_copy", "batch_write", "batch_edit", "code_index", "ast_analyze",
    "refactor", "diff_preview", "mcp_call", "github_fetch",
    "weather", "datetime", "register_tool", "pytest", "run_test",
}

# 工具参数名提示（用于从非结构化文本中提取参数）
_TOOL_PARAM_HINTS: dict[str, set[str]] = {
    "command": {"command", "action"},
    "read_file": {"file_path", "path", "start_line", "max_lines"},
    "write_file": {"file_path", "path", "content"},
    "list_files": {"file_path", "path", "pattern"},
    "search_files": {"file_path", "path", "search_pattern", "pattern", "query"},
    "git": {"git_command", "command"},
    "web_fetch": {"url"},
    "edit_file": {"file_path", "path", "old_text", "new_text"},
    "create_directory": {"file_path", "path"},
    "file_move": {"source", "destination"},
    "file_copy": {"source", "destination"},
    "github_fetch": {"repo", "github_action", "github_path", "branch"},
    "pytest": {"test_path", "filter_expr"},
    "run_test": {"command", "timeout_seconds"},
}


def parse_review(raw: str) -> dict[str, Any]:
    """解析 LLM 输出为标准审查结构。

    Returns:
        {
            "pass": bool,          # 是否通过
            "score": int,          # 评分 0-10
            "feedback": str,       # 反馈意见
            "issues": list,        # 问题列表
            "suggestions": list,   # 改进建议
        }
    """
    data = _extract_json(raw)
    if data is None:
        return {**_reflection_template(), "feedback": raw}

    result = _pick(data, _REVIEW_FIELD_ALIASES)

    # 确保模板字段存在
    for key, default in _reflection_template().items():
        result.setdefault(key, default)

    # pass 字段兼容多种写法
    if isinstance(result["pass"], str):
        result["pass"] = result["pass"].lower() in ("true", "pass", "yes", "ok", "1")
    # score 确保是数字
    if not isinstance(result["score"], (int, float)):
        try:
            result["score"] = int(result["score"])
        except (ValueError, TypeError):
            result["score"] = 5

    return result


def parse_reflection_plan(raw: str) -> dict[str, Any]:
    """解析 LLM 输出为标准反思计划结构。

    Returns:
        {
            "is_sufficient": bool,
            "completeness_score": int,
            "missing": list,
            "filled_plan": {标准计划结构},
        }
    """
    data = _extract_json(raw)
    if data is None:
        return {
            **_reflection_plan_template(),
            "is_sufficient": False,
            "missing": ["无法解析 LLM 输出"],
        }

    result = _pick(data, _REVIEW_FIELD_ALIASES, strict=True)

    # 确保模板字段存在
    for key, default in _reflection_plan_template().items():
        result.setdefault(key, default)

    # is_sufficient 兼容
    if isinstance(result["is_sufficient"], str):
        result["is_sufficient"] = result["is_sufficient"].lower() in ("true", "yes", "1")

    # completeness_score 确保是数字
    if not isinstance(result["completeness_score"], (int, float)):
        try:
            result["completeness_score"] = int(result["completeness_score"])
        except (ValueError, TypeError):
            result["completeness_score"] = 0

    # missing 必须是 list
    if isinstance(result["missing"], str):
        result["missing"] = [result["missing"]]
    if not isinstance(result["missing"], list):
        result["missing"] = []

    # filled_plan 递归标准化
    fp = result.get("filled_plan")
    if isinstance(fp, dict):
        result["filled_plan"] = parse_plan(json.dumps(fp, ensure_ascii=False))
    elif isinstance(fp, str):
        result["filled_plan"] = parse_plan(fp)
    else:
        result["filled_plan"] = _analysis_template()

    return result
