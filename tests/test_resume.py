"""v0.4.0 Step 14: Session resume tests."""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import pytest
from xenon.repl.session import (
    auto_save, get_auto_session, cleanup_expired_sessions,
    get_session_age, SESSIONS_DIR, AUTO_SESSION_NAME, SESSION_TTL_DAYS,
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
