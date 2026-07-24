"""P1-A: 批量模型注册测试。"""
from __future__ import annotations

import pytest

from xenon.repl.batch_register import (
    batch_register, parse_file, validate, ModelSpec,
)
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.model_pool import ModelPool


class TestParse:
    def test_parse_yaml(self, tmp_path):
        f = tmp_path / "m.yaml"
        f.write_text(
            "profile: fast\n"
            "models:\n"
            "  - alias: gpt4o\n"
            "    model_id: openai/gpt-4o\n"
            "    weight: 2.0\n"
            "    reasoning_effort: high\n"
            "roles:\n"
            "  planner: [gpt4o]\n"
        )
        specs, roles, profile, errors = parse_file(f)
        assert not errors
        assert profile == "fast"
        assert len(specs) == 1
        assert specs[0].alias == "gpt4o"
        assert specs[0].reasoning_effort == "high"
        assert roles == {"planner": ["gpt4o"]}

    def test_parse_json_compatible(self, tmp_path):
        f = tmp_path / "m.json"
        f.write_text('{"models":[{"alias":"gpt4o","model_id":"openai/gpt-4o"}]}')
        specs, _, _, errors = parse_file(f)
        assert not errors
        assert specs[0].alias == "gpt4o"

    def test_parse_env_ref(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_KEY", "sk-secret")
        f = tmp_path / "m.yaml"
        f.write_text("models:\n  - alias: gpt4o\n    model_id: openai/gpt-4o\n    api_key: ${MY_KEY}\n")
        specs, _, _, errors = parse_file(f)
        assert not errors
        assert specs[0].api_key == "sk-secret"

    def test_parse_dict_models_form(self, tmp_path):
        """兼容 registry.export_config 的 {alias: {fields}} 形式。"""
        f = tmp_path / "m.yaml"
        f.write_text("models:\n  gpt4o:\n    model_id: openai/gpt-4o\n    weight: 3.0\n")
        specs, _, _, errors = parse_file(f)
        assert not errors
        assert specs[0].alias == "gpt4o"
        assert specs[0].weight == 3.0

    def test_parse_missing_file(self, tmp_path):
        specs, _, _, errors = parse_file(tmp_path / "nope.yaml")
        assert errors and "不存在" in errors[0]


class TestValidate:
    def _spec(self, **kw):
        base = dict(alias="gpt4o", model_id="openai/gpt-4o", weight=1.0)
        base.update(kw)
        return ModelSpec(**base)

    def test_valid(self):
        assert validate([self._spec()]) == []

    def test_missing_alias(self):
        errs = validate([self._spec(alias="")])
        assert any("alias" in e for e in errs)

    def test_bad_model_id_no_slash(self):
        errs = validate([self._spec(model_id="gpt4o")])
        assert any("provider/model_name" in e for e in errs)

    def test_bad_alias_chars(self):
        errs = validate([self._spec(alias="gpt 4o")])
        assert any("非法字符" in e for e in errs)

    def test_weight_zero(self):
        errs = validate([self._spec(weight=0)])
        assert any("weight" in e for e in errs)

    def test_tier_out_of_range(self):
        errs = validate([self._spec(tier=9)])
        assert any("tier" in e for e in errs)

    def test_duplicate_alias(self):
        errs = validate([self._spec(), self._spec()])
        assert any("重复" in e for e in errs)

    def test_invalid_reasoning_effort(self):
        errs = validate([self._spec(reasoning_effort="extreme")])
        assert any("reasoning_effort" in e for e in errs)


@pytest.fixture
def stub_probe(monkeypatch):
    """默认全部 probe 通过(避免真发网络请求)。"""
    monkeypatch.setattr("xenon.repl.batch_register.probe_model",
                        lambda s, **kw: (True, ""))


class TestBatchRegister:
    def test_register_basic(self, tmp_path, stub_probe):
        f = tmp_path / "m.yaml"
        f.write_text("models:\n  - alias: gpt4o\n    model_id: openai/gpt-4o\n"
                     "    api_key: sk-t\n    weight: 2.0\n    tier: 4\n")
        r = ModelRegistry()
        p = ModelPool()
        result = batch_register(f, r, p, probe=True)
        assert "gpt4o" in result.registered
        assert r.get_model("gpt4o") is not None
        assert p.get("gpt4o") is not None
        assert p.get("gpt4o").weight == 2.0

    def test_dry_run_no_register(self, tmp_path, stub_probe):
        f = tmp_path / "m.yaml"
        f.write_text("models:\n  - alias: gpt4o\n    model_id: openai/gpt-4o\n    api_key: sk-t\n")
        r = ModelRegistry()
        p = ModelPool()
        result = batch_register(f, r, p, dry_run=True)
        assert not result.registered
        assert r.get_model("gpt4o") is None

    def test_probe_fail_excluded(self, tmp_path, monkeypatch):
        monkeypatch.setattr("xenon.repl.batch_register.probe_model",
                            lambda s, **kw: (False, "401") if s.alias == "bad" else (True, ""))
        f = tmp_path / "m.yaml"
        f.write_text("models:\n  - alias: good\n    model_id: openai/gpt-4o\n    api_key: sk\n"
                     "  - alias: bad\n    model_id: openai/bad\n    api_key: wrong\n")
        r = ModelRegistry()
        p = ModelPool()
        result = batch_register(f, r, p, probe=True)
        assert p.get("good") is not None
        assert p.get("bad") is None
        assert any(a == "bad" for a, _ in result.probed_fail)

    def test_probe_false_skips_probe_call(self, tmp_path, monkeypatch):
        called = []

        def spy(s, **kw):
            called.append(s.alias)
            return (True, "")

        monkeypatch.setattr("xenon.repl.batch_register.probe_model", spy)
        f = tmp_path / "m.yaml"
        f.write_text("models:\n  - alias: gpt4o\n    model_id: openai/gpt-4o\n    api_key: sk\n")
        r = ModelRegistry()
        p = ModelPool()
        batch_register(f, r, p, probe=False)
        assert called == []  # probe=False 不应调用 probe_model

    def test_idempotent_update(self, tmp_path, stub_probe):
        r = ModelRegistry()
        p = ModelPool()
        r.add_model("openai/gpt-4o", "gpt4o", api_key="old", weight=1.0)
        p.register("openai/gpt-4o", alias="gpt4o", weight=1.0, api_key="old")
        f = tmp_path / "m.yaml"
        f.write_text("models:\n  - alias: gpt4o\n    model_id: openai/gpt-4o\n"
                     "    api_key: new\n    weight: 3.0\n")
        result = batch_register(f, r, p, probe=True)
        assert "gpt4o" in result.updated
        assert p.get("gpt4o").weight == 3.0
        assert r.get_model("gpt4o").api_key == "new"

    def test_roles_registered(self, tmp_path, stub_probe):
        f = tmp_path / "m.yaml"
        f.write_text("models:\n  - alias: gpt4o\n    model_id: openai/gpt-4o\n    api_key: sk\n"
                     "  - alias: ds\n    model_id: deepseek/ds\n    api_key: sk\n"
                     "roles:\n  planner: [gpt4o, ds]\n")
        r = ModelRegistry()
        p = ModelPool()
        batch_register(f, r, p, probe=True)
        assert r.role_priority.get("planner") == ["gpt4o", "ds"]


class TestDiscover:
    def test_discover_expand(self, tmp_path, monkeypatch):
        monkeypatch.setattr("xenon.repl.batch_register.probe_model",
                            lambda s, **kw: (True, ""))
        monkeypatch.setattr("xenon.repl.batch_register.discover_models",
                            lambda base_url, api_key, **kw: ["qwen2.5:7b", "llama3:8b"])
        f = tmp_path / "m.yaml"
        f.write_text("models:\n  - alias: ollama-local\n    model_id: ollama/qwen2.5:7b\n"
                     "    base_url: http://localhost:11434\n    discover: true\n")
        r = ModelRegistry()
        p = ModelPool()
        result = batch_register(f, r, p, probe=True)
        assert p.get("ollama-local") is not None      # 父模型
        assert p.get("qwen2-5-7b") is not None         # 子模型 qwen2.5:7b -> qwen2-5-7b
        assert p.get("llama3-8b") is not None
        assert len(result.discovered) == 2

    def test_discover_missing_base_url(self, tmp_path, monkeypatch):
        monkeypatch.setattr("xenon.repl.batch_register.probe_model",
                            lambda s, **kw: (True, ""))
        f = tmp_path / "m.yaml"
        f.write_text("models:\n  - alias: bad\n    model_id: ollama/x\n    discover: true\n")
        r = ModelRegistry()
        p = ModelPool()
        result = batch_register(f, r, p, probe=True)
        assert any("base_url" in reason for _, reason in result.failed)
