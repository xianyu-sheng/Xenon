"""EventBus <-> EngineCallback 桥接层。

提供 EventAwareCallback: 在原有 EngineCallback 的基础上，
同时向 EventBus 发布类型化事件。保持向后兼容性的同时启用事件驱动架构。

使用方式:
    bus = EventBus()
    callback = EventAwareCallback(delegate=ConsoleCallback(), bus=bus)
    engine = ReActEngine(..., callback=callback)
    # callback 正常触发，同时所有事件自动发布到 bus
"""

from __future__ import annotations

import time
from typing import Any

from omniagent.engine.callbacks import EngineCallback
from omniagent.events.bus import EventBus
from omniagent.events.models import (
    AgentFinalAnswerEvent,
    AgentThoughtEvent,
    RunErrorEvent,
    RunWarningEvent,
    ReviewFinishedEvent,
    StepFinishedEvent,
    StepStartedEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
)


class EventAwareCallback(EngineCallback):
    """Enhanced callback that mirrors activity to EventBus.

    Wraps an existing EngineCallback (e.g., ConsoleCallback) and publishes
    every lifecycle event to the EventBus for additional subscribers
    (TUI, trace writer, IPC broadcaster).
    """

    def __init__(self, delegate: EngineCallback, bus: EventBus) -> None:
        self._delegate = delegate
        self._bus = bus
        self._run_id: str = ""
        self._tool_count: int = 0
        self._current_tool_use_id: str | None = None
        self._current_tool_name: str | None = None
        self._tool_start_time: float = 0.0
        self._step_results: dict[int, str] = {}

    def set_run_id(self, run_id: str) -> None:
        """设置当前 run_id（在每次 run 开始时调用）。"""
        self._run_id = run_id
        self._tool_count = 0

    # ── Think ───────────────────────────────────────────────

    def on_think(self, thought: str) -> None:
        from omniagent.events.bus import logger
        self._delegate.on_think(thought)
        try:
            self._bus.publish_sync(
                AgentThoughtEvent(run_id=self._run_id, thought=thought)
            )
        except Exception as e:
            logger.warning("EventBus publish failed: %s", e)

    # ── Act / Observe ───────────────────────────────────────

    def on_act(self, action: str, action_input: dict) -> None:
        self._delegate.on_act(action, action_input)
        self._tool_count += 1
        self._current_tool_use_id = f"tool-{self._tool_count}"
        self._current_tool_name = action
        self._tool_start_time = time.monotonic()
        try:
            self._bus.publish_sync(
                ToolCallStartedEvent(
                    run_id=self._run_id,
                    tool_use_id=self._current_tool_use_id,
                    tool_name=action,
                    params=action_input,
                )
            )
        except Exception:
            pass

    def on_observe(self, observation: str) -> None:
        self._delegate.on_observe(observation)
        elapsed_ms = int((time.monotonic() - self._tool_start_time) * 1000) if self._tool_start_time else 0
        is_error = observation.startswith(("执行失败", "执行异常", "错误:", "Error:"))
        try:
            self._bus.publish_sync(
                ToolCallFinishedEvent(
                    run_id=self._run_id,
                    tool_use_id=self._current_tool_use_id or f"tool-{self._tool_count}",
                    tool_name=self._current_tool_name or "",
                    output=observation,
                    is_error=is_error,
                    elapsed_ms=elapsed_ms,
                )
            )
        except Exception:
            pass
        self._current_tool_use_id = None
        self._current_tool_name = None
        self._tool_start_time = 0.0

    # ── Step ────────────────────────────────────────────────

    def on_step(self, step_id: int, total: int, task: str) -> None:
        self._delegate.on_step(step_id, total, task)
        try:
            self._bus.publish_sync(
                StepStartedEvent(run_id=self._run_id, step=step_id)
            )
        except Exception:
            pass

    def on_step_done(self, step_id: int, success: bool, summary: str) -> None:
        self._delegate.on_step_done(step_id, success, summary)
        self._step_results[step_id] = summary
        try:
            self._bus.publish_sync(
                StepFinishedEvent(
                    run_id=self._run_id, step=step_id,
                    success=success, summary=summary,
                )
            )
        except Exception:
            pass

    # ── Review ──────────────────────────────────────────────

    def on_review(self, score: int, passed: bool, feedback: str) -> None:
        self._delegate.on_review(score, passed, feedback)
        try:
            self._bus.publish_sync(
                ReviewFinishedEvent(
                    run_id=self._run_id, score=score,
                    passed=passed, feedback=feedback,
                )
            )
        except Exception:
            pass

    # ── Error / Warning ─────────────────────────────────────

    def on_error(self, error: str) -> None:
        self._delegate.on_error(error)
        try:
            self._bus.publish_sync(
                RunErrorEvent(run_id=self._run_id, error=error)
            )
        except Exception:
            pass

    def on_warning(self, warning: str) -> None:
        self._delegate.on_warning(warning)
        try:
            self._bus.publish_sync(
                RunWarningEvent(run_id=self._run_id, warning=warning)
            )
        except Exception:
            pass

    # ── Finish ──────────────────────────────────────────────

    def on_finish(self, result: str) -> None:
        self._delegate.on_finish(result)
        try:
            self._bus.publish_sync(
                AgentFinalAnswerEvent(run_id=self._run_id, result=result)
            )
        except Exception:
            pass

    # ── Thinking Panel (delegate) ───────────────────────────

    def get_thinking_panel(self):
        getter = getattr(self._delegate, "get_thinking_panel", None)
        return getter() if getter else None
