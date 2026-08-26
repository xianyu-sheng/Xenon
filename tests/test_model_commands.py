"""Regression tests for the model slash-command group boundary."""

from __future__ import annotations

from xenon.repl.command_groups.model import (
    _cmd_import_models,
    _cmd_mode,
    _cmd_model,
    _cmd_models,
    _cmd_pool,
    _cmd_provider,
    _cmd_reload_models,
    _cmd_remove_model,
    _cmd_set_model,
    _cmd_set_profile,
    _cmd_set_role,
    _model_hint_local,
)
from xenon.repl.commands import (
    _cmd_import_models as legacy_import_models,
    _cmd_mode as legacy_mode,
    _cmd_model as legacy_model,
    _cmd_models as legacy_models,
    _cmd_pool as legacy_pool,
    _cmd_provider as legacy_provider,
    _cmd_reload_models as legacy_reload_models,
    _cmd_remove_model as legacy_remove_model,
    _cmd_set_model as legacy_set_model,
    _cmd_set_profile as legacy_set_profile,
    _cmd_set_role as legacy_set_role,
    _model_hint_local as legacy_model_hint_local,
)


def test_model_group_preserves_legacy_command_exports():
    """Verify that the re-exported names in commands.py point to the real
    implementations in command_groups.model."""
    assert legacy_import_models is _cmd_import_models
    assert legacy_mode is _cmd_mode
    assert legacy_model is _cmd_model
    assert legacy_models is _cmd_models
    assert legacy_pool is _cmd_pool
    assert legacy_provider is _cmd_provider
    assert legacy_reload_models is _cmd_reload_models
    assert legacy_remove_model is _cmd_remove_model
    assert legacy_set_model is _cmd_set_model
    assert legacy_set_profile is _cmd_set_profile
    assert legacy_set_role is _cmd_set_role
    assert legacy_model_hint_local is _model_hint_local


def test_model_hint_local_known_model():
    assert _model_hint_local("deepseek-v4-pro") == "旗舰编程与复杂 Agent · 1M 上下文"


def test_model_hint_local_unknown():
    assert _model_hint_local("some-random-model") == ""


def test_models_no_registry():
    from xenon.repl.model_registry import ModelRegistry

    reg = ModelRegistry()
    result = _cmd_models(registry=reg)
    assert "暂无已注册模型" in result or "已注册模型" in result


def test_mode_no_args():
    from xenon.repl.model_registry import ModelRegistry

    reg = ModelRegistry()
    result = _cmd_mode(args="", registry=reg, session_state={})
    assert "当前范式" in result


def test_mode_list_shows_builtin():
    from xenon.repl.model_registry import ModelRegistry

    reg = ModelRegistry()
    result = _cmd_mode(args="", registry=reg, session_state={})
    assert "可用范式" in result


def test_set_role_needs_args():
    from xenon.repl.model_registry import ModelRegistry

    reg = ModelRegistry()
    result = _cmd_set_role(args="", registry=reg)
    assert "用法" in result


def test_remove_model_needs_alias():
    from xenon.repl.model_registry import ModelRegistry

    reg = ModelRegistry()
    result = _cmd_remove_model(args="", registry=reg)
    assert "用法" in result


def test_remove_model_unknown_alias_reports_not_found():
    """回归锁定：移除不存在的模型必须明确报「不存在」。

    历史上曾出现假阳性成功消息（✅ 已移除 但什么都没删），
    该行为已被修复；此用例防止将来回归。
    """
    from xenon.repl.model_registry import ModelRegistry

    reg = ModelRegistry()
    reg.add_model("deepseek/deepseek-v4-flash", "deepseek-v4-flash")
    result = _cmd_remove_model(args="ghost", registry=reg)
    assert "ghost" in result
    assert "不存在" in result
    assert "✅" not in result
    # 移除存在模型后，列表确实减少
    ok = _cmd_remove_model(args="deepseek-v4-flash", registry=reg)
    assert "已移除" in ok
    assert "deepseek-v4-flash" not in reg.models


def test_remove_model_by_model_id():
    """按 model_id 移除（v0.5.2 特性：custom/xxx 形式）也应生效。"""
    from xenon.repl.model_registry import ModelRegistry

    reg = ModelRegistry()
    reg.add_model("custom/glm-5-2", "alias-x")
    result = _cmd_remove_model(args="custom/glm-5-2", registry=reg)
    assert "已移除" in result
    assert "alias-x" not in reg.models


def test_provider_no_config():
    result = _cmd_provider()
    assert "已配置的厂商" in result


def test_pool_empty():
    result = _cmd_pool(session_state={})
    assert "调用池为空" in result


def test_set_profile_no_pool():
    result = _cmd_set_profile(args="", session_state={})
    assert "调用池不可用" in result


def test_import_models_needs_file():
    from xenon.repl.model_registry import ModelRegistry

    reg = ModelRegistry()
    result = _cmd_import_models(args="", registry=reg, session_state={})
    assert "用法" in result


def test_reload_models_nonexistent():
    from xenon.repl.model_registry import ModelRegistry

    reg = ModelRegistry()
    result = _cmd_reload_models(
        args="/nonexistent/file.yaml", registry=reg, session_state={}
    )
    assert "不存在" in result


def test_set_model_no_args_no_provider(monkeypatch):
    from xenon.repl.model_registry import ModelRegistry

    # Mock no configured providers
    monkeypatch.setattr(
        "xenon.repl.provider_registry.get_configured_providers",
        lambda: [],
    )
    reg = ModelRegistry()
    result = _cmd_set_model(args="", registry=reg, session_state={})
    assert "尚未配置任何 API Key" in result
