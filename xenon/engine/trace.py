"""Trace IDs — run_id / call_id 链路追踪（P3-Q2 / §8.8.4）。

调试"为什么所有模型失败"时，散落各行的日志无法把同一次 ``run()`` 内的多次
``_call_llm`` 调用、同一次调用的多次 fallback 串起来。本模块为每次 run 生成
``run_id``，每次 ``chat_completion`` 调用生成 ``call_id``，日志带
``[run_id/call_id]`` 前缀，即可关联整条 fallback 链。

纯函数 + 无副作用，便于单测；不依赖时间/随机源以外的全局状态。
"""

from __future__ import annotations

import contextvars
import itertools
import logging
import threading
import uuid

_LOGGER = logging.getLogger("xenon.trace")
_CALL_ID_MASK = (1 << 24) - 1
_CALL_ID_COUNTER = itertools.count(uuid.uuid4().int & _CALL_ID_MASK)
_CALL_ID_LOCK = threading.Lock()


def new_run_id() -> str:
    """生成一次 run 的链路 ID（8 位短 hex，足以区分并发 run）。"""
    return uuid.uuid4().hex[:8]


def new_call_id() -> str:
    """生成一次 LLM 调用的 ID（6 位短 hex）。

    The counter is process-local and starts at a random offset, preserving the
    compact log format while making normal in-process calls collision-free.
    It wraps only after 16,777,216 calls, at which point the six-character
    format itself has been exhausted.
    """
    with _CALL_ID_LOCK:
        return f"{next(_CALL_ID_COUNTER) & _CALL_ID_MASK:06x}"


def prefix(run_id: str | None, call_id: str | None = None) -> str:
    """构造日志前缀 ``[run_id]`` 或 ``[run_id/call_id]``；缺省用 ``?`` 占位。"""
    r = run_id or "?"
    if call_id:
        return f"[{r}/{call_id}]"
    return f"[{r}]"


def trace_logger(run_id: str | None, call_id: str | None = None) -> logging.LoggerAdapter:
    """返回带 ``[run_id/call_id]`` 前缀的 LoggerAdapter（前缀并入 message）。"""
    return _TraceAdapter(_LOGGER, {"trace": prefix(run_id, call_id)})


class _TraceAdapter(logging.LoggerAdapter):
    """把 trace 前缀并入 message（默认 LoggerAdapter 只合入 extra，不显式前缀）。"""

    def process(self, msg, kwargs):
        return f"{self.extra['trace']} {msg}", kwargs


# ── v0.8.3: 模块级日志自动带 [run_id] 前缀 ─────────────────
# 引擎模块 logger 挂 TraceContextFilter 后，所有日志自动带当前 run_id，
# 无需改每个调用点。run_id 由 _begin_run() 写入 contextvar（线程隔离），
# 工具执行层（ToolExecutor）日志也随之贯通——排查时整条链可关联。

_TRACE_RUN_VAR: contextvars.ContextVar[str] = contextvars.ContextVar(
    "trace_run_id", default=""
)


def set_trace_run_id(run_id: str | None) -> None:
    """设置当前上下文（线程/协程）的 run_id；None/空则清除。"""
    _TRACE_RUN_VAR.set(run_id or "")


def get_trace_run_id() -> str:
    return _TRACE_RUN_VAR.get()


class TraceContextFilter(logging.Filter):
    """日志过滤器：message 自动加 ``[run_id]`` 前缀（避免双重前缀）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        run_id = _TRACE_RUN_VAR.get()
        if run_id:
            message = record.getMessage()
            if not message.startswith(f"[{run_id}]") and not message.startswith("["):
                record.msg = f"[{run_id}] {record.msg}"
        return True
