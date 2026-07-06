"""F2 HollowDetector 单测。"""

from __future__ import annotations

from omniagent.engine.hollow_detector import HollowDetector, _HOLLOW_PATTERNS


class TestQuickFail:
    def test_empty_is_hollow(self):
        r = HollowDetector().detect("")
        assert r.is_hollow is True
        assert r.score == 1.0
        assert "quick_fail" in r.reasons[0]

    def test_none_is_hollow(self):
        r = HollowDetector().detect(None)
        assert r.is_hollow is True

    def test_whitespace_only_is_hollow(self):
        r = HollowDetector().detect("   \n  ")
        assert r.is_hollow is True

    def test_very_short_is_hollow(self):
        r = HollowDetector().detect("ok")
        assert r.is_hollow is True
        assert r.score == 1.0

    def test_exactly_4_chars_hollow(self):
        r = HollowDetector().detect("好的呢")  # 3 chars
        assert r.is_hollow is True


class TestDisproportionate:
    def test_many_tools_short_answer_hollow(self):
        # 8 chars ≥ quick_fail(5)，避开 quick_fail 专测 disproportionate
        hd = HollowDetector()
        r = hd.detect("已完成所有任务。", tool_call_count=7)
        assert r.is_hollow is True
        assert any("disproportionate" in x for x in r.reasons)

    def test_few_tools_short_answer_not_disproportionate(self):
        """工具调用 <5 时不触发 disproportionate（但可能被其他分支命中）。"""
        hd = HollowDetector()
        r = hd.detect("已完成。", tool_call_count=2)
        # "已完成。" 4 chars < quick_fail(5)? len=4 <5 → quick_fail 命中
        # 这里验证 disproportionate 不在 reasons 里
        assert not any("disproportionate" in x for x in r.reasons)

    def test_many_tools_long_answer_not_hollow(self):
        hd = HollowDetector()
        long = "基于工具执行结果，" + "详细汇报内容。" * 20  # >100 chars
        r = hd.detect(long, tool_call_count=10)
        # 长且可能含套话但有长度——检查 disproportionate 不命中（>100 chars）
        assert not any("disproportionate" in x for x in r.reasons)


class TestRegexCombo:
    def test_regex_hit_with_substance_not_hollow(self):
        """命中正则但够长且有代码块/路径 → 不判空洞（避免假阳）。"""
        hd = HollowDetector()
        text = (
            "综上所述，实现如下：\n```python\ndef main():\n    print('hello world')\n"
            "    return 42\n```\n已写入 src/main.py 文件，详见 https://example.com/repo。"
            "这是第一版实现，包含核心入口函数 main，后续可在此基础上扩展更多功能与测试覆盖。"
            "本次改动同时更新了相关单测 tests/test_f2_budget.py 与 tests/test_f2_hollow.py，"
            "确保三阶段预算边界、奖励封顶与 15 正则组合判定均有覆盖。"
        )
        assert len(text) >= 200  # 够长
        r = hd.detect(text, tool_call_count=1)
        assert r.is_hollow is False
        # hits 仍记录命中（可观测），但不判空洞
        assert "综上所述" in r.hits

    def test_regex_hit_short_no_substance_hollow(self):
        hd = HollowDetector()
        text = "接下来我将基于以上分析进行整体设计完善。"
        r = hd.detect(text, tool_call_count=0)
        assert r.is_hollow is True
        assert len(r.hits) >= 2
        assert r.score >= 0.7

    def test_regex_hit_long_no_substance_hollow(self):
        """长但全是套话无实质结构 → 仍判空洞（结构差分支）。"""
        hd = HollowDetector()
        text = "综上所述，基于以上分析，整体设计方案完善且合理。" * 10  # >200 chars
        r = hd.detect(text, tool_call_count=0)
        assert r.is_hollow is True
        assert any("结构差" in x for x in r.reasons)

    def test_regex_hit_long_with_substance_not_hollow(self):
        """长且有文件路径 → 不判空洞。"""
        hd = HollowDetector()
        text = (
            "综上所述，已修改如下文件：\n"
            "- omniagent/engine/budget.py：新增 BudgetManager 三阶段软预算与奖励机制\n"
            "- omniagent/engine/hollow_detector.py：新增 15 正则空洞检测器\n"
            "- omniagent/engine/base.py：接入 mercy compile 与合成注入\n"
            "测试在 tests/test_f2_budget.py 与 tests/test_f2_hollow.py，"
            "覆盖三阶段边界、奖励封顶、工具门控、15 正则、组合判定与 hint 生成。"
        )
        assert len(text) >= 200
        r = hd.detect(text, tool_call_count=3)
        assert r.is_hollow is False

    def test_first_second_last_pattern(self):
        hd = HollowDetector()
        text = "首先，分析需求。其次，设计方案。最后，给出结论。"
        r = hd.detect(text, tool_call_count=0)
        assert r.is_hollow is True
        assert "首先其次最后" in r.hits

    def test_ellipsis_fill(self):
        hd = HollowDetector()
        r = hd.detect("这个嘛……然后……", tool_call_count=0)
        assert r.is_hollow is True
        assert "省略号填充" in r.hits


class TestPatternCoverage:
    def test_15_patterns_exist(self):
        assert len(_HOLLOW_PATTERNS) == 15

    def test_each_pattern_compiles_and_matches(self):
        """每个正则至少能匹配其名称对应的样例。"""
        samples = {
            "接下来我将": "接下来我将开始",
            "基于以上分析": "基于以上分析可知",
            "整体设计完善": "整体设计较为完善",
            "综上所述": "综上所述",
            "总而言之": "总而言之",
            "需要注意的是": "需要注意的是",
            "在此基础上": "在此基础上",
            "通过以上": "通过以上步骤",
            "如下所示": "如下所示",
            "具体如下": "具体如下",
            "我认为/我觉得": "我认为应该",
            "建议你/您": "建议你这样做",
            "可以尝试": "可以尝试一下",
            "首先其次最后": "首先，A。其次，B。最后，C。",
            "省略号填充": "然后……",
        }
        for name, pat in _HOLLOW_PATTERNS:
            assert pat.search(samples[name]) is not None, f"{name} 未匹配样例"


class TestHint:
    def test_non_hollow_no_hint(self):
        # 代码块、无套话正则 → 非空洞，hint 为空
        r = HollowDetector().detect("```python\nprint(1)\n```", 0)
        assert r.is_hollow is False
        assert r.hint() == ""

    def test_hollow_hint_mentions_concrete(self):
        hd = HollowDetector()
        r = hd.detect("接下来我将设计", 0)
        h = r.hint()
        assert h.startswith("⚠️")
        assert "具体内容" in h

    def test_disproportionate_hint_mentions_tools(self):
        hd = HollowDetector()
        r = hd.detect("done.", tool_call_count=6)
        h = r.hint()
        assert "多次工具" in h or "工具" in h


class TestHasSubstance:
    def test_code_block_is_substance(self):
        assert HollowDetector().has_substance("```\ncode\n```") is True

    def test_file_path_is_substance(self):
        assert HollowDetector().has_substance("写入 main.py 文件") is True

    def test_url_is_substance(self):
        assert HollowDetector().has_substance("见 https://x.com/y") is True

    def test_inline_code_is_substance(self):
        assert HollowDetector().has_substance("运行 `pip install x` 即可") is True

    def test_plain_text_no_substance(self):
        assert HollowDetector().has_substance("这只是普通文字没有结构") is False
