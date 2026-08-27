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

    # v0.9.0 懒加载：启动路径（_check_first_run）不再探测 provider，探测逻辑
    # 移到按需入口 ensure_providers_probed()（由 /model、/provider 触发）。
    # 这个用例原本验证"探测结果先于欢迎卡渲染"，现在改为验证探测那条路径本身
    # 仍然正确：模型进池、认证失败被如实报告、且原始错误体不泄露给用户。
    summary = repl.ensure_providers_probed()
    repl._print_welcome()
    repl._render_startup_summary(summary)

    rendered = output.getvalue()
    assert "deepseek-v4-pro" in rendered
    assert "未配置" not in rendered
    assert "已准备 2 个模型" in rendered
    assert "OpenAI 模型列表不可用：认证失败（HTTP 401）" in rendered
    assert "secret provider response" not in rendered


def test_default_startup_suppresses_httpx_probe_info(monkeypatch):
    """懒加载契约后探测发生在 ensure_providers_probed，默认路径仍须抑制 httpx 噪音。

    启动路径不再探测，但 /model 之类触发探测时仍应抑制 httpx INFO（除非 --verbose），
    否则首次 /model 就污染用户终端。v0.9.0 前该抑制发生在 _check_first_run，现在
    在 ensure_providers_probed 里；覆盖不能丢。
    """
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
        REPL(streaming=False, verbose=False).ensure_providers_probed()
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
        # 懒加载后探测发生在 ensure_providers_probed()，--verbose 仍须保留
        # httpx 的诊断日志（默认路径才抑制），这条保证不因搬家而丢失。
        REPL(streaming=False, verbose=True).ensure_providers_probed()
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
    monkeypatch.setattr(
        repl, "_load_custom_commands", lambda: events.append("commands")
    )
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
