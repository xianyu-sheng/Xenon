"""
MCP Transport — JSON-RPC 2.0 传输层。

支持两种传输方式：
- stdio: 通过子进程的标准输入/输出通信
- SSE: 通过 HTTP Server-Sent Events 通信（含 `endpoint` 发现与 JSON-RPC id 关联）
"""

from __future__ import annotations

import json
import logging
import os
import queue
import select
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urljoin

import httpx

from xenon.utils.llm_client import _create_http_client

logger = logging.getLogger(__name__)


class MCPTransport:
    """MCP 传输基类。"""

    def _pump_stdout(self) -> None:
        """Windows-compatible stdout pump: one queue item per complete line."""
        try:
            stream = self._proc.stdout if self._proc else None
            if stream is None:
                return
            while True:
                line = stream.buffer.readline()
                if not line:
                    break
                self._line_queue.put(line)
        except Exception:
            logger.debug("MCP stdout pump stopped", exc_info=True)
        finally:
            self._line_queue.put(None)

    def send(self, message: dict[str, Any]) -> None:
        raise NotImplementedError

    def receive(self) -> dict[str, Any]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class StdioTransport(MCPTransport):
    """通过子进程 stdio 通信。"""

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.command = command
        self.args = args or []
        self.env = env
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._request_id = 0
        # stdout 行缓冲：跨多次 _readline_with_timeout 调用保留未消费字节，
        # 避免与 BufferedReader 内部缓冲冲突。
        self._read_buf = bytearray()
        self._line_queue: "queue.Queue[bytes | None]" = queue.Queue()
        self._reader_thread: threading.Thread | None = None
        self._start()

    def _start(self) -> None:
        """启动子进程。"""
        cmd = [self.command] + self.args
        import os

        child_env = dict(os.environ)
        if self.env:
            child_env.update(self.env)

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=child_env,
                text=True,
            )
            logger.info(f"MCP 子进程启动: {' '.join(cmd)} (PID: {self._proc.pid})")
            if os.name == "nt" and self._proc.stdout is not None:
                # Windows select() only supports sockets, not pipe handles.
                # Pump stdout in a daemon thread so request deadlines still
                # work and a hung child cannot block readline forever.
                self._reader_thread = threading.Thread(
                    target=self._pump_stdout,
                    name=f"mcp-stdout-{self._proc.pid}",
                    daemon=True,
                )
                self._reader_thread.start()
        except FileNotFoundError as e:
            raise RuntimeError(f"MCP 命令不存在: {self.command}") from e
        except Exception as e:
            raise RuntimeError(f"MCP 子进程启动失败: {e}") from e

    def send(self, message: dict[str, Any]) -> None:
        """发送 JSON-RPC 消息。"""
        if not self._proc or self._proc.poll() is not None:
            raise RuntimeError("MCP 子进程未运行")

        data = json.dumps(message) + "\n"
        with self._lock:
            try:
                self._proc.stdin.write(data)
                self._proc.stdin.flush()
            except Exception as e:
                raise RuntimeError(f"MCP 发送失败: {e}") from e

    def receive(self, timeout: float = 30.0) -> dict[str, Any]:
        """接收 JSON-RPC 消息（带墙钟超时，避免 readline 无限阻塞）。"""
        if not self._proc or self._proc.poll() is not None:
            raise RuntimeError("MCP 子进程未运行")

        with self._lock:
            deadline = time.monotonic() + timeout
            line = self._readline_with_timeout(deadline)
            if line is None:
                raise RuntimeError(f"MCP 接收超时：{timeout}s 内无输出")
            if line == "":
                stderr = self._read_stderr_safely()
                raise RuntimeError(f"MCP 子进程无输出（EOF）。stderr: {stderr[:500]}")
            try:
                return json.loads(line)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"MCP 响应解析失败: {e}") from e

    def _readline_with_timeout(self, deadline: float) -> str | None:
        """从 stdout 读取一行，带整体 deadline 墙钟超时。

        使用 select 等待数据可读，避免 ``readline()`` 在子进程挂起时无限阻塞
        （B11）。返回值约定：
          - 行字符串（含 ``\\n``，已 utf-8 解码）：读到完整一行；
          - ``""``：遇到 EOF 且缓冲区无残留；
          - ``None``：deadline 超时，未读到完整行。
        """
        if os.name == "nt":
            try:
                remaining = max(0.0, deadline - time.monotonic())
                line = self._line_queue.get(timeout=remaining)
            except queue.Empty:
                return None
            return "" if line is None else line.decode("utf-8", errors="replace")

        stream = self._proc.stdout
        try:
            fd = stream.fileno()
        except (AttributeError, OSError, ValueError):
            return None
        while True:
            nl = self._read_buf.find(b"\n")
            if nl >= 0:
                line = bytes(self._read_buf[: nl + 1])
                del self._read_buf[: nl + 1]
                return line.decode("utf-8", errors="replace")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                ready, _, _ = select.select([fd], [], [], remaining)
            except (OSError, ValueError):
                return None
            if not ready:
                return None
            try:
                chunk = stream.buffer.read1(8192)
            except Exception:
                return None
            if not chunk:
                # EOF：返回缓冲区残留（无换行）或空串
                if self._read_buf:
                    line = bytes(self._read_buf)
                    self._read_buf.clear()
                    return line.decode("utf-8", errors="replace")
                return ""
            self._read_buf += chunk

    def _read_stderr_safely(self) -> str:
        """非阻塞读取 stderr 当前可读内容（仅用于错误诊断）。"""
        if not self._proc or not self._proc.stderr:
            return ""
        if os.name == "nt":
            # Pipe handles are not selectable on Windows; avoid blocking the
            # error path.  The child stderr still flows to the pipe for later
            # diagnostics if the caller captures it separately.
            return ""
        try:
            fd = self._proc.stderr.fileno()
            chunks: list[bytes] = []
            while True:
                ready, _, _ = select.select([fd], [], [], 0)
                if not ready:
                    break
                data = self._proc.stderr.buffer.read1(4096)
                if not data:
                    break
                chunks.append(data)
            return b"".join(chunks).decode("utf-8", errors="replace")
        except Exception:
            return ""

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        max_lines: int = 50,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """发送请求并等待响应（原子操作，带墙钟超时）。

        - ``max_lines``：最多读取的行数上限（防止被无关通知/日志行无限消耗）。
        - ``timeout``：整体墙钟超时（秒）；超时抛 ``RuntimeError``。
          （B11：替代原先 ``max_retries`` 仅限行数、单行 readline 仍可无限阻塞的缺陷。）
        """
        with self._lock:
            if not self._proc or self._proc.poll() is not None:
                raise RuntimeError("MCP 子进程未运行")

            self._request_id += 1
            request_id = self._request_id
            message = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
            }
            if params:
                message["params"] = params

            data = json.dumps(message) + "\n"
            try:
                self._proc.stdin.write(data)
                self._proc.stdin.flush()
            except Exception as e:
                raise RuntimeError(f"MCP 发送失败: {e}") from e

            # 等待响应（匹配 id），带墙钟 deadline 与行数上限
            deadline = time.monotonic() + timeout
            for _ in range(max_lines):
                line = self._readline_with_timeout(deadline)
                if line is None:
                    raise RuntimeError(
                        f"MCP 请求超时：{timeout}s 内未收到 id={request_id} 的响应"
                    )
                if line == "":
                    stderr = self._read_stderr_safely()
                    raise RuntimeError(
                        f"MCP 子进程无输出（EOF）。stderr: {stderr[:500]}"
                    )
                try:
                    response = json.loads(line)
                except json.JSONDecodeError as e:
                    raise RuntimeError(f"MCP 响应解析失败: {e}") from e

                if response.get("id") == request_id:
                    return response
                if "id" not in response:
                    logger.debug(f"MCP 通知: {response.get('method', 'unknown')}")
                    continue

            raise RuntimeError(
                f"MCP 请求超时：读取 {max_lines} 行后仍未收到 id={request_id} 的响应"
            )

    def close(self) -> None:
        """关闭子进程。"""
        proc = self._proc
        if proc:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            # Terminate before closing stdout.  On Windows the stdout reader
            # thread may hold the pipe buffer lock; closing it first can block
            # forever when a child is hung (hanging-MCP timeout path).
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass
            if (
                self._reader_thread is not None
                and self._reader_thread is not threading.current_thread()
            ):
                self._reader_thread.join(timeout=2)
            for stream in (proc.stdout, proc.stderr):
                try:
                    if stream:
                        stream.close()
                except Exception:
                    pass
            self._proc = None

    def __del__(self) -> None:
        self.close()


