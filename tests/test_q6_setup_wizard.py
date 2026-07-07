"""P3-Q6 setup_wizard: _clean_api_key export 前缀 + 连通性测试 + 删 key 联动清理。"""

from __future__ import annotations

from types import SimpleNamespace

from omniagent.repl.setup_wizard import (
    _clean_api_key,
    _test_key_connectivity,
    _purge_provider_models,
)
from omniagent.repl.model_registry import ModelRegistry


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
    import omniagent.repl.setup_wizard as sw
    monkeypatch.setattr(sw, "fetch_provider_models", lambda p, k: ["gpt-4", "gpt-3.5"])
    monkeypatch.setattr(sw, "MODEL_FETCH_ERRORS", {})
    ok, detail = _test_key_connectivity(provider, "sk-valid")
    assert ok is True
    assert "2" in detail


def test_connectivity_fail(monkeypatch):
    provider = SimpleNamespace(key="openai")
    import omniagent.repl.setup_wizard as sw
    monkeypatch.setattr(sw, "fetch_provider_models", lambda p, k: [])
    monkeypatch.setattr(sw, "MODEL_FETCH_ERRORS", {"openai": "HTTP 401: Unauthorized"})
    ok, detail = _test_key_connectivity(provider, "sk-bad")
    assert ok is False
    assert "401" in detail


def test_connectivity_fail_unknown_error(monkeypatch):
    provider = SimpleNamespace(key="anthropic")
    import omniagent.repl.setup_wizard as sw
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
