"""Trace IDs — run_id / call_id 链路追踪（P3-Q2 / §8.8.4）。

调试"为什么所有模型失败"时，散落各行的日志无法把同一次 ``run()`` 内的多次
``_call_llm`` 调用、同一次调用的多次 fallback 串起来。本模块为每次 run 生成
``run_id``，每次 ``chat_completion`` 调用生成 ``call_id``，日志带
``[run_id/call_id]`` 前缀，即可关联整条 fallback 链。

纯函数 + 无副作用，便于单测；不依赖时间/随机源以外的全局状态。
"""

from __future__ import annotations

import logging
import uuid

_LOGGER = logging.getLogger("omniagent.trace")


def new_run_id() -> str:
    """生成一次 run 的链路 ID（8 位短 hex，足以区分并发 run）。"""
    return uuid.uuid4().hex[:8]


def new_call_id() -> str:
    """生成一次 LLM 调用的 ID（6 位短 hex）。"""
    return uuid.uuid4().hex[:6]


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
