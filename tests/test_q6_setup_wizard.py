"""P3-Q6 setup_wizard: _clean_api_key export 前缀 + 连通性测试 + 删 key 联动清理。"""

from __future__ import annotations

from types import SimpleNamespace

from xenon.repl.model_registry import ModelRegistry
from xenon.repl.setup_wizard import (
    _clean_api_key,
    _purge_provider_models,
    _test_key_connectivity,
)

# --------------------------- _clean_api_key export 前缀 ---------------------------


def test_clean_strips_export_prefix_double_quotes():
    raw = 'export OPENAI_API_KEY="sk-abc123"'
    assert _clean_api_key(raw) == "sk-abc123"


def test_clean_strips_export_prefix_single_quotes():
    raw = "export OPENAI_API_KEY='sk-abc123'"
    assert _clean_api_key(raw) == "sk-abc123"


def test_clean_strips_export_prefix_no_quotes():
    raw = "export OPENAI_API_KEY=sk-abc123"
    assert _clean_api_key(raw) == "sk-abc123"


def test_clean_strips_var_prefix_without_export():
    raw = "DEEPSEEK_API_KEY=sk-xyz"
    assert _clean_api_key(raw) == "sk-xyz"


def test_clean_preserves_plain_key():
    assert _clean_api_key("sk-plainkey123") == "sk-plainkey123"


def test_clean_handles_multiline_paste():
    # 粘贴多行，取首行并剥前缀
    raw = 'export OPENAI_API_KEY="sk-real"\nsome other line\n'
    assert _clean_api_key(raw) == "sk-real"


def test_clean_empty():
    assert _clean_api_key("") == ""
    assert _clean_api_key("   \n  ") == ""


def test_clean_strips_surrounding_whitespace():
    assert _clean_api_key("  sk-key  ") == "sk-key"


def test_clean_does_not_strip_partial_prefix():
    # key 本身含 = 不应误剥（前缀要求 VAR= 形式且 VAR 是合法标识符在开头）
    # "sk-abc=def" 没有 export/VAR= 前缀（sk-abc 不是合法 VAR 名开头？sk 是字母，- 不在标识符）
    # 正则 [A-Za-z_][A-Za-z0-9_]* 匹配 "sk" 然后 "-" 不匹配 \s*= → 不剥
    assert _clean_api_key("sk-abc=def") == "sk-abc=def"


# --------------------------- _test_key_connectivity ---------------------------


def test_connectivity_ok(monkeypatch):
    provider = SimpleNamespace(key="openai")
    import xenon.repl.setup_wizard as sw

    monkeypatch.setattr(sw, "fetch_provider_models", lambda p, k: ["gpt-4", "gpt-3.5"])
    monkeypatch.setattr(sw, "MODEL_FETCH_ERRORS", {})
    ok, detail = _test_key_connectivity(provider, "sk-valid")
    assert ok is True
    assert "2" in detail


def test_connectivity_fail(monkeypatch):
    provider = SimpleNamespace(key="openai")
    import xenon.repl.setup_wizard as sw

    monkeypatch.setattr(sw, "fetch_provider_models", lambda p, k: [])
    monkeypatch.setattr(sw, "MODEL_FETCH_ERRORS", {"openai": "HTTP 401: Unauthorized"})
    ok, detail = _test_key_connectivity(provider, "sk-bad")
    assert ok is False
    assert "401" in detail


def test_connectivity_fail_unknown_error(monkeypatch):
    provider = SimpleNamespace(key="anthropic")
    import xenon.repl.setup_wizard as sw

    monkeypatch.setattr(sw, "fetch_provider_models", lambda p, k: [])
    monkeypatch.setattr(sw, "MODEL_FETCH_ERRORS", {})
    ok, detail = _test_key_connectivity(provider, "sk-x")
    assert ok is False
    assert "未知" in detail


# --------------------------- _purge_provider_models ---------------------------


def _registry_with_models():
    reg = ModelRegistry()
    reg.add_model("openai/gpt-4", "gpt4")
    reg.add_model("openai/gpt-3.5", "gpt35")
    reg.add_model("deepseek/deepseek-chat", "ds")
    reg.assign_role("planner", ["gpt4", "ds"])
    reg.assign_role("coder", ["gpt35"])
    return reg


