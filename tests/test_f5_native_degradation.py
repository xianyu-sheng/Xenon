"""F5 三层 LLM 降级 _call_llm_native 单测。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from xenon.engine.base import BaseEngine
from xenon.engine.context import AgentContext
from xenon.engine.react_engine import ReActEngine
from xenon.utils.llm_client import LLMResponse


# ── 辅助 ────────────────────────────────────────────────────
def _http_error(status: int) -> httpx.HTTPStatusError:
    """构造指定 status 的 HTTPStatusError。"""
    req = httpx.Request("POST", "http://x.test")
    resp = httpx.Response(status, request=req)
    return httpx.HTTPStatusError(f"{status}", request=req, response=resp)


class _ConcreteEngine(BaseEngine):
    """可实例化的 BaseEngine（run 占位）供直接测 _call_llm_native。"""

    def run(self, user_input, context=None, ctx_mgr=None):
        return ""


def _engine(models=("m1",), max_tokens=None):
    eng = _ConcreteEngine(list(models))
    return eng


def _patch_fc(monkeypatch, eng, fn):
    """patch base 模块里的 chat_completion_with_tools。"""
    monkeypatch.setattr("xenon.engine.base.chat_completion_with_tools", fn)


# ════════════════════════════════════════════════════════════
# _tool_calls_to_react_json
# ════════════════════════════════════════════════════════════
class TestToolCallsToReactJson:
    def test_synthesizes_react_json(self):
        s = BaseEngine._tool_calls_to_react_json(
            [{"id": "1", "name": "write_file", "arguments": {"file_path": "a.py", "content": "x"}}])
        parsed = json.loads(s)
        assert parsed["action"] == "write_file"
        assert parsed["action_input"] == {"file_path": "a.py", "content": "x"}
        assert parsed["thought"] == ""

    def test_empty_tool_calls(self):
        s = BaseEngine._tool_calls_to_react_json([])
        parsed = json.loads(s)
        assert parsed["action"] == ""
        assert parsed["action_input"] == {}

    def test_arguments_dict_when_none(self):
        s = BaseEngine._tool_calls_to_react_json([{"name": "t", "arguments": None}])
        parsed = json.loads(s)
        assert parsed["action_input"] == {}


# ════════════════════════════════════════════════════════════
# _call_llm_native 三层降级
# ════════════════════════════════════════════════════════════
class TestCallLlmNative:
    def test_no_tools_no_format_falls_back_directly(self, monkeypatch):
        """无 tools_schema 且无 response_format → 直接 _call_llm。"""
        eng = _engine()
        eng._call_llm = lambda msgs, max_tokens=None: "plain"
        # 若误调 chat_completion_with_tools 会触发 fake 抛错
        _patch_fc(monkeypatch, eng, lambda *a, **k: pytest.fail("不应调用 FC"))
        assert eng._call_llm_native([], None, None) == "plain"

    def test_tier1_tool_calls_synthesized(self, monkeypatch):
        eng = _engine()
        _patch_fc(monkeypatch, eng, lambda mid, msgs, **k: LLMResponse(
            content="", tool_calls=[{"id": "1", "name": "write_file",
                                     "arguments": {"file_path": "a.py"}}]))
        out = eng._call_llm_native([], [{"type": "function"}], {"type": "json_object"})
        parsed = json.loads(out)
        assert parsed["action"] == "write_file"

    def test_tier1_content_returned(self, monkeypatch):
        eng = _engine()
        _patch_fc(monkeypatch, eng, lambda mid, msgs, **k: LLMResponse(
            content='{"thought":"t","final_answer":"done"}'))
        out = eng._call_llm_native([], [{"type": "function"}], {"type": "json_object"})
        assert "final_answer" in out

    def test_tier1_fails_tier2_tools_only_succeeds(self, monkeypatch):
        """tier①(tools+format) 抛错 → tier②(tools only) 返回 tool_calls。"""
        eng = _engine()
        calls = []

        def fake(mid, msgs, *, tools=None, response_format=None, **kw):
            calls.append((bool(tools), bool(response_format)))
            if tools and response_format:
                raise RuntimeError("tier1 不支持 response_format")
            if tools:  # tier 2
                return LLMResponse(content="", tool_calls=[
                    {"id": "1", "name": "command", "arguments": {"action": "ls"}}])
            return LLMResponse(content="")

        _patch_fc(monkeypatch, eng, fake)
        out = eng._call_llm_native([], [{"type": "function"}], {"type": "json_object"})
        parsed = json.loads(out)
        assert parsed["action"] == "command"
        # tier1 与 tier2 都被调用
        assert (True, True) in calls
        assert (True, False) in calls

    def test_tier1_tier2_fail_tier3_format_only(self, monkeypatch):
        """tier①② 都失败 → tier③(format only) 返回 content。"""
        eng = _engine()
        calls = []

        def fake(mid, msgs, *, tools=None, response_format=None, **kw):
            calls.append((bool(tools), bool(response_format)))
            if tools:  # tier1, tier2 都带 tools → 失败
                raise RuntimeError("不支持 tools")
            # tier3: format only
            return LLMResponse(content='{"thought":"t","final_answer":"done"}')

        _patch_fc(monkeypatch, eng, fake)
        out = eng._call_llm_native([], [{"type": "function"}], {"type": "json_object"})
        assert "final_answer" in out
        # tier3 (False, True) 被调用
        assert (False, True) in calls

    def test_all_tiers_fail_fallback_to_call_llm(self, monkeypatch):
        """三层全败 → 回退 _call_llm。"""
        eng = _engine()
        _patch_fc(monkeypatch, eng, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("全挂")))
        eng._call_llm = lambda msgs, max_tokens=None: "fallback text"
        out = eng._call_llm_native([], [{"type": "function"}], {"type": "json_object"})
        assert out == "fallback text"

    def test_empty_content_degrades_tier(self, monkeypatch):
        """tier 返回空 content → 降级下一层。"""
        eng = _engine()
        state = {"n": 0}

        def fake(mid, msgs, *, tools=None, response_format=None, **kw):
            state["n"] += 1
            if state["n"] == 1:
                return LLMResponse(content="   ")  # 空 → 降级
            return LLMResponse(content='{"final_answer":"ok"}')

        _patch_fc(monkeypatch, eng, fake)
        out = eng._call_llm_native([], [{"type": "function"}], {"type": "json_object"})
        assert "ok" in out
        assert state["n"] == 2

    def test_401_terminal_raises(self, monkeypatch):
        """401 认证失败 = 终端错误，直接抛不降级。"""
        eng = _engine()

        def fake(mid, msgs, **kw):
            raise _http_error(401)

        _patch_fc(monkeypatch, eng, fake)
        with pytest.raises(RuntimeError, match="认证失败"):
            eng._call_llm_native([], [{"type": "function"}], {"type": "json_object"})

    def test_400_then_next_model(self, monkeypatch):
        """400（不支持）→ 试下一个模型；全 400 → 本层降级。"""
        eng = _engine(("m1", "m2"))
        tried = []

        def fake(mid, msgs, **kw):
            tried.append(mid)
            raise _http_error(400)

        _patch_fc(monkeypatch, eng, fake)
        eng._call_llm = lambda msgs, max_tokens=None: "fallback"
        out = eng._call_llm_native([], [{"type": "function"}], {"type": "json_object"})
        # 两个模型都被试过（tier1 内），最终全败回退
        assert "m1" in tried and "m2" in tried
        assert out == "fallback"

    def test_only_tools_no_format_skips_tier3(self, monkeypatch):
        """只传 tools 不传 format → tier③(format only)被过滤掉，不调用。"""
        eng = _engine()
        calls = []

        def fake(mid, msgs, *, tools=None, response_format=None, **kw):
            calls.append((bool(tools), bool(response_format)))
            raise RuntimeError("fail")

        _patch_fc(monkeypatch, eng, fake)
        eng._call_llm = lambda msgs, max_tokens=None: "fb"
        eng._call_llm_native([], [{"type": "function"}], None)
        # 只有 tier1(tools+format?) 不——format=None：tier1=(True,False)?
        # 实际：tools=T,format=None → tier1=(T,空被过滤)? tiers = [(t1,T,None),(t2,T,None)] 去重?
        # tiers 列表：tier1=(T,None)→保留, tier2=(T,None)→保留, tier3=(None,None)→过滤
        # tier1 与 tier2 都是 (tools, None) → 两次相同调用
        assert all(c == (True, False) for c in calls)


# ════════════════════════════════════════════════════════════
# ReAct native_fc 集成
# ════════════════════════════════════════════════════════════
class TestReActNativeFc:
    def test_build_tools_schema_structure(self):
        eng = ReActEngine(["m1"], native_fc=True)
        schema = eng._build_tools_schema()
        assert len(schema) == len(eng.tools)
        # 抽查 write_file
        wf = next(s for s in schema if s["function"]["name"] == "write_file")
        assert wf["type"] == "function"
        assert "file_path" in wf["function"]["parameters"]["properties"]
        assert "content" in wf["function"]["parameters"]["properties"]

    def test_react_response_format_is_json_object(self):
        assert ReActEngine._react_response_format() == {"type": "json_object"}

    def test_native_fc_tool_call_routes_to_execute(self, monkeypatch):
        """native_fc=True：原生 tool_call → 合成 ReAct JSON → _execute_tool 被调。"""
        eng = ReActEngine(["m1"], max_iterations=3, native_fc=True)
        executed = []

        def fake_fc(mid, msgs, *, tools=None, response_format=None, **kw):
            # 第一次返回 tool_call，第二次返回 final_answer
            if not executed:
                return LLMResponse(content="", tool_calls=[
                    {"id": "1", "name": "write_file",
                     "arguments": {"file_path": "a.py", "content": "print(1)"}}])
            return LLMResponse(content='{"thought":"t","final_answer":"已写入 a.py"}')

        _patch_fc(monkeypatch, eng, fake_fc)
        from xenon.utils.response_adapter import parse_react
        eng._parse_response = parse_react
        eng._input_requires_tools = lambda u: True

        def fake_execute(action, ai, ctx, tracker):
            executed.append((action, ai))
            tracker.record(action, ai, True, "obs")  # 记录以便 has_executions
            return "obs"
        eng._execute_tool = fake_execute

        result = eng.run("写 a.py", AgentContext())
        assert "已写入 a.py" in result
        assert len(executed) == 1
        assert executed[0][0] == "write_file"
        assert executed[0][1]["file_path"] == "a.py"

    def test_native_fc_off_uses_call_llm(self, monkeypatch):
        """native_fc=False（默认）：走 _call_llm，不调 FC。"""
        eng = ReActEngine(["m1"], max_iterations=2)
        called_llm = {"n": 0}

        def fake_llm(msgs, max_tokens=None):
            called_llm["n"] += 1
            return '{"thought":"t","final_answer":"done"}'

        eng._call_llm = fake_llm
        _patch_fc(monkeypatch, eng, lambda *a, **k: pytest.fail("不应调用 FC"))
        eng._parse_response = lambda r: {"thought": "t", "final_answer": "done"}
        eng._input_requires_tools = lambda u: False
        result = eng.run("你好", AgentContext())
        assert result == "done"
        assert called_llm["n"] == 1
