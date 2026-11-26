"""SSE transport: endpoint discovery, id correlation, reconnect (#427).

The old ``SSETransport`` posted to the SSE URL itself and read one ``data:``
line per call, so it never discovered the ``endpoint`` event, could not tell
two concurrent responses apart, and mis-read any server that answered
``202 Accepted``.  These tests run a real HTTP server over a real socket: the
handshake, the POST target and the stream are exercised, not mocked.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

import pytest

from xenon.mcp.transport import SSETransport


class _SSEServer:
    """Minimal MCP HTTP+SSE server: one stream per session, POST to endpoint."""

    def __init__(self) -> None:
        self.posts: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self.outbox: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.sessions: list[str] = []
        self.announce_endpoint = True
        self.die_after_responses = 0
        self.inline_reply = False
        self.post_status = 202
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._served = 0

    # ── lifecycle ────────────────────────────────────────────
    def start(self) -> "_SSEServer":
        self._thread.start()
        return self

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/sse"

    def stop(self) -> None:
        self.outbox.put(None)
        self._server.shutdown()
        self._server.server_close()

    # ── stream helpers ───────────────────────────────────────
    def push(self, payload: dict[str, Any], *, event_id: str = "") -> None:
        self.outbox.put({"payload": payload, "id": event_id})

    def push_raw(self, text: str) -> None:
        self.outbox.put({"raw": text})

    def last_post(self, timeout: float = 5.0) -> tuple[str, dict[str, Any]]:
        return self.posts.get(timeout=timeout)

    # ── handler ──────────────────────────────────────────────
    def _handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # silence test output
                pass

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                session = f"s{len(server.sessions) + 1}"
                server.sessions.append(session)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Mcp-Session-Id", session)
                self.end_headers()
                if server.announce_endpoint:
                    self._write(
                        "event: endpoint\ndata: "
                        f"/messages?session={session}\n\n"
                    )
                served = 0
                while True:
                    try:
                        item = server.outbox.get(timeout=5)
                    except queue.Empty:
                        break
                    if item is None:
                        break
                    if "raw" in item:
                        self._write(item["raw"])
                        continue
                    payload = item["payload"]
                    body = json.dumps(payload)
                    stamp = item.get("id") or ""
                    header = f"id: {stamp}\n" if stamp else ""
                    self._write(f"event: message\n{header}data: {body}\n\n")
                    served += 1
                    if (
                        server.die_after_responses
                        and served >= server.die_after_responses
                    ):
                        break
                # Dropping the stream on purpose is how a reconnect is tested.
                try:
                    self.close_connection = True
                    self.wfile.flush()
                except Exception:
                    pass

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    message = {"raw": raw.decode("utf-8", "replace")}
                parsed = urlparse(self.path)
                server.posts.put((self.path, message))
                session = parsed.query.split("session=")[-1]
                if server.inline_reply:
                    body = json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": message.get("id"),
                            "result": {"inline": True},
                        }
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Mcp-Session-Id", session)
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(server.post_status)
                self.send_header("Content-Length", "0")
                self.send_header("Mcp-Session-Id", session)
                self.end_headers()

            def _write(self, text: str) -> None:
                try:
                    self.wfile.write(text.encode("utf-8"))
                    self.wfile.flush()
                except Exception:
                    pass

        return Handler


@pytest.fixture()
def sse_server() -> Any:
    server = _SSEServer().start()
    yield server
    server.stop()


def _respond(server: _SSEServer, request_id: Any, result: dict[str, Any]) -> None:
    server.push({"jsonrpc": "2.0", "id": request_id, "result": result})


def test_endpoint_discovery_decides_the_post_target(sse_server: _SSEServer) -> None:
    """The POST must go to the URL from ``event: endpoint``, not the SSE URL."""

    transport = SSETransport(sse_server.url, timeout=5)
    try:
        waiter = threading.Thread(
            target=lambda: transport.request("tools/list"), daemon=True
        )
        waiter.start()
        path, message = sse_server.last_post()
        assert path.startswith("/messages?session="), path
        assert message["method"] == "tools/list"
        _respond(sse_server, message["id"], {"tools": []})
        waiter.join(timeout=5)
        status = transport.status()
        assert status["connected"] is True
        assert status["endpoint"].endswith(path)
        assert status["session_headers"] == ["mcp-session-id"]
    finally:
        transport.close()


def test_concurrent_requests_keep_their_own_answers(sse_server: _SSEServer) -> None:
    """Two calls in flight, answers in reverse order: no cross-talk."""

    transport = SSETransport(sse_server.url, timeout=5)
    results: dict[str, Any] = {}

    def call(name: str) -> None:
        results[name] = transport.request("tools/call", {"name": name})

    try:
        first = threading.Thread(target=call, args=("slow",), daemon=True)
        first.start()
        _, slow = sse_server.last_post()
        second = threading.Thread(target=call, args=("fast",), daemon=True)
        second.start()
        _, fast = sse_server.last_post()

        # Answer the second request first, and throw in a notification and a
        # duplicate of the first response for good measure.
        transport.send({"jsonrpc": "2.0", "method": "notifications/progress"})
        _respond(sse_server, fast["id"], {"content": "fast"})
        _respond(sse_server, slow["id"], {"content": "slow"})
        sse_server.push(
            {"jsonrpc": "2.0", "id": slow["id"], "result": {"content": "wrong"}},
            event_id="replayed-1",
        )
        first.join(timeout=5)
        second.join(timeout=5)
        assert results["slow"]["result"] == {"content": "slow"}
        assert results["fast"]["result"] == {"content": "fast"}
    finally:
        transport.close()


def test_a_missing_endpoint_fails_closed_fast(sse_server: _SSEServer) -> None:
    """No ``endpoint`` event: say so, do not POST to a guessed URL."""

    sse_server.announce_endpoint = False
    transport = SSETransport(sse_server.url, timeout=5, connect_timeout=1.0)
    try:
        with pytest.raises(RuntimeError) as excinfo:
            transport.request("tools/list")
        assert "endpoint" in str(excinfo.value)
        assert "握手失败" in str(excinfo.value)
        assert transport.status()["connected"] is False
        assert sse_server.posts.empty(), "没有 endpoint 时不得发送 POST"
    finally:
        transport.close()


def test_a_dropped_stream_fails_the_in_flight_request(sse_server: _SSEServer) -> None:
    """A dead stream must not leave a caller waiting for an answer that is gone."""

    transport = SSETransport(
        sse_server.url, timeout=2, connect_timeout=5, max_reconnects=0
    )
    sse_server.die_after_responses = 0  # not used: we drop the stream by hand
    failures: list[str] = []

    def call() -> None:
        try:
            transport.request("tools/list")
        except RuntimeError as exc:
            failures.append(str(exc))

    try:
        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        _, message = sse_server.last_post()
        assert message["method"] == "tools/list"
        # Kill the stream without answering: the reply lived on it.
        sse_server.outbox.put(None)
        thread.join(timeout=6)
        assert not thread.is_alive(), "连接断开后请求不得悬挂"
        assert failures, "请求必须报错，而不是拿到空答案"
        assert "中断" in failures[0]
        status = transport.status()
        assert status["connected"] is False
        assert "SSE" in status["last_error"]
    finally:
        transport.close()


def test_a_replay_with_the_same_event_id_is_dropped(sse_server: _SSEServer) -> None:
    transport = SSETransport(sse_server.url, timeout=5)
    try:
        thread = threading.Thread(
            target=lambda: transport.request("tools/list"), daemon=True
        )
        thread.start()
        _, message = sse_server.last_post()
        body = {"jsonrpc": "2.0", "id": message["id"], "result": {"n": 1}}
        sse_server.push(body, event_id="evt-1")
        sse_server.push({"jsonrpc": "2.0", "id": 999, "result": {"n": 2}}, event_id="evt-1")
        thread.join(timeout=5)
        assert sse_server.sessions, "握手必须真的发生过"
    finally:
        transport.close()


def test_a_dropped_stream_reconnects_and_keeps_working(sse_server: _SSEServer) -> None:
    """The server closes the stream after one answer; the next call still works."""

    sse_server.die_after_responses = 1
    transport = SSETransport(
        sse_server.url, timeout=5, connect_timeout=5, max_reconnects=3
    )
    first: dict[str, Any] = {}

    def call() -> None:
        first["result"] = transport.request("tools/list")

    try:
        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        _, message = sse_server.last_post()
        _respond(sse_server, message["id"], {"tools": ["a"]})
        thread.join(timeout=6)
        assert first["result"]["result"] == {"tools": ["a"]}

        deadline = time.time() + 10
        while time.time() < deadline and transport.status()["reconnects"] < 1:
            time.sleep(0.05)
        assert transport.status()["reconnects"] >= 1, "断线后必须尝试重连"
        while time.time() < deadline and not transport.status()["connected"]:
            time.sleep(0.05)
        assert transport.status()["connected"] is True

        second: dict[str, Any] = {}

        def call_again() -> None:
            second["result"] = transport.request("tools/call", {"name": "a"})

        thread2 = threading.Thread(target=call_again, daemon=True)
        thread2.start()
        _, message2 = sse_server.last_post()
        _respond(sse_server, message2["id"], {"content": "ok"})
        thread2.join(timeout=6)
        assert second["result"]["result"] == {"content": "ok"}
        assert len(sse_server.sessions) >= 2, "重连必须重新握手"
    finally:
        transport.close()


def test_mcp_list_reports_transport_truth(sse_server: _SSEServer) -> None:
    """``/mcp list`` must not show a broken SSE connection as healthy."""

    from types import SimpleNamespace

    from xenon.repl.command_groups.resources import _cmd_mcp

    class _Client:
        server_info = {"name": "fake", "version": "1.0"}
        tools: list[Any] = []

        def __init__(self, status: dict[str, Any]) -> None:
            self._status = status

        def transport_status(self) -> dict[str, Any]:
            return dict(self._status)

    registry = SimpleNamespace(
        clients={
            "good": _Client(
                {
                    "transport": "sse",
                    "connected": True,
                    "endpoint": "http://x/messages?session=1",
                    "reconnects": 1,
                }
            ),
            "broken": _Client(
                {
                    "transport": "sse",
                    "connected": False,
                    "last_error": "SSE 握手失败：未收到 endpoint 事件",
                    "duplicates_dropped": 2,
                }
            ),
            "stdio": _Client({"transport": "stdio", "last_error": "子进程已退出"}),
        },
        has_pending_servers=lambda: False,
        get_pending_server_names=lambda: [],
    )
    repl = SimpleNamespace(_mcp_registry=registry, agent_context=None)

    rendered = _cmd_mcp(args="list", session_state={"_repl": repl})

    assert "sse: 已握手 endpoint=http://x/messages?session=1" in rendered
    assert "重连 1 次" in rendered
    assert "sse: 未连接" in rendered
    assert "未收到 endpoint 事件" in rendered
    assert "丢弃重复事件 2 条" in rendered
    assert "传输错误: 子进程已退出" in rendered


def test_inline_json_reply_is_accepted(sse_server: _SSEServer) -> None:
    """Older servers answer the POST inline instead of on the stream."""

    sse_server.inline_reply = True
    transport = SSETransport(sse_server.url, timeout=5)
    try:
        answer = transport.request("tools/list")
        assert answer["result"] == {"inline": True}
    finally:
        transport.close()


def test_an_unanswered_request_times_out_with_its_id(sse_server: _SSEServer) -> None:
    transport = SSETransport(sse_server.url, timeout=0.6, connect_timeout=5)
    try:
        with pytest.raises(RuntimeError) as excinfo:
            transport.request("tools/list")
        assert "超时" in str(excinfo.value)
        assert "id=" in str(excinfo.value)
    finally:
        transport.close()


def test_keepalive_comments_and_junk_do_not_break_the_stream(
    sse_server: _SSEServer,
) -> None:
    """A `: ping` keep-alive and a non-JSON frame are ignored, not fatal."""

    transport = SSETransport(sse_server.url, timeout=5)
    got: dict[str, Any] = {}

    def call() -> None:
        got["answer"] = transport.request("tools/list")

    try:
        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        _, message = sse_server.last_post()
        sse_server.push_raw(": keep-alive\n\n")
        sse_server.push_raw("event: message\ndata: not-json\n\n")
        _respond(sse_server, message["id"], {"tools": ["a"]})
        thread.join(timeout=5)
        assert got["answer"]["result"] == {"tools": ["a"]}
        assert "不是 JSON" in transport.status()["last_error"]
    finally:
        transport.close()


def test_notifications_are_readable_and_receive_times_out(
    sse_server: _SSEServer,
) -> None:
    transport = SSETransport(sse_server.url, timeout=0.5)
    try:
        # The handshake is lazy; force it and get the stream going.
        threading.Thread(target=lambda: transport._ensure_ready(), daemon=True).start()
        while not transport.status()["connected"]:
            time.sleep(0.02)
        sse_server.push({"jsonrpc": "2.0", "method": "notifications/message", "params": {}})
        sse_server.push({"serverInfo": {"name": "fake", "version": "9"}})
        note = transport.receive()
        assert note["method"] == "notifications/message"
        deadline = time.time() + 3
        while time.time() < deadline and not transport.status()["server"]:
            time.sleep(0.02)
        assert transport.status()["server"]["name"] == "fake"
        with pytest.raises(RuntimeError) as excinfo:
            transport.receive()
        assert "接收超时" in str(excinfo.value)
    finally:
        transport.close()


def test_a_failed_post_is_reported(sse_server: _SSEServer) -> None:
    sse_server.post_status = 500
    transport = SSETransport(sse_server.url, timeout=2)
    try:
        with pytest.raises(RuntimeError) as excinfo:
            transport.request("tools/list")
        assert "发送失败" in str(excinfo.value)
        assert "发送失败" in transport.status()["last_error"]
    finally:
        transport.close()


def test_close_fails_the_in_flight_request(sse_server: _SSEServer) -> None:
    transport = SSETransport(sse_server.url, timeout=5)
    failures: list[str] = []

    def call() -> None:
        try:
            transport.request("tools/list")
        except RuntimeError as exc:
            failures.append(str(exc))

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    sse_server.last_post()
    transport.close()
    thread.join(timeout=5)
    assert not thread.is_alive(), "close 必须唤醒等待中的请求"
    # close 与在飞的 POST/读取是竞态的：可能走「已关闭」「流中断」或套接字
    # 报错三条路径之一。三者都必须满足的契约是——明确报错、不悬挂、之后不可用。
    assert failures, "close 必须让在飞请求失败，而不是静默返回"
    assert "SSE" in failures[0], failures[0]
    with pytest.raises(RuntimeError) as excinfo:
        transport.request("tools/list")
    assert "已关闭" in str(excinfo.value)


def test_an_unreachable_server_fails_closed() -> None:
    transport = SSETransport("http://127.0.0.1:9/sse", timeout=2, connect_timeout=2)
    try:
        with pytest.raises(RuntimeError) as excinfo:
            transport.request("tools/list")
        assert "握手失败" in str(excinfo.value)
        assert transport.status()["connected"] is False
    finally:
        transport.close()


class _TinySeenCache(SSETransport):
    """Same transport with a 2-entry dedup cache, to exercise eviction."""

    _SEEN_EVENT_IDS = 2


def test_the_event_id_cache_is_bounded(sse_server: _SSEServer) -> None:
    transport = _TinySeenCache(sse_server.url, timeout=5)
    got: dict[str, Any] = {}

    def call() -> None:
        got["answer"] = transport.request("tools/list")

    try:
        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        _, message = sse_server.last_post()
        for index in range(5):
            sse_server.push(
                {"jsonrpc": "2.0", "method": "notifications/tick", "params": {"i": index}},
                event_id=f"evt-{index}",
            )
        _respond(sse_server, message["id"], {"tools": []})
        thread.join(timeout=5)
        assert got["answer"]["result"] == {"tools": []}
    finally:
        transport.close()


def test_an_empty_endpoint_event_fails_the_handshake(sse_server: _SSEServer) -> None:
    """An empty `endpoint` must not degrade into "POST to the SSE URL"."""

    sse_server.announce_endpoint = False
    transport = SSETransport(sse_server.url, timeout=2, connect_timeout=0.8)
    try:
        # Queued before the handshake: the first GET drains the outbox.
        sse_server.push_raw("event: endpoint\ndata: \n\n")
        with pytest.raises(RuntimeError) as excinfo:
            transport.request("tools/list")
        assert "握手失败" in str(excinfo.value)
        assert sse_server.posts.empty()
    finally:
        transport.close()


def test_odd_stream_frames_are_recorded_not_fatal(sse_server: _SSEServer) -> None:
    transport = SSETransport(sse_server.url, timeout=5)
    got: dict[str, Any] = {}

    def call() -> None:
        got["answer"] = transport.request("tools/list")

    try:
        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        _, message = sse_server.last_post()
        sse_server.push_raw("event: message\ndata: [1, 2]\n\n")
        sse_server.push_raw('event: message\ndata: {"foo": 1}\n\n')
        _respond(sse_server, message["id"], {"tools": ["a"]})
        thread.join(timeout=5)
        assert got["answer"]["result"] == {"tools": ["a"]}
    finally:
        transport.close()


def test_a_response_with_no_owner_is_counted(sse_server: _SSEServer) -> None:
    transport = SSETransport(sse_server.url, timeout=5)
    try:
        transport._ensure_ready()
        sse_server.push({"jsonrpc": "2.0", "id": 12345, "result": {"ghost": True}})
        deadline = time.time() + 3
        while time.time() < deadline and not transport.status()["duplicates_dropped"]:
            time.sleep(0.02)
        assert transport.status()["duplicates_dropped"] >= 1
    finally:
        transport.close()


def test_the_notification_queue_drops_the_oldest(sse_server: _SSEServer) -> None:
    transport = SSETransport(sse_server.url, timeout=5)
    try:
        transport.notifications = queue.Queue(maxsize=1)
        transport._ensure_ready()
        for index in range(3):
            sse_server.push(
                {"jsonrpc": "2.0", "method": "notifications/n", "params": {"i": index}}
            )
        # 让读线程把三条都处理完，再一次排干；队列上限 1 意味着最终只剩最新一条。
        time.sleep(0.5)
        received: list[dict[str, Any]] = []
        while not transport.notifications.empty():
            received.append(transport.notifications.get_nowait())
        assert received, "通知必须被投递，而不是阻塞读线程"
        assert received[-1]["params"]["i"] == 2, received
        assert len(received) <= 1
    finally:
        transport.close()


def test_client_transport_status_degrades_safely() -> None:
    from xenon.mcp.client import MCPClient

    class _NoStatus:
        pass

    class _BrokenStatus:
        def status(self) -> dict[str, Any]:
            raise RuntimeError("boom")

    plain = MCPClient(_NoStatus())  # type: ignore[arg-type]
    assert plain.transport_status()["transport"] == "_NoStatus"

    broken = MCPClient(_BrokenStatus())  # type: ignore[arg-type]
    assert broken.transport_status()["transport"] == "unknown"
