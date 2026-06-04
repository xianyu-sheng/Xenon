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
def _extract_json(text: str) -> dict | None:
    """从 LLM 输出中提取 JSON 对象，处理 markdown 代码块和多余文字。"""
    text = text.strip()

    # 尝试 ```json ... ``` 代码块
    m = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1), strict=False)
        except json.JSONDecodeError:
            pass

    # 尝试 ``` ... ``` 代码块（无 json 标记）
    m = re.search(r"```\s*(.*?)\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1), strict=False)
        except json.JSONDecodeError:
            pass

    # 直接尝试解析
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        pass

    # 找到第一个 { 和最后一个 }
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start != -1 and brace_end != -1:
        try:
            return json.loads(text[brace_start:brace_end + 1], strict=False)
        except json.JSONDecodeError:
            pass

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

    Returns:
        {
            "thought": str,        # 思考过程
            "action": str,         # 工具名（空字符串=最终回答）
            "action_input": dict,  # 工具参数
            "final_answer": str,   # 最终回答
            "question": str,
            "options": list,
        }
    """
    data = _extract_json(raw)
    if data is None:
        return {**_react_template(), "thought": raw, "final_answer": raw}

    result = _pick(data, _REACT_FIELD_ALIASES)

    # 确保模板字段存在
    for key, default in _react_template().items():
        result.setdefault(key, default)

    # action_input 必须是 dict
    if not isinstance(result.get("action_input"), dict):
        result["action_input"] = {}

    # options 必须是 list
    if not isinstance(result.get("options"), list):
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
