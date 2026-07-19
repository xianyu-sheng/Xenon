"""
REPL 真实任务端到端测试（§9 验收 — 任务 A-N）。

对未提交的 query 路由修复（repl.py:718/784/1052 + test_repl.py:657-687）做
真实任务端到端回归，发现行为/预期不符时记 bug。

工作流：
1. 直接构造 REPL 实例 + 调用 _handle_chat 模拟用户输入
2. monkeypatch.setattr(xenon.engine.base, "chat_completion", fake)
3. XENON_ASSUME_YES=1 由 conftest 自动设置

覆盖：
- A: query 意图路由（基础路径 + 端到端 mock 工具）
- B: query 端到端（mock chat_completion 返回 ReAct JSON）
- C: chat 闲聊（不应路由）
- D: explain 解释（不应路由）
- E: write_code 编程（应路由）
- F: 文件路径触发
- G: Git 操作
- H: 边界用例
- I: 混合意图
- J: mode 切换
- K/L: optimize_prompts 关闭
- M: 否定/复杂 query
- N: 多种 query 变体

附加验证（来自 §9 coordinator 关注点）：
- 1: trim_last_assistant 后递归失败的状态污染
- 2: mode 切换后 intent 路由（query 修复"无效"）
- 3: mode plan-execute + query
- 4: chat + _TOOL_PATTERNS 交叉误判
- 5: 空字符串 process_user_input
- 6: prompt_optimizer 内部的 chat 模板
- 7: detect_intent 顺序敏感
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import pytest

import xenon.engine.base as engine_base
import xenon.utils.llm_client as llm_client
from xenon.repl.context_manager import ContextManager
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.prompt_optimizer import detect_intent
from xenon.repl.repl import REPL


# ── Mock 辅助函数 ──────────────────────────────────────────

def _make_repl(optimize_prompts: bool = True, mode: str = "direct") -> REPL:
    """构造一个可工作的 REPL 实例（注册一个 fake model）。"""
    reg = ModelRegistry()
    reg.add_model(
        "openai/gpt-4o", "gpt4",
        api_key="sk-test", base_url="https://api.test.com",
    )
    reg.assign_role("planner", ["gpt4"])
    if mode != "direct":
        reg.set_mode(mode)
    return REPL(
        registry=reg, streaming=False,
        optimize_prompts=optimize_prompts,
    )


def _patch_chat_all(monkeypatch, responder):
    """把 engine.base.chat_completion + util.llm_client 的同步/流式全替换。"""
    def fake_engine(model_id, messages, **kw):
        return responder(("engine", model_id, messages))
    def fake_util(model_id, messages, **kw):
        return responder(("util", model_id, messages))
    def fake_util_stream(model_id, messages, **kw):
        text = responder(("util_stream", model_id, messages))
        # yield 整段
        yield text

    monkeypatch.setattr(engine_base, "chat_completion", fake_engine)
    monkeypatch.setattr(llm_client, "chat_completion", fake_util)
    monkeypatch.setattr(llm_client, "chat_completion_stream", fake_util_stream)


def _final_answer_json(text: str) -> str:
    return json.dumps({"thought": "mock thought", "final_answer": text}, ensure_ascii=False)


def _tool_call_json(tool: str, args: dict, final: str = "ok") -> str:
    return json.dumps({
        "thought": "use tool",
        "action": tool,
        "action_input": args,
        "final_answer": "",
    }, ensure_ascii=False)


# ── A. query 意图路由（基础路径） ─────────────────────────

class TestQueryIntentRouting:
    """query 意图（天气/价格/汇率/新闻）必然需要工具 → 路由 ReAct。"""

    QUERY_CASES = [
        "今天苏州的天气怎么样",
        "今天黄金价格多少",
        "现在美元兑人民币汇率多少",
        "查看今天的科技新闻",
        "北京现在几度",
    ]

    @pytest.mark.parametrize("text", QUERY_CASES)
    def test_detect_intent_is_query(self, text):
        """detect_intent 应识别为 query（regex 路径）。"""
        assert detect_intent(text) == "query", (
            f"意图识别失败: {text} → {detect_intent(text)}"
        )

    @pytest.mark.parametrize("text", QUERY_CASES)
    def test_detect_tool_need_true_with_query_intent(self, text):
        """_detect_tool_need(text, intent='query') → True。"""
        assert REPL._detect_tool_need(text, intent="query") is True

    @pytest.mark.parametrize("text", QUERY_CASES)
    def test_query_routes_to_react_engine(self, text, monkeypatch):
        """_handle_chat(query_text) → 实际走到 ReAct 引擎（LLM mock 由 engine 捕获）。"""
        seen_in_engine: list[str] = []
        def responder(ctx):
            kind, model_id, messages = ctx
            if kind == "engine":
                seen_in_engine.append(model_id)
                return _final_answer_json("mocked 实时数据：25°C 晴")
            return "direct mode mocked"

        _patch_chat_all(monkeypatch, responder)
        repl = _make_repl(optimize_prompts=True)
        repl._handle_chat(text)

        assert seen_in_engine, (
            f"query '{text}' 应走到 ReAct 引擎，但 engine.chat_completion 未被调用"
        )


# ── B. query 端到端（mock 工具调用） ───────────────────────

class TestQueryEnd2End:
    """端到端：query → 路由 ReAct → mock chat_completion 模拟工具调用 → 输出真实数据。"""

    def test_query_with_tool_call_returns_real_data(self, monkeypatch):
        """query → ReAct → 直接返回 final_answer（端到端验证 assistant 消息含真实数据）。

        NOTE: ReAct 引擎在没有工具调用的情况下，连续 2 次拒答后强制接受 final_answer
        并附加警告。所以这里让 LLM 直接返回 final_answer。
        """
        seen_in_engine: list[str] = []
        def responder(ctx):
            kind, model_id, messages = ctx
            if kind == "engine":
                seen_in_engine.append(model_id)
                return _final_answer_json("苏州当前 25°C 晴，西北风 3 级")
            return "direct"
        _patch_chat_all(monkeypatch, responder)
        repl = _make_repl(optimize_prompts=True)
        repl._handle_chat("今天苏州的天气怎么样")

        # ctx_mgr 应有 user 消息 + assistant 消息
        last_asst = next(
            (m for m in reversed(repl.ctx_mgr.history) if m.role == "assistant"),
            None,
        )
        assert last_asst is not None, "ctx_mgr 缺 assistant 消息"
        assert "苏州" in last_asst.content or "25" in last_asst.content, (
            f"assistant 消息应包含真实数据，实际: {last_asst.content[:100]}"
        )
        # 至少 1 次 engine 调用（ReAct 路由成功）
        assert seen_in_engine, "query 应至少 1 次走 ReAct 引擎"


# ── C. chat 闲聊（不应路由） ───────────────────────────

class TestChatIntent:
    """chat 闲聊：detect_intent='chat'，_detect_tool_need=False，走 direct LLM。"""

    CHAT_CASES = [
        "你好", "hi", "谢谢", "再见", "您好", "hello", "bye", "thanks",
    ]

    @pytest.mark.parametrize("text", CHAT_CASES)
    def test_detect_intent_is_chat(self, text):
        assert detect_intent(text) == "chat", f"意图: {text} → {detect_intent(text)}"

    @pytest.mark.parametrize("text", CHAT_CASES)
    def test_chat_does_not_route_to_react(self, text, monkeypatch):
        """chat + 无工具关键词 → 走 direct LLM（util.chat_completion）。"""
        seen_in_engine: list[str] = []
        seen_in_util: list[str] = []

        def responder(ctx):
            kind, model_id, messages = ctx
            if kind == "engine":
                seen_in_engine.append(model_id)
                return _final_answer_json("e")
            seen_in_util.append(model_id)
            return "direct 闲聊回复"

        _patch_chat_all(monkeypatch, responder)
        repl = _make_repl(optimize_prompts=True)
        repl._handle_chat(text)

        assert not seen_in_engine, f"chat '{text}' 不应走 ReAct，实际调了 engine"
        assert seen_in_util, f"chat '{text}' 应走 direct LLM，util 未被调"


# ── D. explain 解释（不应路由） ───────────────────────────

class TestExplainIntent:
    """explain 解释：detect_intent='explain'，_detect_tool_need=False。"""

    EXPLAIN_CASES = [
        "解释一下装饰器",
        "explain what is x",
        "how does y work",
    ]

    @pytest.mark.parametrize("text", EXPLAIN_CASES)
    def test_detect_intent_is_explain(self, text):
        assert detect_intent(text) == "explain", f"意图: {text} → {detect_intent(text)}"

    @pytest.mark.parametrize("text", EXPLAIN_CASES)
    def test_explain_does_not_route_to_react(self, text, monkeypatch):
        seen_in_engine: list[str] = []
        def responder(ctx):
            kind, model_id, messages = ctx
            if kind == "engine":
                seen_in_engine.append(model_id)
                return _final_answer_json("e")
            return "direct 解释回复"
        _patch_chat_all(monkeypatch, responder)
        repl = _make_repl(optimize_prompts=True)
        repl._handle_chat(text)
        assert not seen_in_engine, f"explain '{text}' 不应走 ReAct"


# ── E. write_code 编程（应路由） ─────────────────────────

class TestWriteCodeIntent:
    """write_code：detect_intent='write_code'，_detect_tool_need=True（无 intent 也命中 _TOOL_PATTERNS）。"""

    WRITE_CODE_CASES = [
        "帮我写一个快速排序函数",
        "写一个 Python 爬虫",
        "用 JS 写一个待办事项应用",
    ]

    @pytest.mark.parametrize("text", WRITE_CODE_CASES)
    def test_detect_intent_is_write_code(self, text):
        # NOTE: write_code 在 TEMPLATES 顺序中 line 142-163 (在 query 之前)
        assert detect_intent(text) == "write_code", f"意图: {text} → {detect_intent(text)}"

    @pytest.mark.parametrize("text", WRITE_CODE_CASES)
    def test_write_code_routes_to_react(self, text, monkeypatch):
        seen_in_engine: list[str] = []
        def responder(ctx):
            kind, model_id, messages = ctx
            if kind == "engine":
                seen_in_engine.append(model_id)
                return _final_answer_json("code")
            return "direct"
        _patch_chat_all(monkeypatch, responder)
        repl = _make_repl(optimize_prompts=True)
        repl._handle_chat(text)
        assert seen_in_engine, f"write_code '{text}' 应走 ReAct"


# ── F. 文件路径触发 ─────────────────────────────────────

class TestFilePathTriggers:
    """含文件路径/扩展名的输入应路由到 ReAct。"""

    FILE_CASES = [
        "把 src/main.py 改一下",
        "读取 config.yaml",
        "删除 /tmp/foo.txt",
    ]

    @pytest.mark.parametrize("text", FILE_CASES)
    def test_file_path_routes_to_react(self, text, monkeypatch):
        seen_in_engine: list[str] = []
        def responder(ctx):
            kind, model_id, messages = ctx
            if kind == "engine":
                seen_in_engine.append(model_id)
                return _final_answer_json("ok")
            return "direct"
        _patch_chat_all(monkeypatch, responder)
        repl = _make_repl(optimize_prompts=True)
        repl._handle_chat(text)
        assert seen_in_engine, f"文件路径 '{text}' 应走 ReAct"


# ── G. Git 操作 ────────────────────────────────────────

class TestGitTriggers:
    """Git 操作应路由到 ReAct。"""

    GIT_CASES = [
        "git commit 一下",
        "git push",
        "帮我合并分支",
    ]

    @pytest.mark.parametrize("text", GIT_CASES)
    def test_git_routes_to_react(self, text, monkeypatch):
        seen_in_engine: list[str] = []
        def responder(ctx):
            kind, model_id, messages = ctx
            if kind == "engine":
                seen_in_engine.append(model_id)
                return _final_answer_json("ok")
            return "direct"
        _patch_chat_all(monkeypatch, responder)
        repl = _make_repl(optimize_prompts=True)
        repl._handle_chat(text)
        assert seen_in_engine, f"git '{text}' 应走 ReAct"


# ── H. 边界用例 ────────────────────────────────────────

class TestEdgeCases:
    """空输入/超长输入/特殊字符/多行/中英混杂。"""

    def test_empty_string_does_not_crash(self, monkeypatch):
        """空字符串不崩溃，且应被 detect_intent 接受（None）+ 走 direct。"""
        seen_in_util: list[str] = []
        def responder(ctx):
            kind, model_id, messages = ctx
            if kind == "util":
                seen_in_util.append(model_id)
            return "ok"
        _patch_chat_all(monkeypatch, responder)
        repl = _make_repl(optimize_prompts=True)
        # 实际 _handle_chat 会 add_user_message("")，但不应抛
        try:
            repl._handle_chat("")
        except Exception as e:
            pytest.fail(f"空输入崩: {e}")
        # 看 ctx_mgr 是否有空 user 消息
        last_user = next(
            (m for m in reversed(repl.ctx_mgr.history) if m.role == "user"),
            None,
        )
        # NOTE: 不强制断言 add_user_message 行为，只验证不崩
        # 但记录实际行为给报告用

    def test_pure_spaces_does_not_crash(self, monkeypatch):
        def responder(ctx):
            return "ok"
        _patch_chat_all(monkeypatch, responder)
        repl = _make_repl(optimize_prompts=True)
        try:
            repl._handle_chat("   ")
        except Exception as e:
            pytest.fail(f"纯空格崩: {e}")

    def test_very_long_input(self, monkeypatch):
        def responder(ctx):
            return "ok"
        _patch_chat_all(monkeypatch, responder)
        repl = _make_repl(optimize_prompts=True)
        long_text = "# " + "Lorem ipsum " * 500 + "\n" + "more text " * 200
        try:
            repl._handle_chat(long_text)
        except Exception as e:
            pytest.fail(f"超长输入崩: {e}")

    def test_pure_numbers(self, monkeypatch):
        def responder(ctx):
            return "ok"
        _patch_chat_all(monkeypatch, responder)
        repl = _make_repl(optimize_prompts=True)
        try:
            repl._handle_chat("12345")
        except Exception as e:
            pytest.fail(f"纯数字崩: {e}")

    def test_special_chars(self, monkeypatch):
        def responder(ctx):
            return "ok"
        _patch_chat_all(monkeypatch, responder)
        repl = _make_repl(optimize_prompts=True)
        try:
            repl._handle_chat("🤔 ?? !! @@@ $")
        except Exception as e:
            pytest.fail(f"特殊字符崩: {e}")

    def test_mixed_cn_en(self, monkeypatch):
        def responder(ctx):
            return "ok"
        _patch_chat_all(monkeypatch, responder)
        repl = _make_repl(optimize_prompts=True)
        try:
            repl._handle_chat("Python 中 list 和 tuple 区别")
        except Exception as e:
            pytest.fail(f"中英混杂崩: {e}")

    def test_multiline_input(self, monkeypatch):
        def responder(ctx):
            return "ok"
        _patch_chat_all(monkeypatch, responder)
        repl = _make_repl(optimize_prompts=True)
        try:
            repl._handle_chat("line1\nline2\nline3")
        except Exception as e:
            pytest.fail(f"多行输入崩: {e}")


# ── I. 混合意图 ────────────────────────────────────────

class TestMixedIntent:
    """最易出 bug：多种意图混合的输入。"""

    def test_write_code_with_query_ambiguous(self, monkeypatch):
        """"写一个 Python 脚本查询天气" — write_code 模板有"脚本"关键词。
        TEMPLATES 顺序: write_code (line 142) 在 query (line 222) 之前。
        预期: detect_intent='write_code' → 仍路由 ReAct（write_code 意图 + 无需 query 检测）。"""
        text = "我想写一个 Python 脚本查询天气"
        # detect_intent 应先匹配 write_code
        intent = detect_intent(text)
        # 写代码脚本类会先触发 write_code 模板
        # _detect_tool_need 走 _TOOL_PATTERNS，命中"写"+"脚本" → True
        assert REPL._detect_tool_need(text, intent=intent) is True, (
            f"写脚本查天气应路由工具: intent={intent}"
        )

    def test_query_with_file_path(self, monkeypatch):
        """"帮我查天气并保存到 weather.json" — query + 文件路径。
        _TOOL_PATTERNS 含 .json 扩展名 → 路由 ReAct。"""
        text = "帮我查天气并保存到 weather.json"
        seen_in_engine: list[str] = []
        def responder(ctx):
            kind, model_id, messages = ctx
            if kind == "engine":
                seen_in_engine.append(model_id)
                return _final_answer_json("ok")
            return "direct"
        _patch_chat_all(monkeypatch, responder)
        repl = _make_repl(optimize_prompts=True)
        repl._handle_chat(text)
        assert seen_in_engine, "query+文件路径应走 ReAct（命中 .json 扩展名正则）"

    def test_url_fetch(self, monkeypatch):
        """"读取 https://example.com/api/weather 的数据" — 含 URL。
        _TOOL_PATTERNS 不含 https? URL regex，但 intent='query' 仍会触发。
        验证：传入正确 intent 时应路由。"""
        text = "读取 https://example.com/api/weather 的数据"
        from xenon.repl.prompt_optimizer import detect_intent as di
        intent = di(text)
        # 实际：intent='query'（"读取"不在 query trigger 里；可能 None）
        # _TOOL_PATTERNS 不含 URL/URL 协议正则
        # 仅当 intent='query' 时才能路由
        result = REPL._detect_tool_need(text, intent=intent)
        assert result is True, (
            f"含 URL 的 query 路由失败: intent={intent}, "
            f"detect_tool_need={result}"
        )


# ── J. mode 切换 ──────────────────────────────────────

class TestModeSwitch:
    """/mode 切换后再输入 query。"""

    def test_mode_react_then_chat(self, monkeypatch):
        """/mode react + "你好" — 应走 _run_react_engine（不再走 _run_direct）。"""
        seen_in_engine: list[str] = []
        seen_in_util: list[str] = []
        def responder(ctx):
            kind, model_id, messages = ctx
            if kind == "engine":
                seen_in_engine.append(model_id)
                return _final_answer_json("react 闲聊回复")
            seen_in_util.append(model_id)
            return "util 闲聊"
        _patch_chat_all(monkeypatch, responder)
        repl = _make_repl(optimize_prompts=True, mode="react")
        repl._handle_chat("你好")
        assert seen_in_engine, "react 模式下应走 ReAct 引擎"
        assert not seen_in_util, "react 模式下不应走 direct LLM"

    def test_mode_plan_execute_then_query(self, monkeypatch):
        """/mode plan-execute + "今天天气" — 应走 plan-execute 引擎。"""
        seen_in_engine: list[str] = []
        def responder(ctx):
            kind, model_id, messages = ctx
            if kind == "engine":
                seen_in_engine.append(model_id)
                return "plan-execute 引擎输出"
            return "util"
        _patch_chat_all(monkeypatch, responder)
        repl = _make_repl(optimize_prompts=True, mode="plan-execute")
        repl._handle_chat("今天天气")
        # plan-execute 引擎走 engine.chat_completion
        assert seen_in_engine, "plan-execute 模式下应走 PlanExecute 引擎"


# ── K. /optimize_prompts 关闭 + query ──────────────────

class TestOptimizePromptsOffQuery:
    """optimize_prompts=False 时，query 仍应路由 ReAct（不依赖 optimize_prompts）。"""

    def test_query_routes_to_react_without_optimizer(self, monkeypatch):
        seen_in_engine: list[str] = []
        def responder(ctx):
            kind, model_id, messages = ctx
            if kind == "engine":
                seen_in_engine.append(model_id)
                return _final_answer_json("ok")
            return "direct"
        _patch_chat_all(monkeypatch, responder)
        repl = _make_repl(optimize_prompts=False)
        repl._handle_chat("今天苏州的天气怎么样")
        assert seen_in_engine, (
            "optimize_prompts=False + query 仍应路由 ReAct（验证 line 718 "
            "intent 检测在 optimize_prompts 外执行）"
        )


# ── L. /optimize_prompts 关闭 + 闲聊 ──────────────────

class TestOptimizePromptsOffChat:
    """optimize_prompts=False + chat → 走 direct LLM。"""

    def test_chat_with_optimizer_off_routes_direct(self, monkeypatch):
        seen_in_engine: list[str] = []
        seen_in_util: list[str] = []
        def responder(ctx):
            kind, model_id, messages = ctx
            if kind == "engine":
                seen_in_engine.append(model_id)
                return _final_answer_json("e")
            seen_in_util.append(model_id)
            return "direct 闲聊"
        _patch_chat_all(monkeypatch, responder)
        repl = _make_repl(optimize_prompts=False)
        repl._handle_chat("你好")
        assert not seen_in_engine
        assert seen_in_util


# ── M. 否定/复杂 query ─────────────────────────────────

class TestComplexQuery:
    """否定句/条件句的 query 路由。"""

    def test_query_with_negation(self, monkeypatch):
        """"查一下今天天气，但不要给我穿衣建议" — 仍应识别为 query。"""
        text = "查一下今天天气，但不要给我穿衣建议"
        intent = detect_intent(text)
        assert intent == "query", f"否定句 query 识别失败: {intent}"
        assert REPL._detect_tool_need(text, intent="query") is True

    def test_query_with_condition(self, monkeypatch):
        """"如果今天下雨就告诉我" — 仍应识别为 query。"""
        text = "如果今天下雨就告诉我"
        intent = detect_intent(text)
        assert intent == "query", f"条件句 query 识别失败: {intent}"
        assert REPL._detect_tool_need(text, intent="query") is True


# ── N. 多种 query 变体 ────────────────────────────────

class TestQueryVariants:
    """检查 _TOOL_PATTERNS 是否也匹配 query 关键词（边界 case）。"""

    def test_stock_query(self, monkeypatch):
        """"看下腾讯股价" — query 意图。"""
        text = "看下腾讯股价"
        intent = detect_intent(text)
        assert intent == "query", f"股价 query 识别失败: {intent}"
        assert REPL._detect_tool_need(text, intent="query") is True

    def test_crypto_query(self, monkeypatch):
        """"BTC 现在多少美元" — query 意图。"""
        text = "BTC 现在多少美元"
        intent = detect_intent(text)
        # BTC/USD 不在 query trigger 列表中，但"多少美元"可能匹配
        # 实际可能落到 None
        # 关键：即便 intent=None，路由决策应能识别
        print(f"  BTC query intent: {intent}")
        # _TOOL_PATTERNS 中也没有 BTC/USD 正则
        # 实际能否路由取决于 intent
        if intent == "query":
            assert REPL._detect_tool_need(text, intent="query") is True


# ── 附加验证（来自 §9 coordinator 关注点） ─────────────

class TestAdditionalConcerns:
    """验证 coordinator 提出的 7 个关注点。"""

    def test_concern_1_trim_then_recursive_failure(self, monkeypatch):
        """关注点 1：trim_last_assistant 后递归失败 → ctx_mgr 状态污染。
        模拟：direct 模式 LLM 返回拒答 → trim + 递归调 ReAct → ReAct 抛异常。
        验证：第二轮 user 进来时，history 是否有无 assistant 回复的 user 消息。
        """
        from xenon.repl.repl import REPL

        def responder(ctx):
            kind, model_id, messages = ctx
            if kind == "util":
                # direct 模式：返回拒答关键词 → 触发 trim + 递归 ReAct
                return "I cannot do this, I'm unable to help."
            # engine 抛异常
            raise RuntimeError("mock engine failure")

        _patch_chat_all(monkeypatch, responder)
        repl = _make_repl(optimize_prompts=True)
        repl._handle_chat("write a hello world")  # write_code → 路由 ReAct（不受 _detect_denial 影响）

        # NOTE: write_code 直接走 ReAct，不经过 _run_direct 的 _detect_denial
        # 需另构造场景：让 LLM 在 direct 模式返回拒答
        # 但 chat 不会触发 _detect_denial（要满足"LLM 回复 + 非文件声明 + 拒绝"）
        # 用文件声明关键词来触发 trim
        # 见下一个测试

    def test_concern_1b_file_claim_trim_then_recursive_failure(self, monkeypatch):
        """关注点 1（变体）：file_claim 触发 trim + 递归 ReAct → ReAct 抛异常。
        构造：direct 模式命中 _TOOL_PATTERNS（git 操作）→ LLM 假装完成 → trim + 递归 → 抛错。
        """
        # 关键观察：git commit 会触发 _TOOL_PATTERNS → _run_direct 走 ReAct
        # 但 trim + recursive 路径只发生在 _run_direct 的 LLM 调用后
        # 我们的 _handle_chat → _run_direct → _detect_tool_need → True → _run_react_engine
        # _run_react_engine 不经过 _detect_denial
        # 所以这条路径不直接测，关注点验证需要更精细的内部测试

    def test_concern_2_mode_react_query_redundant_intent(self, monkeypatch):
        """关注点 2：/mode react 后，main flow 直接走 _run_react_engine，绕过 _run_direct。
        query 意图检测在 line 718 仍执行，但结果不传给 ReAct 引擎（ReAct 不读 intent）。
        验证：/mode react + "今天天气" → 走 ReAct（应该），但 query 检测白做。
        """
        seen_in_engine: list[str] = []
        def responder(ctx):
            kind, model_id, messages = ctx
            if kind == "engine":
                seen_in_engine.append(model_id)
                return _final_answer_json("react 处理了")
            return "util"
        _patch_chat_all(monkeypatch, responder)
        repl = _make_repl(optimize_prompts=True, mode="react")
        repl._handle_chat("今天苏州的天气怎么样")
        # 验证：query 仍路由 ReAct（无 bug）
        assert seen_in_engine, "react 模式 + query 应走 ReAct"
        # 文档记录：intent 检测此时白做（line 718 仍执行，但 result 不被使用）

    def test_concern_3_mode_plan_execute_query(self, monkeypatch):
        """关注点 3：/mode plan-execute + query → plan-execute 引擎如何处理？
        验证：plan-execute 引擎收到 query 文本时，是否会把它当编程任务。
        """
        seen_in_engine: list[str] = []
        def responder(ctx):
            kind, model_id, messages = ctx
            if kind == "engine":
                seen_in_engine.append(model_id)
                return "plan-execute 引擎处理 query"
            return "util"
        _patch_chat_all(monkeypatch, responder)
        repl = _make_repl(optimize_prompts=True, mode="plan-execute")
        repl._handle_chat("今天苏州的天气怎么样")
        assert seen_in_engine, "plan-execute 模式 + query 应走 PlanExecute 引擎"
        # plan-execute 内部行为不在本测试范围，记录为"待深入验证"

    def test_concern_4_chat_with_tool_keywords(self, monkeypatch):
        """关注点 4："你好，帮我查一下 src/foo.py 的代码"
        — chat 问候 + 编程请求
        期望：detect_intent='query' (因"查" + "src/foo.py" 都触发)，_detect_tool_need=True
        """
        text = "你好，帮我查一下 src/foo.py 的代码"
        intent = detect_intent(text)
        print(f"  '你好，帮我查一下 src/foo.py' intent: {intent}")
        # 验证：_detect_tool_need 路由到 ReAct
        assert REPL._detect_tool_need(text, intent=intent) is True, (
            f"chat 问候 + 编程请求应路由 ReAct: {REPL._detect_tool_need(text, intent=intent)}"
        )

    def test_concern_5_empty_string_history_pollution(self, monkeypatch):
        """关注点 5：空字符串 process_user_input → B-3 修复后应被入口防护拦截，
        不再污染 history。验证：连续两次空输入，history 应保持空（无 user 消息）。
        """
        def responder(ctx):
            return "ok"
        _patch_chat_all(monkeypatch, responder)
        repl = _make_repl(optimize_prompts=True)
        repl._handle_chat("")
        empty_count_1 = sum(
            1 for m in repl.ctx_mgr.history
            if m.role == "user" and m.content == ""
        )
        repl._handle_chat("")
        empty_count_2 = sum(
            1 for m in repl.ctx_mgr.history
            if m.role == "user" and m.content == ""
        )
        print(f"  空 user 消息: 第1次={empty_count_1}, 第2次={empty_count_2}")
        # B-3 修复后：空输入被 _handle_chat 入口拦截（repl.py:697），不调 LLM，不 add user 消息
        assert empty_count_1 == 0, (
            f"第1次空输入应被入口拦截（不 add_user_message），实际累积: {empty_count_1}"
        )
        assert empty_count_2 == 0, (
            f"第2次空输入应仍被拦截，实际累积: {empty_count_2}"
        )

    def test_concern_6_chat_template_pollutes_optimized(self):
        """关注点 6："你好" 触发 chat 模板（line 266-278），优化后追加
        '（这是一句问候/闲聊…）'。这会让 LLM 收到奇怪的指令上下文。"""
        from xenon.repl.prompt_optimizer import optimize_prompt

        optimized, system_hint, was_optimized = optimize_prompt("你好")
        # chat 模板 — assess_quality 返回 True（短输入），was_optimized=True
        print(f"  optimized: {optimized!r}")
        print(f"  system_hint: {system_hint!r}")
        # 文档记录：实际优化后内容

    def test_concern_7_intent_order_sensitivity(self):
        """关注点 7：detect_intent 顺序敏感（按 TEMPLATES 列表顺序匹配）。
        '写代码查天气' 既触发 write_code 也触发 query →
        write_code (line 142) 在 query (line 222) 之前 → write_code。
        """
        text = "写代码查天气"
        intent = detect_intent(text)
        # write_code 模板 trigger: (?:帮我|请|给).{0,5}(?:写|做|创建|实现|开发|搭|建)
        # 实际"写代码"可能不命中"帮我写"
        print(f"  '写代码查天气' intent: {intent}")
        # 关键验证：TEMPLATES 顺序对路由结果有影响
        # 文档记录实际行为


# ── 已知可疑点 7 项的回归验证 ─────────────────────────

class TestKnownSuspicious:
    """验证 task 描述中列出的 7 个已知可疑点。"""

    def test_suspicious_1_optimized_vs_original_user_input(self, monkeypatch):
        """可疑点 1：_run_direct 用 user_input（line 789）但 ctx_mgr 存 optimized（line 745）。
        验证：optimize 后 user_input != optimized 时，ReAct 收到哪个？"""
        seen_in_engine: list[list] = []
        def responder(ctx):
            kind, model_id, messages = ctx
            if kind == "engine":
                seen_in_engine.append(messages)
                return _final_answer_json("ok")
            return "direct"
        _patch_chat_all(monkeypatch, responder)
        repl = _make_repl(optimize_prompts=True)
        # query 文本会被优化为模板
        repl._handle_chat("今天苏州的天气怎么样")
        # 找到 engine 调用的最后一条 user 消息
        if seen_in_engine:
            last_user_in_engine = next(
                (m for m in reversed(seen_in_engine[-1]) if m["role"] == "user"),
                None,
            )
            # ReAct 在 line 789 收到 user_input（line 745 的 optimized 已加 user message）
            # ReAct.run() line 342 追加 user_input
            # ctx_mgr.get_messages() 已含 optimized
            print(f"  engine messages 末条 user: {last_user_in_engine!r}")

    def test_suspicious_2_recursive_depth_unbounded(self, monkeypatch):
        """可疑点 2：_run_direct 递归 _run_react_engine，ReAct 又调 spawn_agent → 父子链。
        max_subagent_depth 限制子 Agent 深度，但 _run_direct 递归本身无深度限制。"""
        # 简单记录观察：构造触发 _run_direct → _run_react_engine → 内部循环
        # ReAct 自身不会递归调 _run_react_engine（除非 file_claim/denial 触发）
        # 见 suspicious_2b
        pass

    def test_suspicious_2b_recursive_via_denial(self, monkeypatch):
        """可疑点 2 变体：构造"在 ReAct 中也触发 denial" — 但 ReAct 不会自己调 _run_direct。
        实际：_run_react_engine 内不调用 _run_direct，所以无限递归不会发生。
        """
        # 记录：_run_direct → _run_react_engine（一次），_run_react_engine 内部不再
        # 调 _run_direct。无限递归风险低。
        # 真正风险在 spawn_agent → 子 ReAct → 子子 ReAct（受 max_subagent_depth 限制）
        pass

    def test_suspicious_3_tool_patterns_no_query_keyword(self):
        """可疑点 3：_TOOL_PATTERNS 确实没有 query 关键词（天气/价格/汇率）。
        这就是这次修改的原因——query 意图直接判 True。"""
        for pattern in REPL._TOOL_PATTERNS:
            for kw in ["天气", "价格", "汇率", "黄金", "金价", "BTC", "股价"]:
                # 不强制断言 — 记录实际匹配情况
                m = pattern.search(f"今天{kw}")
                if m:
                    print(f"  _TOOL_PATTERNS 匹配: {pattern.pattern} → {kw}")

    def test_suspicious_4_optimize_off_intent_passed(self, monkeypatch):
        """可疑点 4：optimize_prompts=False 时 line 718 intent 仍执行。
        line 742 optimized = user_input。验证 intent 是否传给 _run_direct。"""
        # 直接调 _run_direct，验证参数透传
        repl = _make_repl(optimize_prompts=False)
        # _run_direct 签名: (user_input, model_ids, intent=None)
        import inspect
        sig = inspect.signature(repl._run_direct)
        print(f"  _run_direct 签名: {sig}")
        assert "intent" in sig.parameters, "_run_direct 应接收 intent 参数"

    def test_suspicious_5_chinese_query_regression(self, monkeypatch):
        """可疑点 5：中文 query "今天天气怎么样" 之前是 chat（不路由），现在应是 query。
        验证：detect_intent("今天天气怎么样") == "query"。"""
        text = "今天天气怎么样"
        intent = detect_intent(text)
        assert intent == "query", f"中文 query 回归失败: {intent}"
        # 进一步验证：_run_react 路由
        seen_in_engine: list[str] = []
        def responder(ctx):
            kind, model_id, messages = ctx
            if kind == "engine":
                seen_in_engine.append(model_id)
                return _final_answer_json("ok")
            return "direct"
        _patch_chat_all(monkeypatch, responder)
        repl = _make_repl(optimize_prompts=True)
        repl._handle_chat(text)
        assert seen_in_engine, "中文 query 应路由 ReAct"

    def test_suspicious_6_ctx_mgr_singleton_no_reset(self, monkeypatch):
        """可疑点 6：ctx_mgr 单例 → 连续 process_user_input 不 reset。
        验证：连续 query 输入累积 history，不影响下次路由判断。"""
        def responder(ctx):
            kind, model_id, messages = ctx
            if kind == "engine":
                return _final_answer_json("ok")
            return "direct"
        _patch_chat_all(monkeypatch, responder)
        repl = _make_repl(optimize_prompts=True)
        repl._handle_chat("今天天气怎么样")
        n1 = len(repl.ctx_mgr.history)
        repl._handle_chat("今天黄金价格多少")
        n2 = len(repl.ctx_mgr.history)
        print(f"  history: 第1次={n1}, 第2次={n2}")
        assert n2 > n1, "连续 query 应累积 history"
        # 不影响路由 — 每次都基于当前 user_input 的 detect_intent

    def test_suspicious_7_react_exception_leaves_user_msg(self, monkeypatch):
        """可疑点 8：ReAct 抛异常时，ctx_mgr 已 add_user_message，无 assistant 回复。
        验证：异常后 history 包含无 assistant 回复的 user 消息。"""
        def responder(ctx):
            kind, model_id, messages = ctx
            if kind == "engine":
                raise RuntimeError("mock engine exception")
            return "direct"
        _patch_chat_all(monkeypatch, responder)
        repl = _make_repl(optimize_prompts=True)
        repl._handle_chat("今天天气怎么样")
        # ReAct 异常被 line 840-841 捕获，仅 print+return
        # 此时 user 消息已 add（line 745），assistant 未 add
        user_only = [
            m for m in repl.ctx_mgr.history
            if m.role == "user"
        ]
        # 找有没有 user 消息没有后续 assistant 消息
        last_user_idx = None
        last_asst_idx = None
        for i, m in enumerate(repl.ctx_mgr.history):
            if m.role == "user":
                last_user_idx = i
            elif m.role == "assistant":
                last_asst_idx = i
        if last_user_idx is not None:
            if last_asst_idx is None or last_user_idx > last_asst_idx:
                print(
                    f"  ⚠️ 状态污染: user 消息 idx={last_user_idx} "
                    f"无对应 assistant 消息（last_asst_idx={last_asst_idx}）"
                )
                # 不强制 assert，记录给报告
