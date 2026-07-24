"""Chaos Test 5: 超大输出。

目标：验证 LLM 返回 10KB+ 单行文本时 ReAct 引擎不卡死。
关键路径：
- ``response_adapter.parse_react`` 应能处理大字符串（不 OOM/超时）；
- 引擎的 observation 截断（_near_context_window 保护）应生效；
- LLM 内部 retry/重试不被超大 payload 阻塞。
"""
from __future__ import annotations

import time


from xenon.utils.response_adapter import parse_react


class TestParseReactOnHugeOutput:
    """parse_react 处理 10KB+ 文本的性能与正确性。"""

    def test_10kb_text_in_thought(self):
        """10KB thought 文本 + 合法 action → 正确解析。"""
        huge_thought = "x" * 10_000
        text = (
            '{"thought": "' + huge_thought + '", '
            '"action": "read_file", "action_input": {"file_path": "/x"}}'
        )
        start = time.monotonic()
        result = parse_react(text)
        elapsed = time.monotonic() - start
        # 解析应 <2s
        assert elapsed < 2.0, f"parse_react 超时: {elapsed:.2f}s"
        # action 应被识别（关键：超大 thought 不能淹没 action）
        assert result.get("action") == "read_file"

    def test_10kb_text_in_final_answer(self):
        """10KB final_answer 文本 → 正确解析。"""
        huge_answer = "y" * 10_000
        text = '{"thought": "done", "final_answer": "' + huge_answer + '"}'
        start = time.monotonic()
        result = parse_react(text)
        elapsed = time.monotonic() - start
        assert elapsed < 2.0
        # final_answer 至少应被识别为有值（即使被截断修复）
        assert "final_answer" in result

    def test_100kb_does_not_crash(self):
        """100KB 极端输入 → 不抛异常，能在合理时间内完成。"""
        huge = "z" * 100_000
        text = '{"thought": "' + huge + '", "action": "command", "action_input": {}}'
        start = time.monotonic()
        # 关键是**不抛异常**
        result = parse_react(text)
        elapsed = time.monotonic() - start
        assert elapsed < 5.0, f"100KB parse 超时: {elapsed:.2f}s"
        assert isinstance(result, dict)


class TestEngineWithHugeObservation:
    """ReAct 引擎遇到超大 Observation 不挂死。"""

    def test_huge_observation_truncated(self, monkeypatch):
        """tool 返回 50KB 输出 → 引擎 observation 截断（_near_context_window）防止 OOM。"""
        import xenon.engine.base as engine_base
        import xenon.utils.llm_client as llm_client
        from xenon.engine.react_engine import ReActEngine
        from xenon.engine.callbacks import SilentCallback
        from xenon.nodes import tool_executor as te_mod

        # tool 返回 50KB 内容
        huge_output = "A" * 50_000

        class _HugeNode:
            @staticmethod
            def normalize_params(p):
                return p
            def __init__(self, name, action_type=None, **params):
                pass
            def execute(self, context):
                return {"success": True, "content": huge_output}

        responses = [
            '{"thought": "read file", "action": "read_file", "action_input": {"file_path": "/x"}}',
            '{"thought": "got it", "final_answer": "done"}',
        ]

        def fake_engine(model_id, messages, **kw):
            return responses.pop(0) if responses else '{"final_answer": "fallback"}'

        def fake_util(model_id, messages, **kw):
            return fake_engine(model_id, messages, **kw)

        def fake_util_stream(model_id, messages, **kw):
            yield fake_engine(model_id, messages, **kw)

        monkeypatch.setattr(engine_base, "chat_completion", fake_engine)
        monkeypatch.setattr(llm_client, "chat_completion", fake_util)
        monkeypatch.setattr(llm_client, "chat_completion_stream", fake_util_stream)
        monkeypatch.setattr(te_mod, "ToolNode", _HugeNode)

        eng = ReActEngine(
            ["openai/gpt-4o"],
            max_iterations=5,
            callback=SilentCallback(),
            model_configs={
                "openai/gpt-4o": type("MC", (), {
                    "api_key": "sk", "base_url": "https://api.test/v1",
                    "max_tokens": 4096, "context_window": 128000,
                })(),
            },
        )
        start = time.monotonic()
        answer = eng.run("read /x")
        elapsed = time.monotonic() - start
        # 引擎应在合理时间完成（< 5s），不被 50KB Observation 阻塞
        assert elapsed < 5.0, f"引擎处理 50KB Observation 超时: {elapsed:.2f}s"
        assert isinstance(answer, str)
