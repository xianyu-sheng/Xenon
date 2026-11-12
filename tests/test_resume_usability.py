"""会话恢复可用性修复：CLI 位置参数 + 扁平 credentials 派生模型。"""

from __future__ import annotations

import sys

import yaml


def _run_cli(argv: list[str], monkeypatch) -> dict:
    """跑一次 main.cli() 并拦截结果。"""
    import xenon.main as main_mod

    captured: dict = {}
    monkeypatch.setattr(main_mod, "_cmd_chat", lambda args: captured.setdefault("args", args))
    monkeypatch.setattr(main_mod, "_print_session_list", lambda: captured.setdefault("list", True))
    monkeypatch.setattr(main_mod, "_validate_resume_target", lambda t: t)
    # These tests exercise the built-in editor route; the default route is the
    # TS front-end (covered by tests/test_main_ui_routing.py).
    monkeypatch.setattr(main_mod, "_launch_ts", lambda _args: None)
    monkeypatch.setattr(sys, "argv", ["xenon", *argv])
    main_mod.cli()
    return captured


def test_cli_resume_positional_defaults_to_latest(monkeypatch):
    """"xenon resume"（无参）→ 恢复最近会话（列表第 1 个）。"""
    captured = _run_cli(["resume"], monkeypatch)
    assert captured["args"].resume == "1"


def test_cli_resume_positional_number(monkeypatch):
    captured = _run_cli(["resume", "3"], monkeypatch)
    assert captured["args"].resume == "3"


def test_cli_resume_positional_list_prints_and_exits(monkeypatch):
    captured = _run_cli(["resume", "list"], monkeypatch)
    assert captured == {"list": True}


def test_flat_credentials_derive_builtin_models(tmp_path, monkeypatch):
    """credentials.yaml 只写 ``deepseek: sk-...``（无 providers 段）→ 仍派生模型。"""

    from xenon.repl.model_registry import ModelRegistry

    creds = tmp_path / "credentials.yaml"
    creds.write_text("deepseek: sk-test-token\n", encoding="utf-8")

    registry = ModelRegistry()
    registry.load_from_credentials(credentials_path=creds)
    ids = [m.model_id for m in registry.models.values()]
    assert "deepseek/deepseek-v4-pro" in ids
    assert "deepseek/deepseek-v4-flash" in ids


def test_flat_credentials_ignore_unknown_provider(tmp_path):
    """非内置厂商 key（中转站）无离线目录 → 不猜、不注册。"""

    from xenon.repl.model_registry import ModelRegistry

    creds = tmp_path / "credentials.yaml"
    creds.write_text("my-proxy: sk-xxx\n", encoding="utf-8")
    registry = ModelRegistry()
    registry.load_from_credentials(credentials_path=creds)
    assert len(registry.models) == 0


def test_providers_section_still_wins_over_flat(tmp_path):
    """providers 段存在时走原路径，扁平段不重复注册。"""

    from xenon.repl.model_registry import ModelRegistry

    creds = tmp_path / "credentials.yaml"
    creds.write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "deepseek": {
                        "api_key": "sk-a",
                        "base_url": "https://api.deepseek.com/v1",
                        "models": ["deepseek-v4-flash"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    registry = ModelRegistry()
    registry.load_from_credentials(credentials_path=creds)
    ids = [m.model_id for m in registry.models.values()]
    assert ids == ["deepseek/deepseek-v4-flash"]


def test_startup_picker_returns_chosen_index(monkeypatch, capsys):
    """门前进会话选择器：选 1 → 返回 '1'；选 0 → None（新对话）。"""

    import rich.prompt as rich_prompt

    import xenon.main as main_mod
    import xenon.repl.session as session_mod

    fake_sessions = [
        {
            "name": "_auto",
            "messages": 3,
            "paradigm": "direct",
            "saved_at": "2026-10-12T12:00:00",
        }
    ]
    monkeypatch.setattr(session_mod, "list_sessions", lambda: fake_sessions)
    monkeypatch.setattr(session_mod, "get_session_age", lambda s: "刚刚")

    class _Stdin:
        def isatty(self):
            return True

    monkeypatch.setattr(main_mod.sys, "stdin", _Stdin())

    class _FakeIntPrompt:
        @staticmethod
        def ask(prompt, choices=None, default=None, show_default=True):
            return default

    monkeypatch.setattr(rich_prompt, "IntPrompt", _FakeIntPrompt)
    assert main_mod._interactive_session_picker() == "1"


def test_startup_picker_new_conversation(monkeypatch):
    import rich.prompt as rich_prompt

    import xenon.main as main_mod
    import xenon.repl.session as session_mod

    monkeypatch.setattr(session_mod, "list_sessions", lambda: [{"name": "_auto", "messages": 1}])
    monkeypatch.setattr(session_mod, "get_session_age", lambda s: "刚刚")

    class _Stdin:
        def isatty(self):
            return True

    monkeypatch.setattr(main_mod.sys, "stdin", _Stdin())

    class _FakeIntPrompt:
        @staticmethod
        def ask(prompt, choices=None, default=None, show_default=True):
            return 0

    monkeypatch.setattr(rich_prompt, "IntPrompt", _FakeIntPrompt)
    assert main_mod._interactive_session_picker() is None


def test_startup_picker_skipped_when_not_tty(monkeypatch):
    import xenon.main as main_mod
    import xenon.repl.session as session_mod

    called = []
    monkeypatch.setattr(session_mod, "list_sessions", lambda: called.append(1) or [])

    class _Stdin:
        def isatty(self):
            return False

    monkeypatch.setattr(main_mod.sys, "stdin", _Stdin())
    assert main_mod._interactive_session_picker() is None
    assert called == []  # 非 TTY 连会话列表都不读


def test_startup_picker_env_kill_switch(monkeypatch):
    import xenon.main as main_mod

    monkeypatch.setenv("XENON_NO_SESSION_PICKER", "1")

    class _Stdin:
        def isatty(self):
            return True

    monkeypatch.setattr(main_mod.sys, "stdin", _Stdin())
    assert main_mod._interactive_session_picker() is None


def test_resume_replays_conversation(capsys):
    """恢复后把历史对话回放到终端：用户/助手消息可见。"""

    from xenon.repl.command_groups.session import _render_resumed_conversation

    _render_resumed_conversation(
        [
            {"role": "user", "content": "帮我调研海能达"},
            {"role": "assistant", "content": "结论如下…", "model_used": "deepseek-v4-pro"},
        ],
        2,
    )
    out = capsys.readouterr().out
    assert "已恢复的对话" in out
    assert "帮我调研海能达" in out
    assert "结论如下…" in out
    assert "deepseek-v4-pro" in out


def test_resume_replay_folds_long_content(capsys):
    from xenon.repl.command_groups.session import _render_resumed_conversation

    long = "x" * 5000
    _render_resumed_conversation([{"role": "assistant", "content": long}], 1)
    out = capsys.readouterr().out
    assert "内容过长，显示已折叠" in out
    assert "x" * 5000 not in out


def test_resume_replay_folds_old_messages(capsys):
    from xenon.repl.command_groups.session import _render_resumed_conversation

    history = [{"role": "user", "content": f"m{i}"} for i in range(15)]
    _render_resumed_conversation(history, 15)
    out = capsys.readouterr().out
    assert "较早的 3 条消息未回放" in out
    assert "m0" not in out
    assert "m14" in out
