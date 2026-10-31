"""setup_wizard 交互流程测试（issue #149）。

Covers the setup wizard's menu text (_model_hint, _mode_scene) and its
interactive flows. Flow tests run inside the ``wizard_env`` fixture, which keeps
them away from the real home directory, real credentials and the network.
"""

from __future__ import annotations

import io
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from rich.console import Console

import xenon.repl.setup_wizard as sw
from xenon.modes import MODE_CATALOG
from xenon.repl import provider_registry, system_config
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.provider_registry import list_providers
from xenon.repl.setup_wizard import _mode_scene, _model_hint

# --------------------------- _model_hint ---------------------------


@pytest.mark.parametrize(
    ("model_name", "expected"),
    [
        ("gpt-4o", "旗舰，全能"),  # flagship, all-rounder
        ("claude-3-5-sonnet-20241022", "旗舰，编程强"),  # flagship, strong at coding
        (
            "deepseek-v4-pro",
            "旗舰编程与复杂 Agent · 1M 上下文",  # flagship for coding and complex agents · 1M context
        ),
        ("gemini-1.5-pro", "长上下文"),  # long context
        ("glm-4-flash", "快速，免费"),  # fast, free
        ("qwen-turbo", "快速，便宜"),  # fast, cheap
        ("moonshot-v1-128k", "128K 上下文"),  # 128K context
    ],
    ids=["gpt-4o", "claude-3-5-sonnet", "deepseek-v4-pro", "gemini-1.5-pro", "glm-4-flash", "qwen-turbo", "moonshot-128k"],
)
def test_model_hint_known_models(model_name, expected):
    """Each known model shows its exact hint text in the model menu."""
    assert _model_hint(model_name) == expected


@pytest.mark.parametrize("model_name", ["some-new-model", ""], ids=["unknown-model", "empty"])
def test_model_hint_unknown_returns_empty(model_name):
    """A model the table doesn't know gets a blank hint, not None or an error."""
    assert _model_hint(model_name) == ""


# --------------------------- _mode_scene ---------------------------


@pytest.mark.parametrize(
    ("mode_name", "expected"),
    [
        ("plan-execute", "复杂任务、多步骤编程"),  # complex tasks, multi-step programming
        ("react", "探索性任务、试错调试"),  # exploratory tasks, trial-and-error debugging
        ("reflection", "高质量输出、代码审查"),  # high-quality output, code review
        ("plan-react", "全局规划+灵活执行"),  # global planning + flexible execution
        ("plan-reflection", "规划执行+质量保证"),  # plan-and-execute + quality assurance
        ("react-reflection", "探索执行+审查修正"),  # exploratory execution + review and correction
    ],
    ids=["plan-execute", "react", "reflection", "plan-react", "plan-reflection", "react-reflection"],
)
def test_mode_scene_builtin_modes(mode_name, expected):
    """The six built-in modes use the wizard's own short "recommended for" text."""
    assert _mode_scene(mode_name) == expected


def test_mode_scene_short_text_wins_over_catalog():
    """For a built-in mode, the wizard's short text is used, not the catalog text."""
    catalog_description = MODE_CATALOG["react"].description
    short_text = "探索性任务、试错调试"  # exploratory tasks, trial-and-error debugging
    # Precondition: the two texts really differ, otherwise this test proves nothing.
    assert catalog_description != short_text

    assert _mode_scene("react") == short_text


def test_mode_scene_direct_falls_back_to_catalog():
    """The "direct" mode is not in the wizard's table, so the catalog text is shown."""
    expected = MODE_CATALOG["direct"].description
    assert expected  # the fallback must produce real text, not a blank row

    assert _mode_scene("direct") == expected


def test_mode_scene_unknown_mode_is_blank():
    """An unknown mode has no description; the catalog is the only source (#202)."""
    assert _mode_scene("test-wizard-fake") == ""


def test_mode_scene_unknown_mode_returns_empty():
    """A mode that is neither built in nor registered gets a blank text."""
    assert _mode_scene("no-such-mode") == ""


# --------------------------- wizard_env: isolation for flow tests ---------------------------


