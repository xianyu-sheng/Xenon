"""P3: Configuration Watchdog 测试。

覆盖:
- 平台支持/开关判断
- start/stop 生命周期(目录缺失降级、stop 幂等)
- debounce 合并 + 回调异常吞掉
- 端到端 inotify 触发(文件修改/无关文件忽略/atomic rename,需 Linux inotify)
"""
import os
import time

import pytest

from omniagent.repl.config_watcher import (
    ConfigWatcher,
    is_watch_enabled,
    is_watch_supported,
)

# 端到端用例需真实 inotify;非 Linux 跳过
_inotify = pytest.mark.skipif(
    not is_watch_supported(), reason="当前平台无 inotify")


# ── 平台 / 开关 ─────────────────────────────────────────

class TestPlatform:
    def test_is_watch_supported_returns_bool(self):
        assert isinstance(is_watch_supported(), bool)

    def test_is_watch_enabled_default_true(self, monkeypatch):
        monkeypatch.delenv("OMNIAGENT_CONFIG_WATCH", raising=False)
        assert is_watch_enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "False"])
    def test_is_watch_enabled_env_off(self, monkeypatch, val):
        monkeypatch.setenv("OMNIAGENT_CONFIG_WATCH", val)
        assert is_watch_enabled() is False


# ── start / stop ────────────────────────────────────────

class TestStartStop:
    def test_start_returns_false_when_dir_missing(self):
        """监听目录不存在时静默降级,返回 False 不抛错。"""
        w = ConfigWatcher("/nonexistent_dir_xyz/models.yaml", on_reload=lambda: None)
        assert w.start() is False

    @_inotify
    def test_start_stop_lifecycle(self, tmp_path):
        p = tmp_path / "models.yaml"
        p.write_text("models: {}")
        w = ConfigWatcher(p, on_reload=lambda: None, debounce_s=0.05)
        assert w.start() is True
        w.stop()
        # stop 后内部 fd 已释放
        assert w._fd < 0

    @_inotify
    def test_stop_is_idempotent(self, tmp_path):
        p = tmp_path / "models.yaml"
        p.write_text("models: {}")
        w = ConfigWatcher(p, on_reload=lambda: None, debounce_s=0.05)
        assert w.start() is True
        w.stop()
        w.stop()  # 再次 stop 不抛错


# ── debounce / 回调异常 ─────────────────────────────────

class TestDebounce:
    @_inotify
    def test_debounce_coalesces_burst(self, tmp_path):
        """连续多次事件应合并为单次回调。"""
        calls = {"n": 0}
        p = tmp_path / "models.yaml"
        p.write_text("models: {}")
        w = ConfigWatcher(p, on_reload=lambda: calls.__setitem__("n", calls["n"] + 1),
                          debounce_s=0.1)
        assert w.start()
        try:
            for _ in range(5):
                w._schedule_reload()  # 模拟连发 5 个事件
            time.sleep(0.4)  # 等 debounce 触发
        finally:
            w.stop()
        assert calls["n"] == 1

    def test_fire_swallows_callback_exception(self):
        """回调抛错时 _fire 不应传播异常。"""
        def boom():
            raise RuntimeError("boom")
        w = ConfigWatcher("/tmp/never_models.yaml", on_reload=boom, debounce_s=0.05)
        w._fire()  # 不抛错即通过


# ── 端到端 inotify ──────────────────────────────────────

class TestEndToEnd:
    @_inotify
    def test_file_modify_triggers_reload(self, tmp_path):
        calls = {"n": 0}
        p = tmp_path / "models.yaml"
        p.write_text("models: {}")
        w = ConfigWatcher(p, on_reload=lambda: calls.__setitem__("n", calls["n"] + 1),
                          debounce_s=0.1)
        assert w.start()
        try:
            time.sleep(0.1)  # 让 watcher 就绪
            p.write_text("models: {}\n# changed")  # 覆盖保存
            time.sleep(0.6)
        finally:
            w.stop()
        assert calls["n"] >= 1

    @_inotify
    def test_unrelated_file_ignored(self, tmp_path):
        calls = {"n": 0}
        p = tmp_path / "models.yaml"
        p.write_text("models: {}")
        other = tmp_path / "other.txt"
        other.write_text("x")
        w = ConfigWatcher(p, on_reload=lambda: calls.__setitem__("n", calls["n"] + 1),
                          debounce_s=0.1)
        assert w.start()
        try:
            time.sleep(0.1)
            other.write_text("y")  # 同目录无关文件变更
            time.sleep(0.4)
        finally:
            w.stop()
        assert calls["n"] == 0

    @_inotify
    def test_atomic_rename_triggers_reload(self, tmp_path):
        """vim 风格 atomic rename(写临时文件再 os.replace)应触发。"""
        calls = {"n": 0}
        p = tmp_path / "models.yaml"
        p.write_text("models: {}")
        w = ConfigWatcher(p, on_reload=lambda: calls.__setitem__("n", calls["n"] + 1),
                          debounce_s=0.1)
        assert w.start()
        try:
            time.sleep(0.1)
            tmp = tmp_path / ".models.yaml.tmp"
            tmp.write_text("models: {}\n# renamed")
            os.replace(tmp, p)  # atomic rename
            time.sleep(0.6)
        finally:
            w.stop()
        assert calls["n"] >= 1
