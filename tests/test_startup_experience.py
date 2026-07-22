"""Startup ordering, model truthfulness, and probe-noise regressions."""

from __future__ import annotations

import io
import logging

from rich.console import Console

from xenon.repl.provider_registry import ProviderInfo
from xenon.repl.repl import REPL


def _provider(
    name: str,
    key: str,
    models: list[str],
    *,
    error: str = "",
) -> ProviderInfo:
    return ProviderInfo(
        name=name,
        key=key,
        base_url=f"https://{key}.example/v1",
        env_key=f"{key.upper()}_API_KEY",
        models=models,
        api_key="secret",
        model_error=error,
    )


def test_provider_discovery_precedes_model_aware_welcome(monkeypatch):
    configured = [
        _provider("DeepSeek", "deepseek", ["deepseek-v4-pro", "deepseek-v4-flash"]),
        _provider(
            "OpenAI",
            "openai",
            [],
            error='HTTP 401: {"error":"secret provider response"}',
        ),
    ]
    monkeypatch.setattr(
        "xenon.repl.provider_registry.load_credentials",
        lambda: {"deepseek": "secret", "openai": "bad"},
    )
    monkeypatch.setattr(
        "xenon.repl.provider_registry.get_configured_providers",
        lambda: configured,
    )
    output = io.StringIO()
    monkeypatch.setattr(
        "xenon.repl.repl.console",
        Console(file=output, width=100, force_terminal=False),
    )
    repl = REPL(streaming=False)

    summary = repl._check_first_run()
    repl._print_welcome()
    repl._render_startup_summary(summary)

    rendered = output.getvalue()
    assert "deepseek-v4-pro" in rendered
    assert "未配置" not in rendered
    assert "已准备 2 个模型" in rendered
    assert "OpenAI 模型列表不可用：认证失败（HTTP 401）" in rendered
    assert "secret provider response" not in rendered


def test_default_startup_suppresses_httpx_probe_info(monkeypatch):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    network_logger = logging.getLogger("httpx")
    previous_level = network_logger.level
    network_logger.addHandler(handler)
    network_logger.setLevel(logging.INFO)

    def fake_configured():
        network_logger.info("HTTP Request: GET /models 401 Unauthorized")
        return [_provider("OpenAI", "openai", [], error="HTTP 401: private body")]

    monkeypatch.setattr(
        "xenon.repl.provider_registry.load_credentials",
        lambda: {"openai": "bad"},
    )
    monkeypatch.setattr(
        "xenon.repl.provider_registry.get_configured_providers",
        fake_configured,
    )
    try:
        REPL(streaming=False, verbose=False)._check_first_run()
    finally:
        network_logger.removeHandler(handler)
        network_logger.setLevel(previous_level)

    assert stream.getvalue() == ""


def test_verbose_startup_preserves_provider_probe_logs(monkeypatch):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    network_logger = logging.getLogger("httpx")
    previous_level = network_logger.level
    network_logger.addHandler(handler)
    network_logger.setLevel(logging.INFO)

    def fake_configured():
        network_logger.info("provider probe detail")
        return []

    monkeypatch.setattr("xenon.repl.provider_registry.load_credentials", lambda: {})
    monkeypatch.setattr(
        "xenon.repl.provider_registry.get_configured_providers",
        fake_configured,
    )
    try:
        REPL(streaming=False, verbose=True)._check_first_run()
    finally:
        network_logger.removeHandler(handler)
        network_logger.setLevel(previous_level)

    assert "provider probe detail" in stream.getvalue()


def test_run_initializes_models_before_rendering_welcome(monkeypatch):
    repl = REPL(streaming=False)
    events: list[str] = []
    reads = iter((KeyboardInterrupt(), KeyboardInterrupt()))
    summary = {"needs_setup": False, "loaded_models": 1, "failures": []}

    monkeypatch.setattr(repl, "_set_console_title", lambda: events.append("title"))
    monkeypatch.setattr(
        repl,
        "_check_first_run",
        lambda: events.append("models") or summary,
    )
    monkeypatch.setattr(repl, "_print_welcome", lambda: events.append("welcome"))
    monkeypatch.setattr(
        repl,
        "_render_startup_summary",
        lambda value: events.append("summary"),
    )
    monkeypatch.setattr(repl, "_load_custom_commands", lambda: events.append("commands"))
    monkeypatch.setattr(
        repl,
        "_preload_mcp_server_configs",
        lambda: events.append("mcp"),
    )
    monkeypatch.setattr(repl, "_check_auto_resume", lambda: None)
    monkeypatch.setattr(repl, "_auto_save_session", lambda: None)
    monkeypatch.setattr(repl, "_print_exit_report", lambda: None)
    monkeypatch.setattr(repl.status_bar, "print_status", lambda: None)
    monkeypatch.setattr(repl, "_read_input", lambda: (_ for _ in ()).throw(next(reads)))
    monkeypatch.setattr(repl._terminal_activity, "close", lambda: None)

    repl.run()

    assert events[:6] == ["title", "models", "welcome", "summary", "commands", "mcp"]
