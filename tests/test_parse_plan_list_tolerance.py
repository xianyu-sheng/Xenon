"""parse_plan 对 LLM 返回 JSON 数组的容错测试。

根因（SWE-bench 实测）：pylint plan-reflection 崩溃
`AttributeError: 'list' object has no attribute 'items'`。
LLM 规划时偶尔返回顶层 JSON 数组（误把 steps 当整体输出），
parse_plan → _pick(data) 假定 dict，对 list 调用 data.items() 崩溃，
整个引擎被杀。
"""

from __future__ import annotations

from xenon.utils.response_adapter import parse_plan


class TestParsePlanListTolerance:
    def test_top_level_list_with_dict_element(self) -> None:
        """数组首元素为 dict → 提取并正常解析。"""
        raw = (
            '[{"analysis":"修复 pylint","steps":['
            '{"id":1,"task":"读","tool":"read_file",'
            '"params":{"file_path":"pylint/lint/run.py"}}]}]'
        )
        plan = parse_plan(raw)
        assert isinstance(plan, dict)
        assert len(plan.get("steps", [])) == 1
        assert plan["steps"][0]["tool"] == "read_file"

    def test_top_level_list_no_dict(self) -> None:
        """数组无 dict 元素 → 空计划（不抛异常，调用方走格式重试）。"""
        raw = '["just", "a", "list", "of", "strings"]'
        plan = parse_plan(raw)
        assert isinstance(plan, dict)
        assert plan.get("steps") == []

    def test_empty_list(self) -> None:
        raw = "[]"
        plan = parse_plan(raw)
        assert isinstance(plan, dict)
        assert plan.get("steps") == []

    def test_normal_dict_still_works(self) -> None:
        """正常 dict 输入不受影响（零行为变化）。"""
        raw = (
            '{"analysis":"x","steps":['
            '{"id":1,"task":"t","tool":"command","params":{"action":"ls"}}]}'
        )
        plan = parse_plan(raw)
        assert len(plan.get("steps", [])) == 1
        assert plan["steps"][0]["tool"] == "command"

    def test_invalid_json_falls_back(self) -> None:
        """非 JSON 输入仍走原兜底（原始文本作 analysis）。"""
        plan = parse_plan("这不是 JSON")
        assert isinstance(plan, dict)
        assert "这不是 JSON" in plan.get("analysis", "")
