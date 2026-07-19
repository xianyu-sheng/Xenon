"""
Prompt Optimizer 测试。
"""

from __future__ import annotations

import pytest

from xenon.repl.prompt_optimizer import (
    detect_intent,
    optimize_prompt,
    assess_quality,
    get_intent_display,
)


class TestDetectIntent:
    """测试意图识别。"""

    def test_write_code_chinese(self):
        assert detect_intent("帮我写一个快速排序") == "write_code"
        assert detect_intent("实现一个 HTTP 服务器") == "write_code"
        assert detect_intent("用 Python 写一个爬虫") == "write_code"
        assert detect_intent("创建一个 REST API") == "write_code"

    def test_write_code_english(self):
        assert detect_intent("write a function to sort a list") == "write_code"
        assert detect_intent("implement a binary search") == "write_code"
        assert detect_intent("create a web server") == "write_code"

    def test_debug_chinese(self):
        assert detect_intent("代码报错了怎么解决") == "debug"
        assert detect_intent("运行出错，有 bug") == "debug"
        assert detect_intent("为什么运行失败") == "debug"

    def test_debug_english(self):
        assert detect_intent("there is a bug in my code") == "debug"
        assert detect_intent("fix this error") == "debug"
        assert detect_intent("the program crashes") == "debug"

    def test_explain_chinese(self):
        assert detect_intent("解释一下这段代码") == "explain"
        assert detect_intent("这个函数是什么意思") == "explain"
        assert detect_intent("怎么理解这个算法") == "explain"

    def test_explain_english(self):
        assert detect_intent("explain this code") == "explain"
        assert detect_intent("what does this function do") == "explain"

    def test_refactor_chinese(self):
        assert detect_intent("重构这段代码") == "refactor"
        assert detect_intent("优化一下性能") == "refactor"
        assert detect_intent("有没有更好的写法") == "refactor"

    def test_refactor_english(self):
        assert detect_intent("refactor this function") == "refactor"
        assert detect_intent("optimize the performance") == "refactor"

    def test_write_test_chinese(self):
        assert detect_intent("帮我写单元测试") == "write_test"
        assert detect_intent("写一下测试用例") == "write_test"

    def test_write_test_english(self):
        assert detect_intent("write unit tests") == "write_test"
        assert detect_intent("create test cases") == "write_test"

    def test_design_chinese(self):
        assert detect_intent("怎么设计这个架构") == "design"
        assert detect_intent("帮我设计一个方案") == "design"

    def test_design_english(self):
        assert detect_intent("design the architecture") == "design"
        assert detect_intent("what is the best approach") == "design"

    def test_convert_chinese(self):
        assert detect_intent("把这段代码从 Java 转成 Python") == "convert"
        assert detect_intent("迁移这个项目到 FastAPI") == "convert"

    def test_unknown_intent(self):
        assert detect_intent("你好") == "chat"
        assert detect_intent("12345") is None

    def test_query_intent(self):
        assert detect_intent("今天天气怎么样") == "query"
        assert detect_intent("查询今天黄金的价格") == "query"


class TestAssessQuality:
    """测试提示词质量评估。"""

    def test_short_input_needs_optimization(self):
        needs, reason = assess_quality("帮我写")
        assert needs is True

    def test_structured_input_no_optimization(self):
        needs, reason = assess_quality("## 任务\n请帮我实现一个排序算法\n\n## 要求\n1. 使用 Python\n2. 时间复杂度 O(n log n)")
        assert needs is False

    def test_code_block_no_optimization(self):
        needs, reason = assess_quality("请帮我看看这段代码\n```python\nprint('hello')\n```")
        assert needs is False

    def test_long_detailed_input_no_optimization(self):
        needs, reason = assess_quality("请帮我实现一个完整的用户管理系统\n包含注册、登录、密码重置功能\n使用 FastAPI + SQLAlchemy\n数据库用 PostgreSQL")
        assert needs is False

    def test_casual_input_needs_optimization(self):
        needs, reason = assess_quality("帮我写一个快速排序")
        assert needs is True


class TestOptimizePrompt:
    """测试 prompt 优化。"""

    def test_write_code_optimization(self):
        optimized, hint, was_opt = optimize_prompt("帮我写一个快速排序")
        assert "快速排序" in optimized
        assert "## 任务" in optimized
        assert "## 要求" in optimized
        assert hint is not None
        assert was_opt is True

    def test_debug_optimization(self):
        optimized, hint, was_opt = optimize_prompt("代码报错了")
        assert "## 问题描述" in optimized
        assert "## 调试要求" in optimized
        assert hint is not None

    def test_explain_optimization(self):
        optimized, hint, was_opt = optimize_prompt("解释一下这段代码")
        assert "## 需要解释的内容" in optimized
        assert hint is not None

    def test_unknown_intent_passthrough(self):
        optimized, hint, was_opt = optimize_prompt("12345")
        assert optimized == "12345"
        assert hint is None
        assert was_opt is False

    def test_long_input_no_optimization(self):
        """长文本不优化，但仍提供 system hint。"""
        long_input = "请帮我实现一个完整的排序算法\n## 要求\n1. 使用 Python\n2. 时间复杂度 O(n log n)\n3. 包含单元测试"
        optimized, hint, was_opt = optimize_prompt(long_input)
        assert was_opt is False
        assert hint is not None

    def test_context_hints_injection(self):
        optimized, _, was_opt = optimize_prompt(
            "帮我写一个排序",
            context_hints={"file_path": "src/sort.py", "project_type": "Python库"},
        )
        if was_opt:
            assert "src/sort.py" in optimized
            assert "Python库" in optimized

    def test_lang_parameter(self):
        optimized, _, was_opt = optimize_prompt("写一个函数", lang="TypeScript")
        if was_opt:
            assert "TypeScript" in optimized


class TestGetIntentDisplay:
    """测试意图显示名。"""

    def test_known_intents(self):
        assert "编写代码" in get_intent_display("write_code")
        assert "调试" in get_intent_display("debug")
        assert "解释" in get_intent_display("explain")
        assert "重构" in get_intent_display("refactor")
        assert "测试" in get_intent_display("write_test")
        assert "架构" in get_intent_display("design")
        assert "转换" in get_intent_display("convert")
        assert "文档" in get_intent_display("write_doc")
        assert "闲聊" in get_intent_display("chat")

    def test_unknown_intent(self):
        assert "通用对话" in get_intent_display(None)
        assert "通用对话" in get_intent_display("unknown")

    def test_all_templates_have_display(self):
        from xenon.repl.prompt_optimizer import TEMPLATES
        for tmpl in TEMPLATES:
            display = get_intent_display(tmpl.intent)
            assert display != tmpl.intent  # 不应该返回原始 intent
