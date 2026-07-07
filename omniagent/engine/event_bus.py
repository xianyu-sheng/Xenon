"""EventBus — 多订阅者发布/订阅事件总线（P2-E3 / §Q1）。

``EngineCallback`` 是**单订阅者**回调接口：引擎持有一个 callback 对象通知外部。
规范 Q1 称"层间通过事件总线通信，TUI 更新/IPC 广播/日志记录各自独立订阅"——
本模块在 callback 之上加一层 pub/sub，使 REPL、日志、（未来）IPC 可各自订阅同一
事件互不干扰。``callback`` 保留为默认订阅者，**完全向后兼容**（引擎照常
``self.callback``）。

用法::

    bus = EventBus()
    bus.subscribe("think", lambda t: logger.debug("think: %s", t))
    bus.subscribe("finish", ui.render_final)
    engine.callback = CallbackBusBridge(bus)  # 引擎无感知
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from omniagent.engine.callbacks import EngineCallback

logger = logging.getLogger(__name__)

# 支持的事件类型（与 EngineCallback 钩子对齐 + §8.27.3 扩展 start/iteration）
EVENT_TYPES: frozenset[str] = frozenset({
    "think", "act", "observe", "step", "step_done",
    "review", "error", "warning", "finish",
    "start", "iteration",
})

Handler = Callable[..., Any]


class EventBus:
    """多订阅者事件总线。

    - ``subscribe(event_type, handler)`` 注册；同一事件可多订阅者。
    - ``publish`` 顺序调用该事件全部订阅者；**任一订阅者抛异常被隔离**（记 warning，
      不影响其他订阅者与发布方），避免 UI/日志/IPC 互相拖垮。
    """

    def __init__(self) -> None:
        self._subs: dict[str, list[Handler]] = {}

    def subscribe(self, event_type: str, handler: Handler) -> None:
        """注册一个订阅者。"""
        self._subs.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type: str, handler: Handler) -> None:
        """移除一个订阅者（不存在则无操作）。"""
        subs = self._subs.get(event_type, [])
        if handler in subs:
            subs.remove(handler)

    def publish(self, event_type: str, *args: Any, **kwargs: Any) -> None:
        """向该事件全部订阅者广播；订阅者异常隔离。"""
        for handler in list(self._subs.get(event_type, [])):
            try:
                handler(*args, **kwargs)
            except Exception as e:
                logger.warning(
                    "EventBus 订阅者 %s 处理 %s 异常（已隔离）: %s",
                    getattr(handler, "__name__", handler), event_type, e,
                )

    def subscriber_count(self, event_type: str) -> int:
        return len(self._subs.get(event_type, []))

    def clear(self) -> None:
        """清空全部订阅（测试/重置用）。"""
        self._subs.clear()


class CallbackBusBridge(EngineCallback):
    """把 ``EngineCallback`` 钩子转发到 ``EventBus`` 的桥接回调。

    引擎照常持有 ``self.callback``（本桥接实例），各 ``on_xxx`` 把事件 publish
    到 bus，由 bus 分发给全部订阅者。未订阅的事件无副作用。引擎代码无需改动。
    """

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    def on_think(self, thought: str) -> None:
        self._bus.publish("think", thought)

    def on_act(self, action: str, action_input: dict) -> None:
        self._bus.publish("act", action, action_input)

    def on_observe(self, observation: str) -> None:
        self._bus.publish("observe", observation)

    def on_step(self, step_id: int, total: int, task: str) -> None:
        self._bus.publish("step", step_id, total, task)

    def on_step_done(self, step_id: int, success: bool, summary: str) -> None:
        self._bus.publish("step_done", step_id, success, summary)

    def on_review(self, score: int, passed: bool, feedback: str) -> None:
        self._bus.publish("review", score, passed, feedback)

    def on_error(self, error: str) -> None:
        self._bus.publish("error", error)

    def on_warning(self, warning: str) -> None:
        self._bus.publish("warning", warning)

    def on_finish(self, result: str) -> None:
        self._bus.publish("finish", result)