def test_purge_removes_only_target_provider_models():
    reg = _registry_with_models()
    removed = _purge_provider_models(reg, "openai")
    assert removed == 2
    aliases = list(reg.models.keys())
    assert aliases == ["ds"]


def test_purge_cleans_role_priority():
    reg = _registry_with_models()
    _purge_provider_models(reg, "openai")
    # planner 原本 [gpt4, ds] → 删 gpt4 后剩 [ds]，应保留
    assert reg.role_priority.get("planner") == ["ds"]
    # coder 原本 [gpt35] → 删后空 → 应被删除（重置）
    assert "coder" not in reg.role_priority


def test_purge_no_matching_provider():
    reg = _registry_with_models()
    removed = _purge_provider_models(reg, "nonexistent")
    assert removed == 0
    assert len(reg.models) == 3


def test_purge_all_then_roles_reset():
    reg = _registry_with_models()
    _purge_provider_models(reg, "openai")
    _purge_provider_models(reg, "deepseek")
    # 全删后无模型，所有角色清空
    assert len(reg.models) == 0
    assert all(not v for v in reg.role_priority.values())


def test_purge_preserves_other_provider_role():
    reg = _registry_with_models()
    _purge_provider_models(reg, "openai")
    # ds 仍可用，planner 仍指向它
    assert reg.get_model("ds") is not None
    assert "ds" in reg.role_priority.get("planner", [])


def _configured_model_providers():
    return [
        SimpleNamespace(
            key="openai",
            name="OpenAI",
            models=["gpt-4o", "gpt-4o-mini", "gpt-3.5-turbo"],
            model_error=None,
        )
    ]


def _select_models_for_input(monkeypatch, response, providers=None, model_pool=None):
    import xenon.repl.setup_wizard as sw

    configured_providers = providers or _configured_model_providers()
    monkeypatch.setattr(
        sw,
        "get_configured_providers",
        lambda: configured_providers,
    )

    def ask(prompt, **kwargs):
        assert "可选择多个" in prompt
        assert kwargs["default"] == "1"
        # Rich returns the default when the user submits an empty answer.
        return response or kwargs["default"]

    def reject_integer_prompt(*args, **kwargs):
        raise AssertionError("multi-select must use the text prompt")

    monkeypatch.setattr(sw.Prompt, "ask", ask)
    monkeypatch.setattr(sw.IntPrompt, "ask", reject_integer_prompt)

    registry = ModelRegistry()
    sw._select_model(registry, model_pool)
    return registry


def test_select_model_supports_multiple_single_and_default_choices(monkeypatch):
    registry = _select_models_for_input(monkeypatch, "1 3")
    assert list(registry.models) == ["gpt-4o", "gpt-3-5-turbo"]
    assert registry.role_priority["planner"] == ["gpt-4o", "gpt-3-5-turbo"]

    registry = _select_models_for_input(monkeypatch, "1")
    assert list(registry.models) == ["gpt-4o"]
    assert registry.role_priority["planner"] == ["gpt-4o"]

    registry = _select_models_for_input(monkeypatch, "")
    assert list(registry.models) == ["gpt-4o"]
    assert registry.role_priority["planner"] == ["gpt-4o"]


def test_select_model_keeps_same_named_models_from_different_providers(monkeypatch):
    providers = [
        SimpleNamespace(key="openai", name="OpenAI", models=["gpt-4o"]),
        SimpleNamespace(key="openrouter", name="OpenRouter", models=["gpt-4o"]),
    ]

    registry = _select_models_for_input(monkeypatch, "1 2", providers=providers)

    assert {alias: model.model_id for alias, model in registry.models.items()} == {
        "gpt-4o": "openai/gpt-4o",
        "openrouter-gpt-4o": "openrouter/gpt-4o",
    }
    assert registry.role_priority["planner"] == ["gpt-4o", "openrouter-gpt-4o"]


def test_select_model_suffixes_alias_when_provider_alias_is_taken(monkeypatch):
    providers = [
        SimpleNamespace(
            key="openai",
            name="OpenAI",
            models=["gpt-4o", "openrouter-gpt-4o"],
        ),
        SimpleNamespace(key="openrouter", name="OpenRouter", models=["gpt-4o"]),
    ]

    registry = _select_models_for_input(monkeypatch, "1 2 3", providers=providers)

    assert {alias: model.model_id for alias, model in registry.models.items()} == {
        "gpt-4o": "openai/gpt-4o",
        "openrouter-gpt-4o": "openai/openrouter-gpt-4o",
        "openrouter-gpt-4o-2": "openrouter/gpt-4o",
    }
    assert registry.role_priority["planner"] == [
        "gpt-4o",
        "openrouter-gpt-4o",
        "openrouter-gpt-4o-2",
    ]


