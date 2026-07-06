"""B12 验收：finish_reason=length / stop_reason=max_tokens 时自动续写，
续写次数耗尽抛 ResponseTruncatedError（而非仅 logger.warning 静默返回截断内容）。

通过 monkeypatch 单次调用助手 _call_*_once 注入脚本化的 (content, reason)
序列，验证续写循环与异常路径，不依赖真实网络。
"""
import pytest

import omniagent.utils.llm_client as lc
from omniagent.utils.llm_client import ModelEndpoint, ResponseTruncatedError


def _ep(provider: str = "openai") -> ModelEndpoint:
    return ModelEndpoint(provider=provider, model_name="m", base_url="http://x", api_key="k")


class TestOpenaiCompatAutoContinue:
    def test_no_truncation_returns_content(self, monkeypatch):
        calls = []

        def fake_once(ep, msgs, mt, t, to):
            calls.append(list(msgs))
            return ("hello", "stop")

        monkeypatch.setattr(lc, "_call_openai_compat_once", fake_once)
        out = lc._call_openai_compat(_ep(), [{"role": "user", "content": "hi"}], 100, 0.3, 10)
        assert out == "hello"
        assert len(calls) == 1  # 未触发续写

    def test_truncation_auto_continues(self, monkeypatch):
        seq = [("part1", "length"), ("part2", "stop")]

        def fake_once(ep, msgs, mt, t, to):
            return seq.pop(0)

        monkeypatch.setattr(lc, "_call_openai_compat_once", fake_once)
        out = lc._call_openai_compat(_ep(), [{"role": "user", "content": "hi"}], 100, 0.3, 10)
        assert out == "part1part2"

    def test_exhausted_continuations_raises(self, monkeypatch):
        def fake_once(ep, msgs, mt, t, to):
            return ("x", "length")

        monkeypatch.setattr(lc, "_call_openai_compat_once", fake_once)
        with pytest.raises(ResponseTruncatedError):
            lc._call_openai_compat(_ep(), [{"role": "user", "content": "hi"}], 100, 0.3, 10)

    def test_does_not_mutate_caller_messages(self, monkeypatch):
        seq = [("a", "length"), ("b", "stop")]

        def fake_once(ep, msgs, mt, t, to):
            return seq.pop(0)

        monkeypatch.setattr(lc, "_call_openai_compat_once", fake_once)
        original = [{"role": "user", "content": "hi"}]
        lc._call_openai_compat(_ep(), original, 100, 0.3, 10)
        assert original == [{"role": "user", "content": "hi"}]

    def test_continuation_appends_assistant_and_continue(self, monkeypatch):
        seen = []
        seq = [("p1", "length"), ("p2", "stop")]

        def fake_once(ep, msgs, mt, t, to):
            seen.append(msgs)
            return seq.pop(0)

        monkeypatch.setattr(lc, "_call_openai_compat_once", fake_once)
        lc._call_openai_compat(_ep(), [{"role": "user", "content": "hi"}], 100, 0.3, 10)
        # 第二次调用的 messages 末尾应为 assistant(p1) + user(继续)
        assert seen[1][-2] == {"role": "assistant", "content": "p1"}
        assert seen[1][-1] == {"role": "user", "content": "继续"}


class TestAnthropicAutoContinue:
    def test_end_turn_no_continue(self, monkeypatch):
        calls = []

        def fake_once(ep, msgs, mt, t, to):
            calls.append(1)
            return ("done", "end_turn")

        monkeypatch.setattr(lc, "_call_anthropic_once", fake_once)
        out = lc._call_anthropic(_ep("anthropic"), [{"role": "user", "content": "hi"}], 100, 0.3, 10)
        assert out == "done"
        assert len(calls) == 1

    def test_max_tokens_continues(self, monkeypatch):
        seq = [("a", "max_tokens"), ("b", "end_turn")]

        def fake_once(ep, msgs, mt, t, to):
            return seq.pop(0)

        monkeypatch.setattr(lc, "_call_anthropic_once", fake_once)
        out = lc._call_anthropic(_ep("anthropic"), [{"role": "user", "content": "hi"}], 100, 0.3, 10)
        assert out == "ab"

    def test_exhausted_raises(self, monkeypatch):
        def fake_once(ep, msgs, mt, t, to):
            return ("x", "max_tokens")

        monkeypatch.setattr(lc, "_call_anthropic_once", fake_once)
        with pytest.raises(ResponseTruncatedError):
            lc._call_anthropic(_ep("anthropic"), [{"role": "user", "content": "hi"}], 100, 0.3, 10)


def test_response_truncated_error_is_runtime_error():
    """engines 的宽泛 except Exception 需能捕获该异常。"""
    assert issubclass(ResponseTruncatedError, RuntimeError)
