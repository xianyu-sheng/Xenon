"""
LLM 响应适配器中间件
===================
将 LLM 的各种 JSON 输出格式统一转换为引擎期望的标准结构。
引擎只面向标准结构编程，不再关心 LLM 输出细节。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# ── 标准结构定义 ──────────────────────────────────────────────
def _step_template() -> dict:
    return {
        "id": 0,
        "task": "",
        "tool": None,
        "params": {},
        "description": "",
        "depends_on": [],  # P2-E2: 依赖的步骤 id 列表（DAG 拓扑排序用）
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
    "depends_on":  ["depends_on", "deps", "dependencies", "after", "requires", "prerequisite", "prerequisites"],
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
    # P2-E2: 归一化 depends_on 为列表（标量→单元素列表；缺失→空列表）。
    # int 化与未知依赖过滤交由 PlanDAG 处理。
    deps = normalized.get("depends_on")
    if deps is None:
        result["depends_on"] = []
    elif isinstance(deps, (list, tuple)):
        result["depends_on"] = list(deps)
    else:
        result["depends_on"] = [deps]
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
        elif c == ']':
            if bracket_stack and bracket_stack[-1] == '[':
                bracket_stack.pop()

    # 5. 按正确的逆序关闭未闭合的括号
    close_map = {'{': '}', '[': ']'}
    for bracket in reversed(bracket_stack):
        text += close_map.get(bracket, '')

    return text


def _split_adjacent_json_objects(text: str) -> list[dict] | None:
    """
    拆分 LLM 输出的多个相邻 JSON 对象（如 {...} {...}）。

    LLM 有时会输出多个 JSON 对象而不是一个 JSON 数组：
        {"action": "list_files", ...} {"action": "github_fetch", ...}
    这不是合法 JSON，但意图清晰——每个对象是一个独立工具调用。
    按 } 后紧跟空白 + { 的边界拆分，逐个解析。
    """
    # 找到所有 } ... { 边界位置
    boundaries: list[int] = []
    i = 0
    while i < len(text):
        brace_close = text.find("}", i)
        if brace_close == -1:
            break
        # 查找 } 之后的第一个 {
        after_close = brace_close + 1
        # 允许空白和换行
        j = after_close
        while j < len(text) and text[j] in (' ', '\n', '\r', '\t'):
            j += 1
        if j < len(text) and text[j] == '{':
            boundaries.append((brace_close, j))
            i = j
        else:
            i = after_close

    if not boundaries:
        return None

    # 按边界拆分并逐个解析
    objects: list[dict] = []
    prev_start = 0
    for close_pos, open_pos in boundaries:
        segment = text[prev_start:close_pos + 1].strip()
        if segment:
            try:
                obj = json.loads(segment, strict=False)
                if isinstance(obj, dict):
                    objects.append(obj)
            except json.JSONDecodeError:
                repaired = _repair_json(segment)
                if repaired:
                    try:
                        obj = json.loads(repaired, strict=False)
                        if isinstance(obj, dict):
                            objects.append(obj)
                    except json.JSONDecodeError:
                        pass
        prev_start = open_pos

    # 最后一个对象（从最后一个边界到文本末尾）
    last_segment = text[prev_start:].strip()
    if last_segment:
        try:
            obj = json.loads(last_segment, strict=False)
            if isinstance(obj, dict):
                objects.append(obj)
        except json.JSONDecodeError:
            repaired = _repair_json(last_segment)
            if repaired:
                try:
                    obj = json.loads(repaired, strict=False)
                    if isinstance(obj, dict):
                        objects.append(obj)
                except json.JSONDecodeError:
                    pass

    return objects if objects else None


def _extract_json(text: str) -> dict | list | None:
    """从 LLM 输出中提取 JSON 对象或数组，处理 markdown 代码块和多余文字。

    v0.5.0: 支持 JSON 数组（并行工具调用场景）。
    """
    text = text.strip()

    # 尝试 ```json ... ``` 代码块
    m = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if m:
        inner = m.group(1)
        try:
            return json.loads(inner, strict=False)
        except json.JSONDecodeError:
            repaired = _repair_json(inner)
            if repaired:
                try:
                    return json.loads(repaired, strict=False)
                except json.JSONDecodeError:
                    pass
            # v0.6.1: code block 内可能是多个相邻 JSON 对象
            adjacent = _split_adjacent_json_objects(inner)
            if adjacent:
                return adjacent

    # 尝试 ``` ... ``` 代码块（无 json 标记）
    m = re.search(r"```\s*(.*?)\s*```", text, re.DOTALL)
    if m:
        inner = m.group(1)
        try:
            return json.loads(inner, strict=False)
        except json.JSONDecodeError:
            repaired = _repair_json(inner)
            if repaired:
                try:
                    return json.loads(repaired, strict=False)
                except json.JSONDecodeError:
                    pass
            # v0.6.1: code block 内可能是多个相邻 JSON 对象
            adjacent = _split_adjacent_json_objects(inner)
            if adjacent:
                return adjacent

    # 直接尝试解析
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        pass

    # v0.5.4: JSON 对象优先于数组（对象是更常见的 LLM 响应格式）。
    # 比较首个 { 和 [ 的位置，取最外层结构，避免 final_answer 值中
    # 的内嵌数组截断外层对象（BUG-3 审计发现）。
    brace_start = text.find("{")
    bracket_start = text.find("[")
    brace_end = text.rfind("}") if brace_start != -1 else -1
    bracket_end = text.rfind("]") if bracket_start != -1 else -1

    # 对象优先：{ 在 [ 之前，或只有 {
    if brace_start != -1 and (bracket_start == -1 or brace_start < bracket_start):
        if brace_end > brace_start:
            try:
                return json.loads(text[brace_start:brace_end + 1], strict=False)
            except json.JSONDecodeError:
                pass
            # 尝试修复截断的 JSON
            candidate = text[brace_start:]
            repaired = _repair_json(candidate)
            if repaired:
                try:
                    return json.loads(repaired, strict=False)
                except json.JSONDecodeError:
                    pass

    # 数组次之：[ 在 { 之前，或只有 [
    if bracket_start != -1 and bracket_end > bracket_start:
        try:
            result = json.loads(text[bracket_start:bracket_end + 1], strict=False)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    # v0.6.1: 处理 LLM 输出的多个相邻 JSON 对象（如 {...} {...}），
    # 这些对象不是合法 JSON 数组（缺少逗号和方括号），前面的策略
    # "从第一个 { 取到最后一个 }" 会产生非法的混合文本。
    # 这里按 } 后紧跟空白 + { 的边界拆分，逐个解析为 list[dict]。
    adjacent_objects = _split_adjacent_json_objects(text)
    if adjacent_objects:
        return adjacent_objects

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


def parse_react(raw: str) -> dict[str, Any] | list[dict[str, Any]]:
    """解析 LLM 输出为标准 ReAct 结构。

    v0.5.0: 支持并行工具调用——当 LLM 返回 JSON 数组时，
    返回 list[dict] 供引擎并行执行。

    Returns:
        dict: 单工具调用或最终回答
        list[dict]: 并行工具调用列表
    """
    data = _extract_json(raw)
    if data is None:
        # v0.6.1: 解析失败时不要把原始模型输出设为 final_answer，
        # 否则引擎会将未解析的 JSON 文本直接展示给用户。
        # 将 raw text 只放在 thought 中，留空 final_answer 让引擎
        # 走到 "无有效 JSON 输出" 路径做二次处理。
        return {**_react_template(), "thought": raw}

    # v0.5.0: JSON 数组 → 并行工具调用
    if isinstance(data, list):
        actions = []
        for item in data:
            if isinstance(item, dict):
                act = _pick(item, _REACT_FIELD_ALIASES)
                if "action_input" in act and not isinstance(act["action_input"], dict):
                    act["action_input"] = {}
                actions.append(act)
        if actions:
            return actions
        return {**_react_template(), "thought": raw}

    result = _pick(data, _REACT_FIELD_ALIASES)

    # 只为已存在但类型错误的字段提供类型修正，不添加 LLM 未返回的默认值。
    # 之前这里用 setdefault 填充了所有模板字段（包括 final_answer=""），
    # 导致引擎的 "final_answer" in parsed 检查永远为 True，
    # 即使 LLM 实际返回的是 action 也会被误判为最终回答。
    if "action_input" in result and not isinstance(result["action_input"], dict):
        result["action_input"] = {}
    if "options" in result and not isinstance(result["options"], list):
        result["options"] = []

    return result


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
        # B6: 解析失败默认不通过（防静默放行），score=0 并记录
        logger.warning("parse_review: 无法从 LLM 输出解析 JSON，默认不通过")
        return {**_reflection_template(), "pass": False, "score": 0, "feedback": raw}

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