@pytest.fixture
def wizard_env(tmp_path, monkeypatch):
    """Run one wizard flow in a sealed-off temp world, and fail loudly on any leak.

    Gives the test: ``creds_path`` (temp credentials file, not created yet),
    ``inputs`` (keystrokes for rich's prompts, consumed in order) and ``out``
    (everything the wizard printed, as plain text).
    """
    creds_path = tmp_path / "credentials.yaml"
    fake_home = tmp_path / "fake_home"  # deliberately never created

    # Credentials and config come only from temp files.
    monkeypatch.setenv("XENON_CREDENTIALS_PATH", str(creds_path))
    monkeypatch.setattr(system_config, "CONFIG_PATH", tmp_path / "config.yaml")
    # List configured providers without fetching their models over the network.
    monkeypatch.setenv("XENON_SKIP_MODEL_PROBE", "1")
    # Real API keys in the environment would count as "configured"; remove them.
    for provider in list_providers():
        if provider.env_key:
            monkeypatch.delenv(provider.env_key, raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    # Safety net 1: any runtime use of the home directory lands in fake_home.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    # Safety net 2: every credentials read or write must stay inside tmp_path.
    real_get_credentials_path = provider_registry._get_credentials_path

    def guarded_get_credentials_path():
        path = real_get_credentials_path()
        if not path.resolve().is_relative_to(tmp_path.resolve()):
            raise AssertionError(f"credentials path escaped tmp_path: {path}")
        return path

    monkeypatch.setattr(
        provider_registry, "_get_credentials_path", guarded_get_credentials_path
    )

    # Safety net 3: no network.
    def no_network(*args, **kwargs):
        raise AssertionError("network access is blocked in wizard tests")

    monkeypatch.setattr(socket.socket, "connect", no_network)

    # Keystrokes: rich's prompts read with input(), so feed it from a list.
    inputs: list[str] = []

    def scripted_input(prompt=""):
        if not inputs:
            raise AssertionError("wizard asked for more input than the test scripted")
        return inputs.pop(0)

    monkeypatch.setattr("builtins.input", scripted_input)

    # Wizard output goes to memory as plain text (no colours, fixed width).
    out = io.StringIO()
    monkeypatch.setattr(
        sw,
        "console",
        Console(file=out, width=200, color_system=None, legacy_windows=False),
    )

    yield SimpleNamespace(creds_path=creds_path, inputs=inputs, out=out)

    # Safety net 1, checked after the test: nothing may appear under fake_home.
    if fake_home.exists():
        created = sorted(str(p.relative_to(fake_home)) for p in fake_home.rglob("*"))
        raise AssertionError(f"test wrote under the home directory: {created}")


def _fake_provider(**overrides):
    """A configured provider as _show_configured sees it."""
    fields = {"name": "FakeAI", "api_key": "sk-fake-key-000000", "models": [], "model_error": ""}
    fields.update(overrides)
    return SimpleNamespace(**fields)


# --------------------------- _show_configured ---------------------------


def test_show_configured_empty_state_shows_help(wizard_env):
    """With no key anywhere, the wizard prints help instead of an empty table."""
    sw._show_configured()

    # 尚未配置任何 API Key = "no API key configured yet"
    assert "尚未配置任何 API Key" in wizard_env.out.getvalue()


def test_show_configured_masks_long_key(wizard_env):
    """A key longer than 12 characters shows only its first 8 and last 4 characters."""
    key = "sk-abcdefgh-1234-wxyz"
    wizard_env.creds_path.write_text(f"openai: {key}\n", encoding="utf-8")

    sw._show_configured()

    output = wizard_env.out.getvalue()
    assert "sk-abcde****wxyz" in output  # key[:8] + "****" + key[-4:]
    assert key not in output  # the full key must never be printed


@pytest.mark.parametrize("key", ["zq9-", "zq9-zq9-zq9-"], ids=["4-chars", "12-chars"])
def test_show_configured_fully_masks_short_key(wizard_env, key):
    """A key of 12 characters or fewer is shown only as ****."""
    wizard_env.creds_path.write_text(f"openai: {key}\n", encoding="utf-8")

    sw._show_configured()

    output = wizard_env.out.getvalue()
    assert key not in output
    cells = [cell.strip() for line in output.splitlines() for cell in line.split("│")]
    assert "****" in cells  # the API Key cell is exactly ****


def test_show_configured_lists_only_first_four_models(wizard_env, monkeypatch):
    """The models column shows at most four models per provider."""
    provider = _fake_provider(models=["model-1", "model-2", "model-3", "model-4", "model-5"])
    monkeypatch.setattr(sw, "get_configured_providers", lambda: [provider])

    sw._show_configured()

    output = wizard_env.out.getvalue()
    assert "model-4" in output
    assert "model-5" not in output


def test_show_configured_shows_model_fetch_error(wizard_env, monkeypatch):
    """When a provider's model list could not be fetched, the reason is shown."""
    provider = _fake_provider(model_error="HTTP 401: Unauthorized")
    monkeypatch.setattr(sw, "get_configured_providers", lambda: [provider])

    sw._show_configured()

    assert "HTTP 401: Unauthorized" in wizard_env.out.getvalue()


# --------------------------- _select_mode ---------------------------


def _mode_number(registry, mode_name):
    """The menu number the wizard shows for a mode (menus count from 1)."""
    return str(list(registry.modes).index(mode_name) + 1)


def test_select_mode_switches_to_chosen_mode(wizard_env):
    """Typing a mode's number makes it the registry's current mode."""
    registry = ModelRegistry()
    wizard_env.inputs.append(_mode_number(registry, "reflection"))

    sw._select_mode(registry)

    assert registry.current_mode == "reflection"
    assert wizard_env.inputs == []


def test_select_mode_marks_only_the_current_mode(wizard_env):
    """Exactly one row carries the "current" marker, and it is the registry's current mode."""
    registry = ModelRegistry()
    registry.set_mode(list(registry.modes)[-1])  # not the default, so the marker must follow state
    current = registry.current_mode
    wizard_env.inputs.append(_mode_number(registry, current))  # keep the same mode

    sw._select_mode(registry)

    # 当前 = "current"
    marked_rows = [line for line in wizard_env.out.getvalue().splitlines() if "当前" in line]
    assert len(marked_rows) == 1
    assert current in [cell.strip() for cell in marked_rows[0].split("│")]


def test_select_mode_shows_recommended_scene_for_each_mode(wizard_env):
    """The "recommended for" column shows _mode_scene's text for every mode."""
    registry = ModelRegistry()
    wizard_env.inputs.append(_mode_number(registry, registry.current_mode))

    sw._select_mode(registry)

    # Table rows split on "│" into ['', '#', name, description, recommended-for, status, ''];
    # the header row uses "┃" instead, so it is skipped.
    rows = [[cell.strip() for cell in line.split("│")] for line in wizard_env.out.getvalue().splitlines()]
    scene_by_mode = {cells[2]: cells[4] for cells in rows if len(cells) == 7}
    wrong = [name for name in registry.modes if scene_by_mode.get(name) != _mode_scene(name)]
    assert wrong == []


def test_select_mode_reasks_after_out_of_range_number(wizard_env):
    """Out-of-range numbers are rejected and asked again; the valid answer still wins."""
    registry = ModelRegistry()
    wizard_env.inputs.extend(["0", "99", _mode_number(registry, "reflection")])

    sw._select_mode(registry)

    assert registry.current_mode == "reflection"
    assert wizard_env.inputs == []


# --------------------------- interactive_setup: menu dispatch only ---------------------------

# Stand-ins passed to interactive_setup, so a call record shows which one a helper got.
_REGISTRY = SimpleNamespace(role="fake registry")
_POOL = SimpleNamespace(role="fake model pool")


def _recorder(calls, name):
    """A stand-in for a menu helper: it only records that it was called, and with what."""

    def record(*args):
        calls.append((name, args))

    return record


@pytest.fixture
def menu_helper_calls(monkeypatch):
    """Replace all six menu helpers with recorders, so no real flow runs.

    Only the menu's routing to each helper is tested here. The real _select_model
    and _remove_api_key are tested elsewhere or have their own tracked issues
    (#150, #151).
    """
    calls: list[tuple[str, tuple]] = []
    fake_select_model_real_one_never_runs = _recorder(calls, "_select_model")
    fake_remove_api_key_real_one_never_runs = _recorder(calls, "_remove_api_key")

    monkeypatch.setattr(sw, "_setup_api_key", _recorder(calls, "_setup_api_key"))
    monkeypatch.setattr(sw, "_show_configured", _recorder(calls, "_show_configured"))
    monkeypatch.setattr(sw, "_select_model", fake_select_model_real_one_never_runs)
    monkeypatch.setattr(sw, "_select_mode", _recorder(calls, "_select_mode"))
    monkeypatch.setattr(sw, "_remove_api_key", fake_remove_api_key_real_one_never_runs)
    monkeypatch.setattr(sw, "_register_custom", _recorder(calls, "_register_custom"))
    return calls


@pytest.mark.parametrize("keystroke", ["", "0"], ids=["enter-default-0", "zero"])
def test_menu_exits_on_enter_or_zero(wizard_env, menu_helper_calls, keystroke):
    """Enter or 0 leaves the wizard straight away without running any helper."""
    wizard_env.inputs.append(keystroke)

    sw.interactive_setup(_REGISTRY, _POOL)

    assert menu_helper_calls == []
    assert wizard_env.inputs == []


@pytest.mark.parametrize(
    ("choice", "expected_call"),
    [
        ("1", ("_setup_api_key", (_POOL,))),
        ("2", ("_show_configured", ())),
        ("3", ("_select_model", (_REGISTRY, _POOL))),  # fake; the real one never runs
        ("4", ("_select_mode", (_REGISTRY,))),
        ("5", ("_remove_api_key", (_REGISTRY,))),  # fake; the real one never runs
        ("6", ("_register_custom", (_REGISTRY, _POOL))),
    ],
    ids=[
        "1-setup-api-key",
        "2-show-configured",
        "3-select-model-fake",
        "4-select-mode",
        "5-remove-api-key-fake",
        "6-register-custom",
    ],
)
def test_menu_routes_choice_to_its_helper(wizard_env, menu_helper_calls, choice, expected_call):
    """Each menu number calls exactly its own helper, with the right arguments."""
    wizard_env.inputs.extend([choice, "0"])  # pick the item, then exit

    sw.interactive_setup(_REGISTRY, _POOL)

    assert menu_helper_calls == [expected_call]
    assert wizard_env.inputs == []


def test_menu_loops_until_zero(wizard_env, menu_helper_calls):
    """After a helper finishes, the menu comes back; it only stops on 0."""
    wizard_env.inputs.extend(["2", "4", "0"])

    sw.interactive_setup(_REGISTRY, _POOL)

    assert menu_helper_calls == [("_show_configured", ()), ("_select_mode", (_REGISTRY,))]


def test_menu_reasks_after_invalid_choice(wizard_env, menu_helper_calls):
    """A choice that is not on the menu is asked again and runs nothing."""
    wizard_env.inputs.extend(["9", "x", "0"])

    sw.interactive_setup(_REGISTRY, _POOL)

    assert menu_helper_calls == []
    assert wizard_env.inputs == []


# --------------------------- _setup_api_key: empty key cancels ---------------------------


def _must_not_be_called(name):
    """A stand-in that fails the test if the code under test ever calls it."""

    def fail(*args, **kwargs):
        raise AssertionError(f"{name} must not be called in this test")

    return fail


def _provider_number(provider_key):
    """The menu number _setup_api_key shows for a provider (menus count from 1)."""
    return str([p.key for p in list_providers()].index(provider_key) + 1)


@pytest.fixture
def cancel_path(wizard_env, monkeypatch):
    """wizard_env plus a saved fake openai key, with saving and testing a key forbidden."""
    wizard_env.creds_path.write_text("openai: sk-fake-existing-key-1234\n", encoding="utf-8")
    monkeypatch.setattr(sw, "_test_key_connectivity", _must_not_be_called("_test_key_connectivity"))
    monkeypatch.setattr(sw, "set_provider_key", _must_not_be_called("set_provider_key"))
    return wizard_env


@pytest.mark.parametrize("typed_key", ["", "   "], ids=["empty", "spaces"])
def test_setup_api_key_empty_key_cancels(cancel_path, monkeypatch, typed_key):
    """An empty key cancels: nothing is tested or saved and the credentials file is untouched."""
    before = cancel_path.creds_path.read_bytes()
    monkeypatch.setattr(sw, "_masked_input", lambda prompt: typed_key)
    cancel_path.inputs.append(_provider_number("openai"))

    sw._setup_api_key(model_pool=None)

    assert cancel_path.creds_path.read_bytes() == before
    assert cancel_path.inputs == []
    output = cancel_path.out.getvalue()
    # 已取消 = "cancelled"
    assert "已取消" in output
    # The status column was read from our temp file: openai shows 已配置 ("configured").
    # Table rows split on "│" into ['', '#', name, models, status, ''].
    openai_name = next(p.name for p in list_providers() if p.key == "openai")
    rows = [[cell.strip() for cell in line.split("│")] for line in output.splitlines()]
    openai_row = next(cells for cells in rows if len(cells) == 6 and cells[2] == openai_name)
    assert openai_row[4] == "已配置"


def test_setup_api_key_reasks_invalid_number_then_cancels(cancel_path, monkeypatch):
    """Numbers outside the provider list are asked again; the flow still cancels cleanly."""
    before = cancel_path.creds_path.read_bytes()
    monkeypatch.setattr(sw, "_masked_input", lambda prompt: "")
    past_the_end = str(len(list_providers()) + 2)  # one past the "custom provider" row
    cancel_path.inputs.extend(["0", past_the_end, _provider_number("openai")])

    sw._setup_api_key(model_pool=None)

    assert cancel_path.creds_path.read_bytes() == before
    assert cancel_path.inputs == []


# --------------------------- interactive_setup: real end-to-end smoke test ---------------------------


def test_menu_end_to_end_switches_mode(wizard_env):
    """With no fakes for the helpers, menu 4 then a mode number really switches the mode."""
    registry = ModelRegistry()
    target = next(name for name in registry.modes if name != registry.current_mode)
    wizard_env.inputs.extend(["4", _mode_number(registry, target), "0"])

    sw.interactive_setup(registry)

    assert registry.current_mode == target
    assert wizard_env.inputs == []


# --------------------------- _setup_api_key: saving a key ---------------------------

_FAKE_KEY = "sk-fake-not-a-real-key"
_OTHER_FAKE_KEY = "sk-fake-other-not-real"


@pytest.fixture
def save_path(wizard_env, monkeypatch):
    """wizard_env with another provider's fake key already saved and the typed key scripted."""
    wizard_env.creds_path.write_text(f"deepseek: {_OTHER_FAKE_KEY}\n", encoding="utf-8")
    monkeypatch.setattr(sw, "_masked_input", lambda prompt: _FAKE_KEY)
    return wizard_env


def _connectivity(ok):
    """A stand-in for the real connectivity check, which would call the provider's API."""
    return lambda provider, api_key: (ok, "fake: 3 models" if ok else "HTTP 401: fake")


def test_setup_api_key_saves_key_after_successful_check(save_path, monkeypatch, capsys):
    """A key that passes the check is saved next to the keys already there."""
    monkeypatch.setattr(sw, "_test_key_connectivity", _connectivity(True))
    save_path.inputs.append(_provider_number("openai"))

    sw._setup_api_key(model_pool=None)

    saved = yaml.safe_load(save_path.creds_path.read_text(encoding="utf-8"))
    assert saved == {"deepseek": _OTHER_FAKE_KEY, "openai": _FAKE_KEY}
    # The atomic write left no temporary file behind.
    assert [p.name for p in save_path.creds_path.parent.iterdir()] == [save_path.creds_path.name]
    # The full key is never printed, by the wizard or by rich's prompts.
    assert _FAKE_KEY not in save_path.out.getvalue() + capsys.readouterr().out


@pytest.mark.parametrize("answer", ["", "n"], ids=["enter-default-no", "n"])
def test_setup_api_key_failed_check_declined_saves_nothing(save_path, monkeypatch, answer):
    """If the check fails and the user does not say yes, the file is left untouched."""
    before = save_path.creds_path.read_bytes()
    monkeypatch.setattr(sw, "_test_key_connectivity", _connectivity(False))
    monkeypatch.setattr(sw, "set_provider_key", _must_not_be_called("set_provider_key"))
    save_path.inputs.extend([_provider_number("openai"), answer])

    sw._setup_api_key(model_pool=None)

    assert save_path.creds_path.read_bytes() == before
    assert save_path.inputs == []


def test_setup_api_key_failed_check_accepted_saves_key(save_path, monkeypatch):
    """If the check fails but the user answers yes to "save anyway?", the key is saved."""
    monkeypatch.setattr(sw, "_test_key_connectivity", _connectivity(False))
    save_path.inputs.extend([_provider_number("openai"), "y"])

    sw._setup_api_key(model_pool=None)

    saved = yaml.safe_load(save_path.creds_path.read_text(encoding="utf-8"))
    assert saved["openai"] == _FAKE_KEY


def test_setup_api_key_last_row_opens_custom_registration(save_path, monkeypatch):
    """The row after the last built-in provider hands over to custom registration."""
    calls = []
    monkeypatch.setattr(sw, "_register_custom", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(sw, "_masked_input", _must_not_be_called("_masked_input"))
    custom_row = str(len(list_providers()) + 1)  # one past the last provider
    save_path.inputs.append(custom_row)

    sw._setup_api_key(model_pool=_POOL)

    assert calls == [{"registry": None, "model_pool": _POOL}]


# --------------------------- _register_custom ---------------------------

_CUSTOM_URL = "https://fake.invalid/v1"
_CUSTOM_MODELS = [f"fake-model-{n}" for n in range(1, 8)]  # 7 models, no dots
_PREVIEW = 3  # how many models the wizard may register, set through XENON_WIZARD_PREVIEW_MODELS


class _RecordingPool:
    """A stand-in model pool that only records what was registered."""

    def __init__(self):
        self.registered = []

    def register(self, model_id, **kwargs):
        self.registered.append((model_id, kwargs))


def _fake_register_custom_provider(models):
    """A stand-in for register_custom_provider, which would save and fetch over the network."""

    def register(name, base_url, api_key):
        return SimpleNamespace(key="fakecorp", base_url=base_url, models=list(models))

    return register


@pytest.fixture
def custom_env(wizard_env, monkeypatch):
    """wizard_env with a saved fake key and a small, fixed model preview limit."""
    wizard_env.creds_path.write_text(f"deepseek: {_OTHER_FAKE_KEY}\n", encoding="utf-8")
    monkeypatch.setenv("XENON_WIZARD_PREVIEW_MODELS", str(_PREVIEW))
    return wizard_env


@pytest.mark.parametrize("name", ["", "   "], ids=["empty", "spaces"])
def test_register_custom_blank_name_cancels(custom_env, monkeypatch, name):
    """A blank provider name cancels before anything else is asked or saved."""
    before = custom_env.creds_path.read_bytes()
    monkeypatch.setattr(sw, "register_custom_provider", _must_not_be_called("register_custom_provider"))
    monkeypatch.setattr(sw, "_masked_input", _must_not_be_called("_masked_input"))
    custom_env.inputs.append(name)

    sw._register_custom()

    assert custom_env.creds_path.read_bytes() == before
    assert custom_env.inputs == []


def test_register_custom_blank_key_cancels(custom_env, monkeypatch):
    """A blank API key cancels without registering anything."""
    before = custom_env.creds_path.read_bytes()
    monkeypatch.setattr(sw, "register_custom_provider", _must_not_be_called("register_custom_provider"))
    monkeypatch.setattr(sw, "_masked_input", lambda prompt: "")
    custom_env.inputs.extend(["FakeCorp", _CUSTOM_URL])

    sw._register_custom()

    assert custom_env.creds_path.read_bytes() == before


def test_register_custom_registers_models_in_pool(custom_env, monkeypatch, capsys):
    """Success puts the first models into the pool, with the base URL and the cleaned key."""
    monkeypatch.setattr(sw, "register_custom_provider", _fake_register_custom_provider(_CUSTOM_MODELS))
    monkeypatch.setattr(sw, "_masked_input", lambda prompt: f"  {_FAKE_KEY}  ")  # pasted with spaces
    custom_env.inputs.extend(["FakeCorp", _CUSTOM_URL])
    pool = _RecordingPool()

    sw._register_custom(model_pool=pool)

    received = [
        (model_id, kwargs["base_url"], kwargs["api_key"], kwargs["skip_benchmark"])
        for model_id, kwargs in pool.registered
    ]
    expected = [(f"fakecorp/{m}", _CUSTOM_URL, _FAKE_KEY, True) for m in _CUSTOM_MODELS[:_PREVIEW]]
    assert received == expected
    assert _FAKE_KEY not in custom_env.out.getvalue() + capsys.readouterr().out


def test_register_custom_registers_models_in_registry(custom_env, monkeypatch):
    """Without a pool, the first models are added to the registry instead."""
    monkeypatch.setattr(sw, "register_custom_provider", _fake_register_custom_provider(_CUSTOM_MODELS))
    monkeypatch.setattr(sw, "_masked_input", lambda prompt: _FAKE_KEY)
    custom_env.inputs.extend(["FakeCorp", _CUSTOM_URL])
    registry = ModelRegistry()
    before = {mc.model_id for mc in registry.models.values()}

    sw._register_custom(registry=registry)

    added = {mc.model_id for mc in registry.models.values()} - before
    assert added == {f"fakecorp/{m}" for m in _CUSTOM_MODELS[:_PREVIEW]}


def test_register_custom_fetch_failure_registers_nothing(custom_env, monkeypatch):
    """When the model list could not be fetched, no placeholder is put into the pool."""
    placeholder = ["(auto-fetch failed, check base_url and API key)"]
    monkeypatch.setattr(sw, "register_custom_provider", _fake_register_custom_provider(placeholder))
    monkeypatch.setattr(sw, "_masked_input", lambda prompt: _FAKE_KEY)
    custom_env.inputs.extend(["FakeCorp", _CUSTOM_URL])
    pool = _RecordingPool()

    sw._register_custom(model_pool=pool)

    assert pool.registered == []


def test_register_custom_error_is_reported_not_raised(custom_env, monkeypatch):
    """An error during registration is printed instead of crashing the wizard."""

    def failing_register(name, base_url, api_key):
        raise RuntimeError("fake registration failure")

    monkeypatch.setattr(sw, "register_custom_provider", failing_register)
    monkeypatch.setattr(sw, "_masked_input", lambda prompt: _FAKE_KEY)
    custom_env.inputs.extend(["FakeCorp", _CUSTOM_URL])

    sw._register_custom()

    assert "fake registration failure" in custom_env.out.getvalue()


# --------------------------- _masked_input: Windows branch (fake keyboard) ---------------------------


@pytest.fixture
def windows_keys(monkeypatch):
    """Make _masked_input take its Windows branch and read keys from a scripted list.

    monkeypatch undoes both changes after each test, on every operating system:
    the fake msvcrt replaces (or, off Windows, adds) sys.modules["msvcrt"], and
    sys.platform reads "win32" only while the test runs.
    """
    keys: list[str] = []

    def getwch():
        if not keys:
            raise AssertionError("_masked_input read more keys than the test scripted")
        return keys.pop(0)

    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(getwch=getwch))
    monkeypatch.setattr(sys, "platform", "win32")
    return keys


@pytest.mark.parametrize("enter", ["\r", "\n"], ids=["cr", "lf"])
def test_masked_input_returns_typed_text_and_masks_it(windows_keys, capsys, enter):
    """Typed characters are returned on Enter, and each one is echoed as a single *."""
    windows_keys.extend(_FAKE_KEY + enter)  # one list item per character

    result = sw._masked_input("API Key")

    assert result == _FAKE_KEY
    printed = capsys.readouterr().out
    assert printed.count("*") == len(_FAKE_KEY)
    assert _FAKE_KEY not in printed


@pytest.mark.parametrize("backspace", ["\x08", "\x7f"], ids=["backspace", "delete-0x7f"])
def test_masked_input_backspace_removes_last_char(windows_keys, backspace):
    """Backspace removes the last typed character from the result."""
    windows_keys.extend("abcX" + backspace + "\r")

    assert sw._masked_input("API Key") == "abc"


def test_masked_input_backspace_on_empty_input_does_nothing(windows_keys, capsys):
    """Backspace with nothing typed is ignored: nothing to remove, nothing erased on screen."""
    windows_keys.extend("\x08ab\r")

    assert sw._masked_input("API Key") == "ab"
    assert "\b" not in capsys.readouterr().out


def test_masked_input_escape_clears_input(windows_keys):
    """Esc throws away everything typed so far; typing then starts again from empty."""
    windows_keys.extend("abc\x1bxy\r")

    assert sw._masked_input("API Key") == "xy"


def test_masked_input_ctrl_c_raises_keyboard_interrupt(windows_keys):
    """Ctrl+C aborts the prompt with KeyboardInterrupt instead of returning text."""
    windows_keys.extend("ab\x03")

    with pytest.raises(KeyboardInterrupt):
        sw._masked_input("API Key")
