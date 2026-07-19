"""B11 验收：StdioTransport.request 用 select + 墙钟超时替代阻塞 readline，
并将 ``max_retries`` 重命名为 ``max_lines``。

关键场景：MCP 子进程挂起（不响应）时，request 必须在 ``timeout`` 内抛出
RuntimeError，而不是被 ``readline()`` 永久阻塞。
"""
import inspect
import json
import sys
import time

import pytest

from xenon.mcp.transport import StdioTransport

PY = sys.executable


def _echo_server() -> str:
    """读取一行请求，回写同 id 的响应。"""
    return (
        "import sys,json\n"
        "req=sys.stdin.readline()\n"
        "d=json.loads(req)\n"
        "sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':d['id'],'result':{'ok':True}})+'\\n')\n"
        "sys.stdout.flush()\n"
    )


def _hanging_server() -> str:
    """读取请求后永久睡眠（模拟 MCP 服务挂起）。"""
    return "import sys,time\nsys.stdin.readline()\ntime.sleep(3600)\n"


def _notification_then_answer_server() -> str:
    """先发一条通知（无 id），再发匹配 id 的响应。"""
    return (
        "import sys,json\n"
        "sys.stdin.readline()\n"
        "sys.stdout.write(json.dumps({'jsonrpc':'2.0','method':'progress'})+'\\n')\n"
        "sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':1,'result':{'ok':True}})+'\\n')\n"
        "sys.stdout.flush()\n"
    )


def _spam_notifications_server(n: int = 100) -> str:
    """读取请求后狂发 n 条无 id 通知，永不回匹配 id。"""
    return (
        "import sys,json,time\n"
        "sys.stdin.readline()\n"
        "for i in range(%d):\n"
        "    sys.stdout.write(json.dumps({'jsonrpc':'2.0','method':'progress'})+'\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(3600)\n" % n
    )


class TestStdioTransportB11:
    def test_request_returns_matching_response(self):
        t = StdioTransport(PY, ["-c", _echo_server()])
        try:
            r = t.request("initialize", {"x": 1})
            assert r["id"] == 1
            assert r["result"] == {"ok": True}
        finally:
            t.close()

    def test_request_skips_notification_then_matches(self):
        t = StdioTransport(PY, ["-c", _notification_then_answer_server()])
        try:
            r = t.request("initialize")
            assert r["id"] == 1
            assert r["result"] == {"ok": True}
        finally:
            t.close()

    def test_request_times_out_on_hanging_server(self):
        """B11 核心：挂起的服务不得无限阻塞，须在 timeout 内抛错。"""
        t = StdioTransport(PY, ["-c", _hanging_server()])
        try:
            start = time.monotonic()
            with pytest.raises(RuntimeError, match="超时"):
                t.request("initialize", {}, timeout=0.5)
            elapsed = time.monotonic() - start
            assert elapsed < 2.0, f"request 未在超时内返回（elapsed={elapsed:.2f}s）"
        finally:
            t.close()

    def test_max_lines_cap_raises(self):
        """max_lines 限制读取行数；超限仍未匹配 id 时抛错。"""
        t = StdioTransport(PY, ["-c", _spam_notifications_server(100)])
        try:
            with pytest.raises(RuntimeError, match="读取 3 行"):
                t.request("initialize", {}, max_lines=3, timeout=5.0)
        finally:
            t.close()

    def test_param_renamed_to_max_lines(self):
        sig = inspect.signature(StdioTransport.request)
        assert "max_lines" in sig.parameters
        assert "timeout" in sig.parameters
        assert "max_retries" not in sig.parameters

    def test_max_retries_no_longer_accepted(self):
        t = StdioTransport(PY, ["-c", _echo_server()])
        try:
            with pytest.raises(TypeError):
                t.request("initialize", {}, max_retries=10)
        finally:
            t.close()

    def test_receive_times_out_on_silent_server(self):
        t = StdioTransport(PY, ["-c", "import time\ntime.sleep(3600)\n"])
        try:
            start = time.monotonic()
            with pytest.raises(RuntimeError, match="超时"):
                t.receive(timeout=0.5)
            elapsed = time.monotonic() - start
            assert elapsed < 2.0
        finally:
            t.close()