class SSETransport(MCPTransport):
    """MCP over HTTP + Server-Sent Events (the 2024-11-05 HTTP transport).

    The handshake is two-legged and it is *not* optional:

    1. ``GET`` the server URL with ``Accept: text/event-stream`` and keep the
       stream open.  The server answers with one ``endpoint`` event whose data
       is the URL this session must ``POST`` to (it usually carries a session
       id); only after that event may a message be sent.
    2. Every request is ``POST``ed to that endpoint, and the *response* arrives
       on the stream — correlated by JSON-RPC ``id``, not by connection.

    The previous implementation posted to the SSE URL itself and read one
    ``data:`` line per call, so it never discovered the endpoint, could not
    tell two concurrent responses apart, and silently mis-read any server that
    answered with ``202 Accepted`` (#103 / #427).  This one keeps a reader
    thread that dispatches by ``id``, deduplicates replayed events, and fails
    closed with a named reason instead of hanging.
    """

    #: How many event ids are remembered for duplicate suppression.
    _SEEN_EVENT_IDS = 512

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        timeout: float = 30.0,
        connect_timeout: float | None = None,
        max_reconnects: int = 1,
    ) -> None:
        self.url = url
        self.headers = dict(headers or {})
        #: Wall clock budget for one request/reply round trip.
        self.timeout = timeout
        #: Budget for the stream handshake (must finish before we POST).
        self.connect_timeout = (
            connect_timeout if connect_timeout is not None else min(timeout, 15.0)
        )
        self.max_reconnects = max(0, int(max_reconnects))
        # The stream is idle by design between events, so a plain read timeout
        # would tear down a perfectly healthy connection (a real SSE server
        # sends nothing for seconds at a time).  Bound connect, not read, and
        # keep the per-call budget on the POST below.
        self._client = _create_http_client(timeout=None)
        self._request_id = 0
        self._lock = threading.Lock()
        self._pending: dict[int, "_PendingReply"] = {}
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._stream_thread: threading.Thread | None = None
        self._stream_error = ""
        self._post_url = ""
        self._session_headers: dict[str, str] = {}
        self._server_info: dict[str, Any] = {}
        self._seen_event_ids: dict[str, None] = {}
        self._duplicates = 0
        self._reconnects = 0
        self._last_error = ""
        #: Server-initiated notifications, bounded; observable, never answered.
        self.notifications: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=64)

    # ── public API ─────────────────────────────────────────────

    def send(self, message: dict[str, Any]) -> None:
        """POST a message that expects no reply (e.g. ``notifications/*``)."""

        self._ensure_ready()
        self._post(message)

    def receive(self) -> dict[str, Any]:
        """Read one dispatched message; kept for API compatibility.

        Normal callers should use :meth:`request`, which correlates by id.
        """

        self._ensure_ready()
        while True:
            try:
                return self.notifications.get(timeout=self.timeout)
            except queue.Empty as exc:
                raise RuntimeError(
                    "MCP SSE 接收超时：流上没有新的消息"
                    + (f"（最近一次错误：{self._last_error}）" if self._last_error else "")
                ) from exc

    def request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send a request and wait for the response with the same JSON-RPC id."""

        self._ensure_ready()
        with self._lock:
            self._request_id += 1
            request_id = self._request_id
            pending = _PendingReply()
            self._pending[request_id] = pending
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params:
            message["params"] = params

        try:
            inline = self._post(message)
        except Exception:
            with self._lock:
                self._pending.pop(request_id, None)
            raise

        # ``202 Accepted`` means "the answer comes on the stream"; a JSON body
        # is an older server answering inline.  Both are accepted, but only a
        # body carrying *our* id counts as this request's answer.
        if (
            isinstance(inline, Mapping)
            and inline.get("id") == request_id
            and ("result" in inline or "error" in inline)
        ):
            with self._lock:
                self._pending.pop(request_id, None)
            return dict(inline)

        if pending.event.wait(max(0.0, self.timeout)):
            if pending.error:
                raise RuntimeError(pending.error)
            assert pending.response is not None
            return pending.response
        with self._lock:
            self._pending.pop(request_id, None)
        detail = f"（最近一次错误：{self._last_error}）" if self._last_error else ""
        raise RuntimeError(
            f"MCP SSE 请求超时：{round(self.timeout, 1)}s 内未收到 "
            f"id={request_id} 的响应{detail}"
        )

    def status(self) -> dict[str, Any]:
        """Honest transport state for ``/mcp`` and diagnostics."""

        return {
            "transport": "sse",
            "url": self.url,
            "endpoint": self._post_url,
            "connected": self._ready.is_set() and not self._closed.is_set(),
            "reconnects": self._reconnects,
            "duplicates_dropped": self._duplicates,
            "session_headers": sorted(self._session_headers),
            "server": dict(self._server_info),
            "last_error": self._last_error,
        }

    def close(self) -> None:
        self._closed.set()
        self._ready.clear()
        self._fail_pending("MCP SSE 连接已关闭")
        # Close the socket *before* joining: the reader thread is parked on a
        # read with no timeout (an idle stream is normal), so only closing the
        # client wakes it.  Joining first would always burn the full timeout.
        self._client.close()
        thread = self._stream_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._stream_thread = None

    def __del__(self) -> None:  # pragma: no cover - GC timing
        try:
            self.close()
        except Exception:
            pass

    # ── handshake and stream ───────────────────────────────────

    def _ensure_ready(self) -> None:
        if self._closed.is_set():
            raise RuntimeError("MCP SSE 连接已关闭")
        if self._ready.is_set():
            return
        with self._lock:
            thread = self._stream_thread
            if thread is None or not thread.is_alive():
                self._stream_thread = threading.Thread(
                    target=self._stream_loop,
                    name="xenon-mcp-sse",
                    daemon=True,
                )
                self._stream_thread.start()
        if self._ready.wait(self.connect_timeout):
            return
        with self._lock:
            error = self._stream_error
        raise RuntimeError(
            "MCP SSE 握手失败："
            + (error or f"{self.connect_timeout:.1f}s 内未收到 endpoint 事件")
            + "（服务器必须先在 GET 流上发 `event: endpoint`，再接受 POST）"
        )

    def _stream_loop(self) -> None:
        attempts = 0
        while not self._closed.is_set():
            try:
                self._open_stream()
                error = "SSE 连接关闭"
            except Exception as exc:  # noqa: BLE001 — surface as state, not traceback
                error = f"SSE 连接失败：{exc}"
            if self._closed.is_set():
                return
            self._ready.clear()
            self._stream_error = error
            self._last_error = error
            # A dropped stream invalidates every in-flight request: the answer
            # for those ids lived on the old connection and is never coming.
            # Failing them now is the difference between "error" and "hang".
            self._fail_pending(
                f"{error}；请求已随连接中断，不自动重试（侧效应未知）"
            )
            if attempts >= self.max_reconnects:
                return
            attempts += 1
            self._reconnects += 1
            if self._closed.wait(min(0.2 * attempts, 2.0)):
                return

    def _open_stream(self) -> None:
        headers = {
            **self.headers,
            "Accept": "text/event-stream",
            "Cache-Control": "no-store",
        }
        stream_timeout = httpx.Timeout(self.connect_timeout, read=None)
        with self._client.stream(
            "GET", self.url, headers=headers, timeout=stream_timeout
        ) as resp:
            resp.raise_for_status()
            session = resp.headers.get("mcp-session-id") or ""
            if session:
                self._session_headers["mcp-session-id"] = session
            event_name = ""
            event_id = ""
            data_lines: list[str] = []
            for raw in resp.iter_lines():
                if self._closed.is_set():
                    return
                line = raw.rstrip("\r")
                if not line:
                    if data_lines:
                        self._dispatch(event_name, "\n".join(data_lines), event_id)
                    event_name, event_id, data_lines = "", "", []
                    continue
                if line.startswith(":"):
                    continue  # keep-alive comment
                field, _, value = line.partition(":")
                if value.startswith(" "):
                    value = value[1:]
                if field == "event":
                    event_name = value
                elif field == "data":
                    data_lines.append(value)
                elif field == "id":
                    event_id = value
            raise RuntimeError("SSE 流结束")

    def _dispatch(self, event_name: str, data: str, event_id: str) -> None:
        if event_id:
            if event_id in self._seen_event_ids:
                # A reconnect may replay the tail of the stream; replaying a
                # response is harmless once, but resolving an id twice is how
                # two different calls end up sharing one answer.
                self._duplicates += 1
                return
            if len(self._seen_event_ids) >= self._SEEN_EVENT_IDS:
                self._seen_event_ids.pop(next(iter(self._seen_event_ids)), None)
            self._seen_event_ids[event_id] = None
        if event_name == "endpoint":
            declared = data.strip()
            if not declared:
                # ``urljoin`` would happily turn "" into the SSE URL, i.e. POST
                # to the stream address — the exact guess this transport exists
                # to avoid.  An empty endpoint is a broken server, and says so.
                self._last_error = "endpoint 事件为空"
                return
            self._post_url = urljoin(self.url, declared)
            self._ready.set()
            return
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            self._last_error = f"SSE 数据不是 JSON：{data[:120]}"
            logger.debug("MCP SSE 非 JSON 数据被忽略: %s", data[:200])
            return
        if not isinstance(payload, dict):
            return
        if "id" in payload and ("result" in payload or "error" in payload):
            self._resolve(payload)
            return
        if "method" in payload:
            self._queue_notification(payload)
            return
        if "serverInfo" in payload or "capabilities" in payload:
            # A server announcement may wrap the info (``{"serverInfo": {...}}``)
            # or be the info itself; store the meaningful half either way.
            info = payload.get("serverInfo")
            self._server_info = (
                dict(info) if isinstance(info, Mapping) else dict(payload)
            )
            return
        self._last_error = f"无法归类的 SSE 消息：{str(payload)[:120]}"

    def _resolve(self, payload: Mapping[str, Any]) -> None:
        request_id = payload.get("id")
        with self._lock:
            # Pop, do not peek: the entry leaves the table on first resolve, so a
            # server replaying the same id (or a stale answer landing late) can
            # never overwrite the answer the caller already received.
            pending = self._pending.pop(request_id, None)
        if pending is None:
            logger.debug("MCP SSE 收到无主/重复的响应: id=%s", request_id)
            self._duplicates += 1
            return
        pending.response = dict(payload)
        pending.event.set()

    def _queue_notification(self, payload: dict[str, Any]) -> None:
        try:
            self.notifications.put_nowait(payload)
        except queue.Full:
            # Notifications are advisory; dropping the oldest is better than
            # blocking the reader thread that every response depends on.
            try:
                self.notifications.get_nowait()
                self.notifications.put_nowait(payload)
            except queue.Empty:  # pragma: no cover - race with another reader
                pass

    def _fail_pending(self, reason: str) -> None:
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            if item.event.is_set():
                continue
            item.error = reason
            item.event.set()

    def _post(self, message: Mapping[str, Any]) -> Any:
        if not self._post_url:
            raise RuntimeError(
                "MCP SSE 尚未获得 endpoint：服务器未发送 `event: endpoint`"
            )
        headers = {
            **self.headers,
            **self._session_headers,
            "Content-Type": "application/json",
        }
        try:
            resp = self._client.post(
                self._post_url,
                json=dict(message),
                headers=headers,
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 — one error type for callers
            self._last_error = f"MCP SSE 发送失败：{exc}"
            raise RuntimeError(self._last_error) from exc
        if resp.status_code == 202 or not resp.content:
            return None
        try:
            return resp.json()
        except Exception:  # noqa: BLE001 — a non-JSON body is not a reply
            return None


@dataclass
class _PendingReply:
    """One in-flight request waiting for its correlated response."""

    event: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None
    error: str = ""
