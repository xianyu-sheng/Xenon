"""Regression tests for the agent slash-command group boundary."""

from __future__ import annotations

from xenon.repl.command_groups.agent import (
    _cmd_ask,
    _cmd_code,
    _cmd_run,
    _cmd_sub_agent,
)
from xenon.repl.commands import (
    _cmd_ask as legacy_ask,
    _cmd_code as legacy_code,
    _cmd_run as legacy_run,
    _cmd_sub_agent as legacy_sub_agent,
)


def test_agent_group_preserves_legacy_command_exports():
    assert legacy_ask is _cmd_ask
    assert legacy_code is _cmd_code
    assert legacy_run is _cmd_run
    assert legacy_sub_agent is _cmd_sub_agent


def test_ask_needs_model():
    from xenon.repl.model_registry import ModelRegistry
    from xenon.repl.context_manager import ContextManager

    reg = ModelRegistry()
    ctx = ContextManager()
    result = _cmd_ask(args="", registry=reg, ctx_mgr=ctx)
    assert "用法" in result


def test_ask_uses_model_output_budget_and_endpoint(monkeypatch):
    from xenon.repl.context_manager import ContextManager
    from xenon.repl.model_registry import ModelRegistry

    reg = ModelRegistry()
    reg.add_model(
        "openai/deepseek-v4-pro",
        "pro",
        api_key="sk-private",
        base_url="https://relay.example/v1",
        max_tokens=256000,
        reasoning_effort="max",
    )
    captured = {}

    def fake_chat(model_id, messages, **kwargs):
        captured.update(kwargs)
        return "完整回答。"

    monkeypatch.setattr("xenon.utils.llm_client.chat_completion", fake_chat)

    assert (
        _cmd_ask(
            args="pro 解释架构",
            registry=reg,
            ctx_mgr=ContextManager(),
        )
        == "完整回答。"
    )
    assert captured["max_tokens"] == 256000
    assert captured["credentials"] == {"openai": "sk-private"}
    assert captured["base_url"] == "https://relay.example/v1"
    assert captured["reasoning_effort"] == "max"


def test_code_needs_task():
    from xenon.repl.model_registry import ModelRegistry
    from xenon.repl.context_manager import ContextManager

    reg = ModelRegistry()
    ctx = ContextManager()
    result = _cmd_code(args="", registry=reg, ctx_mgr=ctx, session_state={})
    assert "用法" in result


def test_run_needs_workflow():
    from xenon.repl.model_registry import ModelRegistry

    reg = ModelRegistry()
    result = _cmd_run(args="", session_state={}, registry=reg)
    assert isinstance(result, str)


def test_sub_agent_no_args():
    result = _cmd_sub_agent(args="", session_state={})
    assert "委派子 Agent" in result


def test_sub_agent_no_repl():
    result = _cmd_sub_agent(args="do something", session_state={}, repl=None)
    assert "无法获取 REPL 实例" in result
