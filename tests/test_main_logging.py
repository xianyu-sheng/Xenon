"""CLI 日志视觉层次回归测试。"""

from __future__ import annotations

import logging

from xenon.main import _DimNetworkFormatter


def test_network_log_is_dimmed_on_tty(monkeypatch):
    monkeypatch.setattr("xenon.main.sys.stderr.isatty", lambda: True)
    formatter = _DimNetworkFormatter("%(name)s: %(message)s")
    record = logging.LogRecord("httpx", logging.INFO, "", 0, "request ok", (), None)
    assert formatter.format(record) == "\033[2mhttpx: request ok\033[0m"


def test_network_log_has_no_ansi_when_redirected(monkeypatch):
    monkeypatch.setattr("xenon.main.sys.stderr.isatty", lambda: False)
    formatter = _DimNetworkFormatter("%(name)s: %(message)s")
    record = logging.LogRecord("httpx", logging.INFO, "", 0, "request ok", (), None)
    assert formatter.format(record) == "httpx: request ok"


def test_application_log_keeps_normal_brightness(monkeypatch):
    monkeypatch.setattr("xenon.main.sys.stderr.isatty", lambda: True)
    formatter = _DimNetworkFormatter("%(name)s: %(message)s")
    record = logging.LogRecord("xenon.repl", logging.INFO, "", 0, "ready", (), None)
    assert formatter.format(record) == "xenon.repl: ready"
