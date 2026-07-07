"""
P3-Q5 prompt_optimizer 意图收紧测试（§8.15.1/2/4/9）。

覆盖：
- §8.15.1：debug trigger 收紧强信号，移除过宽的「问题/issue」。
- §8.15.2：novel「续写」要求创作语境词，裸「续写」不再误判 novel。
- §8.15.4：补 write_doc / chat 意图；英文词形（writing/writes）。
- §8.15.9：模板已抽配置（TEMPLATES 列表）—— 校验可枚举。
"""

from __future__ import annotations

from omniagent.repl.prompt_optimizer import (
    TEMPLATES,
    detect_intent,
    get_intent_display,
    optimize_prompt,
)


class TestDebugTriggerTightened:
    """§8.15.1：debug trigger 收紧。"""

    def test_strong_signals_still_match(self):
        assert detect_intent("代码报错了怎么解决") == "debug"
        assert detect_intent("运行出错，有 bug") == "debug"
        assert detect_intent("为什么运行失败") == "debug"
        assert detect_intent("抛了异常 traceback") == "debug"
        assert detect_intent("fix this error") == "debug"
        assert detect_intent("there is a bug in my code") == "debug"

    def test_broad_word_no_longer_steals(self):
        """「问题/issue」单独出现不再误判为 debug。"""
        # 「我有个设计问题」—— 设计类讨论，不是报错调试
        assert detect_intent("我有个设计问题想讨论") != "debug"
        # 纯「issue」单词不再触发（除非伴随 fix/error 等）
        assert detect_intent("这是一个 issue") != "debug"

    def test_design_question_not_debug(self):
        assert detect_intent("关于架构的问题，想听听你的建议") != "debug"


class TestNovelXuXieRequiresContext:
    """§8.15.2：novel「续写」要求创作语境词。"""

    def test_xuxie_with_context_matches_novel(self):
        assert detect_intent("续写小说下一章") == "novel"
        assert detect_intent("接着写故事") == "novel"
        assert detect_intent("继续写下一章") == "novel"
        assert detect_intent("帮我续写大纲") == "novel"

    def test_bare_xuxie_not_novel(self):
        """裸「续写」无创作语境词，不再误判为 novel。"""
        assert detect_intent("续写") != "novel"
        assert detect_intent("接着写") != "novel"
        assert detect_intent("往下写") != "novel"

    def test_xuxie_function_is_write_code(self):
        """「继续写代码/功能」走 write_code，不被 novel 抢。"""
        assert detect_intent("继续写代码") == "write_code"

    def test_xuxie_doc_is_write_doc(self):
        """「续写文档」走 write_doc，不被 novel 抢。"""
        assert detect_intent("续写文档") == "write_doc"


class TestWriteDocIntent:
    """§8.15.4：补 write_doc 意图。"""

    def test_write_doc_chinese(self):
        assert detect_intent("帮我写一份文档") == "write_doc"
        assert detect_intent("编写 README") == "write_doc"
        assert detect_intent("编写 API 文档") == "write_doc"
        assert detect_intent("生成接口说明书") == "write_doc"

    def test_write_doc_english(self):
        assert detect_intent("write a doc for this module") == "write_doc"
        assert detect_intent("generate the README") == "write_doc"
        assert detect_intent("draft documentation") == "write_doc"

    def test_write_doc_optimization(self):
        optimized, hint, was_opt = optimize_prompt("帮我写一份文档")
        assert "## 文档目标" in optimized
        assert "文档结构" in optimized
        assert hint is not None
        assert was_opt is True

    def test_write_doc_not_write_code(self):
        """「帮我写文档」不应被 write_code 的 帮我写(?!测试|单测|文档) 拦截。"""
        assert detect_intent("帮我写文档") == "write_doc"


class TestChatIntent:
    """§8.15.4：补 chat 意图（仅纯问候/致谢）。"""

    def test_greetings_match_chat(self):
        assert detect_intent("你好") == "chat"
        assert detect_intent("您好！") == "chat"
        assert detect_intent("hi") == "chat"
        assert detect_intent("hello") == "chat"
        assert detect_intent("早上好。") == "chat"

    def test_thanks_match_chat(self):
        assert detect_intent("谢谢") == "chat"
        assert detect_intent("thanks") == "chat"
        assert detect_intent("多谢！") == "chat"

    def test_greeting_with_question_not_chat(self):
        """问候后带真实问题，不被 chat 抢。"""
        assert detect_intent("你好，我有个报错想问") != "chat"
        assert detect_intent("你好，帮我写一个函数") == "write_code"

    def test_non_greeting_not_chat(self):
        assert detect_intent("12345") is None
        assert detect_intent("代码报错") == "debug"


class TestEnglishWordForms:
    """§8.15.4：英文词形（writing/writes/written）。"""

    def test_writing_matches_write_code(self):
        assert detect_intent("continue writing the function") == "write_code"
        assert detect_intent("he writes a class for parsing") == "write_code"

    def test_written_form(self):
        # "well-written" 之类不一定触发，但 "write a"/"writing a" 应触发
        assert detect_intent("writing a parser") == "write_code"


class TestTemplatesConfigured:
    """§8.15.9：模板抽配置——所有意图可枚举且有显示名。"""

    def test_all_intents_have_display(self):
        for tmpl in TEMPLATES:
            display = get_intent_display(tmpl.intent)
            assert display != tmpl.intent

    def test_new_intents_present(self):
        intents = {t.intent for t in TEMPLATES}
        assert "write_doc" in intents
        assert "chat" in intents
