"""P2-E3 EventBus 多订阅者 pub/sub 测试。"""

from __future__ import annotations

import logging

from omniagent.engine.event_bus import EventBus, CallbackBusBridge, EVENT_TYPES


# --------------------------- subscribe / publish ---------------------------

def test_single_subscriber_receives():
    bus = EventBus()
    got = []
    bus.subscribe("think", lambda t: got.append(t))
    bus.publish("think", "hello")
    assert got == ["hello"]


def test_multiple_subscribers_all_receive():
    bus = EventBus()
    a, b = [], []
    bus.subscribe("think", a.append)
    bus.subscribe("think", b.append)
    bus.publish("think", "hi")
    assert a == ["hi"]
    assert b == ["hi"]


def test_publish_no_subscribers_noop():
    bus = EventBus()
    bus.publish("think", "nobody")  # 不崩


def test_subscribers_isolated_by_event_type():
    bus = EventBus()
    thinks, finishes = [], []
    bus.subscribe("think", thinks.append)
    bus.subscribe("finish", finishes.append)
    bus.publish("think", "t1")
    bus.publish("finish", "done")
    assert thinks == ["t1"]
    assert finishes == ["done"]


def test_publish_passes_multiple_args():
    bus = EventBus()
    got = []
    bus.subscribe("act", lambda a, b: got.append((a, b)))
    bus.publish("act", "read_file", {"path": "x"})
    assert got == [("read_file", {"path": "x"})]


# --------------------------- unsubscribe ---------------------------

def test_unsubscribe_removes_handler():
    bus = EventBus()
    got = []
    h = got.append
    bus.subscribe("think", h)
    assert bus.subscriber_count("think") == 1
    bus.unsubscribe("think", h)
    assert bus.subscriber_count("think") == 0
    bus.publish("think", "x")
    assert got == []


def test_unsubscribe_nonexistent_noop():
    bus = EventBus()
    bus.unsubscribe("think", lambda x: None)  # 不崩


def test_unsubscribe_only_removes_target():
    bus = EventBus()
    a, b = [], []
    ha, hb = a.append, b.append
    bus.subscribe("think", ha)
    bus.subscribe("think", hb)
    bus.unsubscribe("think", ha)
    bus.publish("think", "x")
    assert a == []
    assert b == ["x"]


# --------------------------- 异常隔离 ---------------------------

def test_handler_exception_isolated(caplog):
    """一个订阅者抛异常不影响其他订阅者与发布方。"""
    bus = EventBus()
    ok = []

    def bad(t):
        raise RuntimeError("boom")
    bus.subscribe("think", bad)
    bus.subscribe("think", ok.append)
    caplog.set_level(logging.WARNING)
    bus.publish("think", "x")  # 不应抛
    assert ok == ["x"]  # 第二个订阅者仍收到
    assert any("EventBus" in r.message for r in caplog.records)


# --------------------------- 杂项 ---------------------------

def test_subscriber_count():
    bus = EventBus()
    assert bus.subscriber_count("think") == 0
    bus.subscribe("think", lambda x: None)
    bus.subscribe("think", lambda x: None)
    assert bus.subscriber_count("think") == 2
    assert bus.subscriber_count("finish") == 0


def test_clear():
    bus = EventBus()
    bus.subscribe("think", lambda x: None)
    bus.subscribe("finish", lambda x: None)
    bus.clear()
    assert bus.subscriber_count("think") == 0
    assert bus.subscriber_count("finish") == 0


def test_event_types_complete():
    for evt in ["think", "act", "observe", "step", "step_done",
                "review", "error", "warning", "finish"]:
        assert evt in EVENT_TYPES


# --------------------------- CallbackBusBridge ---------------------------

def test_bridge_forwards_on_think():
    bus = EventBus()
    got = []
    bus.subscribe("think", got.append)
    bridge = CallbackBusBridge(bus)
    bridge.on_think("hello")
    assert got == ["hello"]


def test_bridge_forwards_on_finish():
    bus = EventBus()
    got = []
    bus.subscribe("finish", got.append)
    bridge = CallbackBusBridge(bus)
    bridge.on_finish("result text")
    assert got == ["result text"]


def test_bridge_forwards_on_act_with_args():
    bus = EventBus()
    got = []
    bus.subscribe("act", lambda a, ai: got.append((a, ai)))
    bridge = CallbackBusBridge(bus)
    bridge.on_act("read_file", {"path": "a.py"})
    assert got == [("read_file", {"path": "a.py"})]


def test_bridge_is_engine_callback():
    from omniagent.engine.callbacks import EngineCallback
    bus = EventBus()
    bridge = CallbackBusBridge(bus)
    assert isinstance(bridge, EngineCallback)


def test_bridge_multiple_subscribers_fanout():
    bus = EventBus()
    a, b, c = [], [], []
    bus.subscribe("think", a.append)
    bus.subscribe("think", b.append)
    bus.subscribe("think", c.append)
    bridge = CallbackBusBridge(bus)
    bridge.on_think("fan")
    assert a == b == c == ["fan"]


# --------------------------- 引擎集成 ---------------------------

def test_react_engine_with_bridge_publishes_finish():
    """ReAct 引擎照常 self.callback（桥接），订阅者收到 finish 事件。"""
    from omniagent.engine.react_engine import ReActEngine

    bus = EventBus()
    finishes = []
    thinks = []
    bus.subscribe("finish", finishes.append)
    bus.subscribe("think", thinks.append)

    eng = ReActEngine(["m1"], callback=CallbackBusBridge(bus))

    def fake_llm(messages, **kw):
        return "Thought: 我想想\nFinal Answer: done"
    eng._call_llm = fake_llm
    eng.max_iterations = 2
    eng.run("hi")

    assert len(finishes) == 1
    assert "done" in finishes[0]
    assert thinks  # 至少收到一次 think
