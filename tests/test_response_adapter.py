"""Tests for response_adapter — LLM 输出中间件。"""
from xenon.utils.response_adapter import (
    _extract_json,
    parse_plan,
    parse_react,
    parse_review,
)


# ── _extract_json ──────────────────────────────────────────────
class TestExtractJson:
    def test_raw_json(self):
        assert _extract_json('{"a": 1}') == {"a": 1}

    def test_markdown_json_block(self):
        text = '```json\n{"a": 1}\n```'
        assert _extract_json(text) == {"a": 1}

    def test_markdown_block_no_lang(self):
        text = '```\n{"a": 1}\n```'
        assert _extract_json(text) == {"a": 1}

    def test_json_with_prefix_text(self):
        text = 'Here is the result:\n{"a": 1}\nDone.'
        assert _extract_json(text) == {"a": 1}

    def test_returns_none_for_no_json(self):
        assert _extract_json("just plain text") is None

    def test_malformed_json(self):
        assert _extract_json("{broken json") is None

    def test_deepseek_dsml_parallel_tool_calls(self):
        text = (
            '<｜｜DSML｜｜tool_calls>'
            '<｜｜DSML｜｜invoke name="read_file">'
            '<｜｜DSML｜｜parameter name="file_path" string="true">'
            '/work/internal/provider'
            '</｜｜DSML｜｜parameter>'
            '</｜｜DSML｜｜invoke>'
            '<｜｜DSML｜｜invoke name="read_file">'
            '<｜｜DSML｜｜parameter name="file_path" string="true">'
            '/work/internal/tool'
            '</｜｜DSML｜｜parameter>'
            '</｜｜DSML｜｜invoke>'
            '</｜｜DSML｜｜tool_calls>'
        )

        assert _extract_json(text) == [
            {
                "action": "read_file",
                "action_input": {"file_path": "/work/internal/provider"},
            },
            {
                "action": "read_file",
                "action_input": {"file_path": "/work/internal/tool"},
            },
        ]


# ── parse_plan ─────────────────────────────────────────────────
class TestParsePlan:
    def test_standard_format(self):
        raw = '{"analysis": "分析", "steps": [{"id": 1, "task": "做X"}]}'
        result = parse_plan(raw)
        assert result["analysis"] == "分析"
        assert len(result["steps"]) == 1
        assert result["steps"][0]["id"] == 1
        assert result["steps"][0]["task"] == "做X"

    def test_task_variant(self):
        raw = '{"task": "任务描述", "steps": [{"step_number": 1, "description": "步骤1"}]}'
        result = parse_plan(raw)
        assert result["analysis"] == "任务描述"
        assert result["steps"][0]["id"] == 1
        assert result["steps"][0]["task"] == "步骤1"

    def test_summary_variant(self):
        raw = '{"summary": "摘要", "steps": [{"num": 1, "step": "行动"}]}'
        result = parse_plan(raw)
        assert result["analysis"] == "摘要"
        assert result["steps"][0]["id"] == 1
        assert result["steps"][0]["task"] == "行动"

    def test_string_steps(self):
        raw = '{"analysis": "X", "steps": ["步骤A", "步骤B"]}'
        result = parse_plan(raw)
        assert len(result["steps"]) == 2
        assert result["steps"][0]["task"] == "步骤A"
        assert result["steps"][1]["id"] == 2

    def test_no_json_returns_raw(self):
        result = parse_plan("just text")
        assert result["analysis"] == "just text"
        assert result["steps"] == []

    def test_markdown_code_block(self):
        raw = '```json\n{"task": "T", "steps": []}\n```'
        result = parse_plan(raw)
        assert result["analysis"] == "T"


# ── parse_react ────────────────────────────────────────────────
class TestParseReAct:
    def test_standard_format(self):
        raw = '{"thought": "想", "action": "run_code", "action_input": {"code": "x"}}'
        result = parse_react(raw)
        assert result["thought"] == "想"
        assert result["action"] == "run_code"
        assert result["action_input"] == {"code": "x"}

    def test_tool_variant(self):
        raw = '{"thinking": "分析", "tool": "execute_python", "args": {"code": "1+1"}}'
        result = parse_react(raw)
        assert result["thought"] == "分析"
        assert result["action"] == "execute_python"
        assert result["action_input"] == {"code": "1+1"}

    def test_final_answer(self):
        raw = '{"final_answer": "答案"}'
        result = parse_react(raw)
        assert result["final_answer"] == "答案"

    def test_answer_variant(self):
        raw = '{"answer": "42"}'
        result = parse_react(raw)
        assert result["final_answer"] == "42"

    def test_no_json_returns_raw(self):
        result = parse_react("plain text")
        # v0.6.2: 无法解析 JSON 时，内容放入 thought 而非 final_answer
        # 让引擎识别为 "需要进一步处理" 而非 "最终回答"
        assert result["thought"] == "plain text"
        assert result.get("final_answer", "") == ""

    def test_action_input_not_dict_fallback(self):
        raw = '{"action": "x", "action_input": "bad"}'
        result = parse_react(raw)
        assert result["action_input"] == {}


# ── parse_review ───────────────────────────────────────────────
class TestParseReview:
    def test_standard_format(self):
        raw = '{"pass": true, "score": 9, "feedback": "好"}'
        result = parse_review(raw)
        assert result["pass"] is True
        assert result["score"] == 9
        assert result["feedback"] == "好"

    def test_passed_variant(self):
        raw = '{"passed": false, "rating": 3, "comment": "差"}'
        result = parse_review(raw)
        assert result["pass"] is False
        assert result["score"] == 3
        assert result["feedback"] == "差"

    def test_string_pass(self):
        raw = '{"pass": "yes", "score": "7"}'
        result = parse_review(raw)
        assert result["pass"] is True
        assert result["score"] == 7

    def test_no_json_returns_raw(self):
        # B6: 解析失败默认不通过（防静默放行），score=0
        result = parse_review("looks good")
        assert result["feedback"] == "looks good"
        assert result["pass"] is False
        assert result["score"] == 0
