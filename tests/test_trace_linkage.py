"""链路追踪贯通测试（v0.8.3）：run_id 前缀贯穿引擎与工具执行层。

SWE-bench 排查痛点：工具执行层日志无 run_id，无法从日志关联
「哪次工具失败属于哪个 run/execution」。
"""

from __future__ import annotations

import io
import logging

from xenon.engine.trace import (
    TraceContextFilter,
    get_trace_run_id,
    prefix,
    set_trace_run_id,
)


class TestTraceContextFilter:
    def test_prefix_added_when_run_active(self) -> None:
        logger = logging.getLogger("xenon.engine.test_trace")
        logger.handlers.clear()
        logger.addFilter(TraceContextFilter())
        logger.setLevel(logging.WARNING)
        buf = io.StringIO()
        logger.addHandler(logging.StreamHandler(buf))
        set_trace_run_id("deadbeef")
        try:
            logger.warning("某条引擎日志")
        finally:
            set_trace_run_id(None)
        assert "[deadbeef]" in buf.getvalue()

    def test_no_prefix_without_run(self) -> None:
        logger = logging.getLogger("xenon.engine.test_trace_none")
        logger.addFilter(TraceContextFilter())
        logger.setLevel(logging.WARNING)
        buf = io.StringIO()
        logger.addHandler(logging.StreamHandler(buf))
        set_trace_run_id(None)
        logger.warning("无 run 上下文")
        assert "[deadbeef]" not in buf.getvalue()

    def test_no_double_prefix(self) -> None:
        logger = logging.getLogger("xenon.engine.test_trace_double")
        logger.addFilter(TraceContextFilter())
        logger.setLevel(logging.WARNING)
        buf = io.StringIO()
        logger.addHandler(logging.StreamHandler(buf))
        set_trace_run_id("cafe1234")
        try:
            logger.warning("[cafe1234/abcdef] 已有 call_id 前缀的日志")
        finally:
            set_trace_run_id(None)
        out = buf.getvalue()
        assert out.count("[cafe1234") == 1, f"双重前缀: {out}"

    def test_engine_and_tool_loggers_share_filter(self) -> None:
        """引擎模块与 ToolExecutor 的 logger 都挂了 filter（模块加载时）。"""
        import xenon.engine.plan_execute_engine  # noqa: F401
        import xenon.nodes.tool_executor  # noqa: F401

        for logger in (
            logging.getLogger("xenon.engine.plan_execute_engine"),
            logging.getLogger("xenon.nodes.tool_executor"),
            logging.getLogger("xenon.engine.base"),
        ):
            assert any(
                isinstance(f, TraceContextFilter) for f in logger.filters
            ), f"{logger.name} 未挂 TraceContextFilter"


class TestToolExecutorTracePrefix:
    def test_execute_logs_carry_run_id(self, tmp_path) -> None:
        """ToolExecutor.execute 的关键日志带 [run_id/exec] 前缀。"""
        from xenon.engine.context import AgentContext
        from xenon.engine.tool_tracker import ToolExecutionTracker
        from xenon.nodes.tool_executor import ToolExecutor

        target = tmp_path / "a.py"
        target.write_text("x = 1\n", encoding="utf-8")
        ctx = AgentContext()
        ctx.set("_evidence_session_id", "run1234")
        ex = ToolExecutor()
        logger = logging.getLogger("xenon.nodes.tool_executor")
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            # 证据门拦截路径（无 read 证据直接 edit）会打 warning 日志
            result = ex.execute(
                "edit_file",
                {"file_path": str(target), "old_text": "x = 1", "new_text": "x = 2"},
                ctx, tracker=ToolExecutionTracker(),
            )
            # 自动补读后应成功；无论成败，日志必须带 [run1234/ 前缀
            log_text = buf.getvalue()
            assert "[run1234/" in log_text, f"工具日志缺 run_id 前缀: {log_text[:300]}"
            assert result.success is True, result.error
        finally:
            logger.removeHandler(handler)


class TestPrefixHelper:
    def test_prefix_forms(self) -> None:
        assert prefix("aabbccdd") == "[aabbccdd]"
        assert prefix("aabbccdd", "001122") == "[aabbccdd/001122]"
        assert prefix(None) == "[?]"
