"""R1 验收：BaseEngine._call_llm 区分终端错误与瞬时错误（Q9 策略）。

- 401/403（认证）、400（请求被拒）= 终端，立即上抛 + on_error，不切模型；
- 429/5xx/网络/截断 = 瞬时，切下一个模型；
- 全部失败 → on_error + RuntimeError。
"""
import httpx
import pytest

import xenon.engine.base as base_mod
from xenon.engine.callbacks import EngineCallback
from xenon.engine.react_engine import ReActEngine
from xenon.utils.llm_client import ResponseTruncatedError


class _RecordingCallback(EngineCallback):
    def __init__(self):
        self.errors: list[str] = []

    def on_error(self, error: str) -> None:
        self.errors.append(error)


def _status_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://example.com")
    resp = httpx.Response(status_code=status, request=req)
    return httpx.HTTPStatusError(f"HTTP {status}", request=req, response=resp)


def _engine(models):
    cb = _RecordingCallback()
    eng = ReActEngine(models, callback=cb)
    return eng, cb


class TestTerminalErrorsRaiseImmediately:
    def test_401_raises_without_trying_next(self, monkeypatch):
        eng, cb = _engine(["openai/a", "anthropic/b"])

        def fake(model_id, messages, **kw):
            if model_id == "openai/a":
                raise _status_error(401)
            raise AssertionError("401 终端错误不应切到下一个模型")

        monkeypatch.setattr(base_mod, "chat_completion", fake)
        with pytest.raises(RuntimeError, match="认证失败"):
            eng._call_llm([{"role": "user", "content": "hi"}])
        assert cb.errors  # on_error 被调用

    def test_403_raises_immediately(self, monkeypatch):
        eng, cb = _engine(["openai/a", "anthropic/b"])

        def fake(model_id, messages, **kw):
            if model_id == "openai/a":
                raise _status_error(403)
            raise AssertionError("403 终端错误不应切模型")

        monkeypatch.setattr(base_mod, "chat_completion", fake)
        with pytest.raises(RuntimeError, match="认证失败"):
            eng._call_llm([{"role": "user", "content": "hi"}])

    def test_400_raises_immediately(self, monkeypatch):
        eng, cb = _engine(["openai/a", "anthropic/b"])

        def fake(model_id, messages, **kw):
            if model_id == "openai/a":
                raise _status_error(400)
            raise AssertionError("400 终端错误不应切模型")

        monkeypatch.setattr(base_mod, "chat_completion", fake)
        with pytest.raises(RuntimeError, match="请求被拒"):
            eng._call_llm([{"role": "user", "content": "hi"}])
        assert cb.errors


class TestTransientErrorsSwitchModel:
    def test_429_switches_to_next(self, monkeypatch):
        eng, _ = _engine(["openai/a", "anthropic/b"])

        def fake(model_id, messages, **kw):
            if model_id == "openai/a":
                raise _status_error(429)
            return '{"final_answer":"ok"}'

        monkeypatch.setattr(base_mod, "chat_completion", fake)
        out = eng._call_llm([{"role": "user", "content": "hi"}])
        assert out == '{"final_answer":"ok"}'

    def test_500_switches_to_next(self, monkeypatch):
        eng, _ = _engine(["openai/a", "anthropic/b"])

        def fake(model_id, messages, **kw):
            if model_id == "openai/a":
                raise _status_error(500)
            return '{"final_answer":"ok"}'

        monkeypatch.setattr(base_mod, "chat_completion", fake)
        assert eng._call_llm([{"role": "user", "content": "hi"}]) == '{"final_answer":"ok"}'

    def test_network_error_switches_to_next(self, monkeypatch):
        eng, _ = _engine(["openai/a", "anthropic/b"])

        def fake(model_id, messages, **kw):
            if model_id == "openai/a":
                raise httpx.ConnectError("down")
            return '{"final_answer":"ok"}'

        monkeypatch.setattr(base_mod, "chat_completion", fake)
        assert eng._call_llm([{"role": "user", "content": "hi"}]) == '{"final_answer":"ok"}'

    def test_truncation_switches_to_next(self, monkeypatch):
        eng, _ = _engine(["openai/a", "anthropic/b"])

        def fake(model_id, messages, **kw):
            if model_id == "openai/a":
                raise ResponseTruncatedError("truncated")
            return '{"final_answer":"ok"}'

        monkeypatch.setattr(base_mod, "chat_completion", fake)
        assert eng._call_llm([{"role": "user", "content": "hi"}]) == '{"final_answer":"ok"}'


class TestAllModelsFail:
    def test_all_fail_raises_and_notifies(self, monkeypatch):
        eng, cb = _engine(["openai/a", "anthropic/b"])

        def fake(model_id, messages, **kw):
            raise _status_error(500)

        monkeypatch.setattr(base_mod, "chat_completion", fake)
        with pytest.raises(RuntimeError, match="所有模型均调用失败"):
            eng._call_llm([{"role": "user", "content": "hi"}])
        assert cb.errors  # on_error 被调用（全部失败）
        assert any("所有模型均调用失败" in e for e in cb.errors)
