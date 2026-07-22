"""v0.4.0 Step 14: Session resume tests."""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from xenon.repl.session import (
    AUTO_SESSION_NAME,
    SESSION_TTL_DAYS,
    auto_save,
    cleanup_expired_sessions,
    get_auto_session,
    get_session_age,
    load_session,
    save_session,
)


class TestAutoSave:
    def test_auto_save_creates_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 覆盖 SESSIONS_DIR
            import xenon.repl.session as mod
            old_dir = mod.SESSIONS_DIR
            mod.SESSIONS_DIR = Path(tmp)
            try:
                result = auto_save(
                    history=[{"role": "user", "content": "hello"}],
                    context_store={"mode": "direct"},
                    model_config={"pro": {"model_id": "a/pro", "weight": 5.0}},
                )
                assert result is not None
                assert result.exists()
                data = json.loads(result.read_text())
                assert data["name"] == AUTO_SESSION_NAME
                assert len(data["history"]) == 1
            finally:
                mod.SESSIONS_DIR = old_dir

    def test_get_auto_session_returns_none_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            import xenon.repl.session as mod
            old_dir = mod.SESSIONS_DIR
            mod.SESSIONS_DIR = Path(tmp)
            try:
                assert get_auto_session() is None
            finally:
                mod.SESSIONS_DIR = old_dir

    def test_auto_save_never_persists_provider_credentials(self, tmp_path, monkeypatch):
        import xenon.repl.session as mod

        monkeypatch.setattr(mod, "SESSIONS_DIR", tmp_path)
        result = auto_save(
            history=[{"role": "user", "content": "keep this text unchanged"}],
            context_store={"provider": {"access_token": "context-secret"}},
            model_config={
                "pro": {
                    "model_id": "deepseek/pro",
                    "weight": 5.0,
                    "api_key": "provider-secret",
                    "base_url": "https://example.invalid/v1",
                },
            },
            extra={"mcp": {"authorization": "Bearer secret"}},
        )

        assert result is not None
        raw = result.read_text(encoding="utf-8")
        data = json.loads(raw)
        assert data["version"] == "2.1"
        assert "provider-secret" not in raw
        assert "context-secret" not in raw
        assert "Bearer secret" not in raw
        assert "api_key" not in data["model_config"]["pro"]
        assert data["model_config"]["pro"]["model_id"] == "deepseek/pro"
        assert data["history"][0]["content"] == "keep this text unchanged"

    def test_manual_save_also_removes_credentials(self, tmp_path, monkeypatch):
        import xenon.repl.session as mod

        monkeypatch.setattr(mod, "SESSIONS_DIR", tmp_path)
        path = save_session(
            "safe",
            history=[],
            context_store={},
            model_config={"models": {"pro": {"api_key": "must-not-land"}}},
        )

        assert "must-not-land" not in path.read_text(encoding="utf-8")
        assert load_session("safe")["model_config"] == {"models": {"pro": {}}}

    def test_loading_legacy_session_scrubs_file_in_place(self, tmp_path, monkeypatch):
        import xenon.repl.session as mod

        monkeypatch.setattr(mod, "SESSIONS_DIR", tmp_path)
        legacy = tmp_path / "legacy.json"
        legacy.write_text(
            json.dumps({
                "version": "2.0",
                "name": "legacy",
                "history": [{"role": "user", "content": "hello"}],
                "model_config": {
                    "pro": {"model_id": "a/pro", "api_key": "legacy-secret"},
                },
            }),
            encoding="utf-8",
        )

        data = load_session("legacy")

        assert data["model_config"]["pro"] == {"model_id": "a/pro"}
        rewritten = legacy.read_text(encoding="utf-8")
        assert "legacy-secret" not in rewritten
        assert "api_key" not in rewritten
        assert legacy.stat().st_mode & 0o777 == 0o600


class TestSessionExpiry:
    def test_expired_session_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            import xenon.repl.session as mod
            old_dir = mod.SESSIONS_DIR
            mod.SESSIONS_DIR = Path(tmp)
            try:
                # 保存一个"过期"的会话
                filepath = Path(tmp) / f"{AUTO_SESSION_NAME}.json"
                old_data = {
                    "saved_at_ts": time.time() - (SESSION_TTL_DAYS + 1) * 86400,
                    "history": [],
                    "context": {},
                    "model_config": {},
                }
                filepath.write_text(json.dumps(old_data))

                assert get_auto_session() is None  # 应已过期
            finally:
                mod.SESSIONS_DIR = old_dir


class TestGetSessionAge:
    def test_recent_session(self):
        data = {"saved_at_ts": time.time() - 60}
        assert "分钟前" in get_session_age(data) or "刚刚" in get_session_age(data)

    def test_old_session(self):
        data = {"saved_at_ts": time.time() - 3600 * 5}
        assert "小时前" in get_session_age(data)


class TestCleanupExpired:
    def test_cleanup_removes_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            import xenon.repl.session as mod
            old_dir = mod.SESSIONS_DIR
            mod.SESSIONS_DIR = Path(tmp)
            try:
                # 创建过期会话
                old_file = Path(tmp) / f"{AUTO_SESSION_NAME}.json"
                old_file.write_text(json.dumps({
                    "saved_at_ts": time.time() - 10 * 86400,
                    "history": [],
                }))
                # 创建新鲜会话
                fresh_file = Path(tmp) / "_auto_fresh.json"
                fresh_file.write_text(json.dumps({
                    "saved_at_ts": time.time() - 60,
                    "history": [],
                }))

                deleted = cleanup_expired_sessions()
                assert deleted >= 1
                assert not old_file.exists()  # 应被删除
                assert fresh_file.exists()    # 应保留
            finally:
                mod.SESSIONS_DIR = old_dir
