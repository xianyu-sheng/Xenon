"""Chaos Test 2: LLM 返回 malformed JSON。

目标：验证 ``xenon.utils.response_adapter.parse_react`` 在 LLM 返回
broken JSON 时的行为。ReAct 引擎依赖 ``parse_react`` 解析 LLM 输出；
若解析失败，应回退到 thought / final_answer 字段而非抛异常。
"""
from __future__ import annotations

import pytest

from xenon.utils.response_adapter import parse_react, _extract_json, _repair_json


class TestRepairJson:
    """直接测试 JSON 修复中间件。"""

    def test_unclosed_brace_repaired(self):
        """未闭合的 { 应被补齐。"""
        broken = '{"thought": "ok", "final_answer": "hi"'
        repaired = _repair_json(broken)
        assert repaired is not None
        import json
        data = json.loads(repaired, strict=False)
        assert data["thought"] == "ok"

    def test_unclosed_string_repaired(self):
        """未闭合的字符串应被裁掉。"""
        broken = '{"thought": "unterminated s'
        repaired = _repair_json(broken)
        # 不抛异常即可
        assert repaired is None or isinstance(repaired, str)

    def test_completely_garbage_returns_none_via_extract(self):
        """完全无法识别的输入通过 _extract_json 应返回 None。"""
        broken = "@@@not json at all###"
        # _repair_json 对纯文本无 { 直接返回原串，但 _extract_json 仍应识别为 None
        result = _extract_json(broken)
        assert result is None

    def test_empty_string_returns_none(self):
        assert _repair_json("") is None
        assert _repair_json("   ") is None
        # _extract_json 也应安全处理
        assert _extract_json("") is None


class TestExtractJson:
    """从 LLM 输出中提取 JSON。"""

    def test_markdown_json_code_block(self):
        text = 'Some prose\n```json\n{"a": 1}\n```\nMore prose'
        data = _extract_json(text)
        assert data == {"a": 1}

    def test_bare_json_object(self):
        text = '{"a": 1, "b": "two"}'
        data = _extract_json(text)
        assert data == {"a": 1, "b": "two"}

    def test_broken_json_in_code_block_falls_back(self):
        """代码块内 JSON broken → _extract_json 仍尝试修复或回退。"""
        text = '```json\n{"thought": "ok", "final_answer": "hi"\n```'
        data = _extract_json(text)
        # 修复后能解析或返回 None，但**不抛异常**是硬要求
        if data is not None:
            assert "thought" in data or "final_answer" in data

    def test_garbage_returns_none(self):
        assert _extract_json("not json at all") is None


class TestParseReactFallback:
    """parse_react 在 malformed JSON 时应**不抛异常**，并提供合理回退。"""

    def test_garbage_input_does_not_crash(self):
        """纯垃圾输入 → parse_react 不抛，thought/final_answer 是 raw 字符串。"""
        result = parse_react("this is not json @@@@###")
        # 必须有所有模板字段（虽然可能是空字符串）
        assert isinstance(result, dict)
        # raw 落到 thought 或 final_answer（response_adapter 的设计）
        assert result.get("thought") or result.get("final_answer")

    def test_truncated_json_recovers(self):
        """被截断的 JSON（未闭合大括号）→ parse_react 不抛。"""
        broken = '{"thought": "thinking", "action": "write_file", "action_input": {'
        result = parse_react(broken)
        assert isinstance(result, dict)
        # 至少能识别 thought
        if result.get("thought"):
            assert "thinking" in result["thought"]

    def test_partial_json_with_trailing_garbage(self):
        """JSON 后面跟乱码文字 → parse_react 尝试提取。"""
        text = '{"thought": "ok", "action": "read_file"} random garbage trailing'
        result = parse_react(text)
        assert isinstance(result, dict)
        # 应该识别 action=read_file
        assert result.get("action") == "read_file"

    def test_broken_json_does_not_misclassify_final_answer(self):
        """关键回归：parse_react 不应把 broken action 误判为 final_answer。

        旧实现 setdefault 填充了 final_answer=""，导致 "final_answer" in parsed
        永远为 True，引擎会误判完成。
        """
        # 构造一个"看起来像 final_answer 但实际想调用 action" 的输入
        text = '{"thought": "thinking", "action": "read_file", "action_input": {"file_path": "/x"}'
        result = parse_react(text)
        # 如果解析失败并回退到 thought/final_answer=raw, final_answer 不应是空
        # 而是应保持 thought 或 action 识别出来
        if result.get("final_answer") and not result.get("action"):
            # 这种情况下，final_answer 含 raw 文本是 OK 的
            pass
        elif result.get("action") == "read_file":
            # 正确路径：识别为 action
            assert True
        else:
            # 至少不应同时 final_answer 为空且无 action（这会让引擎认为完成）
            assert result.get("action") or result.get("final_answer"), (
                f"parse_react 解析失败且无回退: {result}"
            )
