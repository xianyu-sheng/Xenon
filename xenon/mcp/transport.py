"""
MCP Transport — JSON-RPC 2.0 传输层。

支持两种传输方式：
- stdio: 通过子进程的标准输入/输出通信
- SSE: 通过 HTTP Server-Sent Events 通信
"""

from __future__ import annotations

import json
import logging
import select
import subprocess
import sys
import threading
import time
from typing import Any

import httpx

from xenon.utils.llm_client import _create_http_client

logger = logging.getLogger(__name__)


class MCPTransport:
    """MCP 传输基类。"""

    def send(self, message: dict[str, Any]) -> None:
        raise NotImplementedError

    def receive(self) -> dict[str, Any]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class StdioTransport(MCPTransport):
    """通过子进程 stdio 通信。"""

    def __init__(self, command: str, args: list[str] | None = None, env: dict[str, str] | None = None) -> None:
        self.command = command
        self.args = args or []
        self.env = env
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._request_id = 0
        # stdout 行缓冲：跨多次 _readline_with_timeout 调用保留未消费字节，
        # 避免与 BufferedReader 内部缓冲冲突。
        self._read_buf = bytearray()
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
        except FileNotFoundError:
            raise RuntimeError(f"MCP 命令不存在: {self.command}")
        except Exception as e:
            raise RuntimeError(f"MCP 子进程启动失败: {e}")

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
                raise RuntimeError(f"MCP 发送失败: {e}")

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
                raise RuntimeError(f"MCP 响应解析失败: {e}")

    def _readline_with_timeout(self, deadline: float) -> str | None:
        """从 stdout 读取一行，带整体 deadline 墙钟超时。

        使用 select 等待数据可读，避免 ``readline()`` 在子进程挂起时无限阻塞
        （B11）。返回值约定：
          - 行字符串（含 ``\\n``，已 utf-8 解码）：读到完整一行；
          - ``""``：遇到 EOF 且缓冲区无残留；
          - ``None``：deadline 超时，未读到完整行。
        """
        stream = self._proc.stdout
        try:
            fd = stream.fileno()
        except (AttributeError, OSError, ValueError):
            return None
        while True:
            nl = self._read_buf.find(b"\n")
            if nl >= 0:
                line = bytes(self._read_buf[:nl + 1])
                del self._read_buf[:nl + 1]
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

    def request(self, method: str, params: dict[str, Any] | None = None,
                max_lines: int = 50, timeout: float = 30.0) -> dict[str, Any]:
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
                raise RuntimeError(f"MCP 发送失败: {e}")

            # 等待响应（匹配 id），带墙钟 deadline 与行数上限
            deadline = time.monotonic() + timeout
            for _ in range(max_lines):
                line = self._readline_with_timeout(deadline)
                if line is None:
                    raise RuntimeError(
                        f"MCP 请求超时：{timeout}s 内未收到 id={request_id} 的响应")
                if line == "":
                    stderr = self._read_stderr_safely()
                    raise RuntimeError(
                        f"MCP 子进程无输出（EOF）。stderr: {stderr[:500]}")
                try:
                    response = json.loads(line)
                except json.JSONDecodeError as e:
                    raise RuntimeError(f"MCP 响应解析失败: {e}")

                if response.get("id") == request_id:
                    return response
                if "id" not in response:
                    logger.debug(f"MCP 通知: {response.get('method', 'unknown')}")
                    continue

            raise RuntimeError(
                f"MCP 请求超时：读取 {max_lines} 行后仍未收到 id={request_id} 的响应")

    def close(self) -> None:
        """关闭子进程。"""
        if self._proc:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.stdout.close()
            except Exception:
                pass
            try:
                self._proc.stderr.close()
            except Exception:
                pass
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    def __del__(self) -> None:
        self.close()


class SSETransport(MCPTransport):
    """通过 HTTP SSE 通信。"""

    def __init__(self, url: str, headers: dict[str, str] | None = None) -> None:
        self.url = url
        self.headers = headers or {}
        self._client = _create_http_client(timeout=30.0)
        self._request_id = 0

    def send(self, message: dict[str, Any]) -> None:
        """通过 HTTP POST 发送消息。"""
        try:
            resp = self._client.post(
                self.url,
                json=message,
                headers={**self.headers, "Content-Type": "application/json"},
            )
            resp.raise_for_status()
        except Exception as e:
            raise RuntimeError(f"MCP SSE 发送失败: {e}")

    def receive(self) -> dict[str, Any]:
        """通过 SSE 接收消息。"""
        try:
            with self._client.stream("GET", self.url, headers=self.headers) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if line.startswith("data:"):
                        data = line[5:].strip()
                        if data:
                            return json.loads(data)
            raise RuntimeError("SSE 连接关闭")
        except Exception as e:
            raise RuntimeError(f"MCP SSE 接收失败: {e}")

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """发送请求。SSE 模式下直接用 POST 请求-响应。"""
        self._request_id += 1
        message = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
        }
        if params:
            message["params"] = params

        try:
            resp = self._client.post(
                self.url,
                json=message,
                headers={**self.headers, "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            raise RuntimeError(f"MCP SSE 请求失败: {e}")

    def close(self) -> None:
        self._client.close()