def test_select_model_registers_selected_models_in_pool(monkeypatch):
    import xenon.repl.provider_registry as provider_registry
    import xenon.repl.setup_wizard as sw

    calls = []
    model_pool = SimpleNamespace(register=lambda *args, **kwargs: calls.append((args, kwargs)))
    provider_info = SimpleNamespace(base_url="https://api.example.test")
    monkeypatch.setattr(sw, "PROVIDERS", {"openai": provider_info})
    monkeypatch.setattr(sw, "load_credentials", lambda: {"openai": "test-key"})
    monkeypatch.setattr(
        provider_registry,
        "_resolve_api_key",
        lambda provider, info, creds: creds[provider],
    )

    registry = _select_models_for_input(
        monkeypatch,
        "1",
        model_pool=model_pool,
    )

    assert registry.role_priority["planner"] == ["gpt-4o"]
    assert calls == [
        (
            ("openai/gpt-4o",),
            {
                "alias": "gpt-4o",
                "weight": 3.0,
                "api_key": "test-key",
                "base_url": "https://api.example.test",
                "skip_benchmark": True,
            },
        )
    ]


def test_select_model_deduplicates_repeated_indices(monkeypatch):
    registry = _select_models_for_input(monkeypatch, "1 1 3 1")

    assert list(registry.models) == ["gpt-4o", "gpt-3-5-turbo"]
    assert registry.role_priority["planner"] == ["gpt-4o", "gpt-3-5-turbo"]


def test_select_model_rejects_invalid_tokens_without_partial_selection(monkeypatch):
    for response in ("1 x 3", "²"):
        registry = _select_models_for_input(monkeypatch, response)

        assert registry.models == {}
        assert registry.role_priority == {}


def test_select_model_rejects_out_of_range_selection(monkeypatch):
    registry = _select_models_for_input(monkeypatch, "1 4")

    assert registry.models == {}
    assert registry.role_priority == {}


def _configured_test_providers():
    return [
        SimpleNamespace(key="openai", name="OpenAI"),
        SimpleNamespace(key="anthropic", name="Anthropic"),
    ]


def _scripted_int_prompt(responses: list[int], choices: list[str]):
    responses = iter(responses)

    def ask(*args, **kwargs):
        assert kwargs["choices"] == choices
        return next(responses)

    return ask


def test_remove_api_key_cancel_then_selected_provider_only(monkeypatch, tmp_path):
    import xenon.repl.setup_wizard as sw

    credentials_path = tmp_path / "credentials.yaml"
    credentials_path.write_text(
        "openai: openai-test-key\nanthropic: anthropic-test-key\n",
        encoding="utf-8",
    )
    from xenon.utils.credentials_io import load_document

    original_contents = load_document(credentials_path)
    monkeypatch.setenv("XENON_CREDENTIALS_PATH", str(credentials_path))
    monkeypatch.setattr(sw, "get_configured_providers", _configured_test_providers)
    monkeypatch.setattr(
        sw.IntPrompt, "ask", _scripted_int_prompt([0, 2], ["0", "1", "2"])
    )
    monkeypatch.setattr(sw.Confirm, "ask", lambda *args, **kwargs: True)

    registry = ModelRegistry()
    registry.add_model("openai/gpt-4", "gpt4")
    registry.add_model("anthropic/claude-3-haiku", "claude")
    registry.add_model("deepseek/deepseek-chat", "ds")
    registry.assign_role("planner", ["claude", "ds"])

    original_models = dict(registry.models)
    original_roles = dict(registry.role_priority)

    # A zero choice must leave credentials and the model registry untouched.
    sw._remove_api_key(registry)

    assert load_document(credentials_path) == original_contents
    assert registry.models == original_models
    assert registry.role_priority == original_roles

    # Selecting provider 2 must remove only Anthropic credentials and models.
    sw._remove_api_key(registry)

    assert load_document(credentials_path) == {"openai": "openai-test-key"}
    assert list(registry.models) == ["gpt4", "ds"]
    assert registry.role_priority["planner"] == ["ds"]
