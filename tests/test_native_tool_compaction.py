"""原生工具协议 in-run 压缩测试（P3 上下文溢出根因修复）。

背景：原生工具调用（DeepSeek/OpenAI function calling）下，历史里存在
``assistant(tool_calls)`` + ``role="tool"`` 成对消息。ContextManager 的摘要格式
无法表达 ``tool_call_id``，所以旧实现遇到这类消息**整体跳过压缩**——长任务的
messages 会一直增长到超出上下文窗口，下一次 provider 调用直接失败。

修复：按协议块裁剪。断言两件事：
1. 压缩后 tool 协议仍然合法（每个 tool_calls 都有配对的 tool 消息）；
2. 逼近窗口时确实会缩小，而不是原样返回。
"""

from __future__ import annotations

from typing import Any

from xenon.engine.base import BaseEngine


class _Engine(BaseEngine):
    """可实例化的 BaseEngine，用于直接测压缩方法。"""

    def run(self, user_input, context=None, ctx_mgr=None):  # pragma: no cover
        return ""


def _engine(window: int = 1000) -> _Engine:
    eng = _Engine.__new__(_Engine)
    eng.model_priority = []
    eng.model_configs = {}
    eng._ctx_mgr = None
    # 固定上下文窗口，让 _near_context_window 可预测
    eng._context_window = lambda: window  # type: ignore[method-assign]
    return eng


def _tool_round(call_id: str, payload: str) -> list[dict[str, Any]]:
    """构造一轮完整的原生工具协议消息。"""
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": payload},
    ]


def _protocol_is_valid(messages: list[dict[str, Any]]) -> bool:
    """每个带 tool_calls 的 assistant 后面必须紧跟其全部 tool 响应。"""
    i = 0
    while i < len(messages):
        message = messages[i]
        calls = message.get("tool_calls")
        if message.get("role") == "assistant" and calls:
            expected = [str(c.get("id", "")) for c in calls]
            got = []
            j = i + 1
            while j < len(messages) and messages[j].get("role") == "tool":
                got.append(str(messages[j].get("tool_call_id", "")))
                j += 1
            if got != expected:
                return False
            i = j
            continue
        if message.get("role") == "tool":
            # 悬挂的 tool 消息（没有前置 assistant tool_calls）→ 非法
            return False
        i += 1
    return True


class TestSplitProtocolBlocks:
    def test_assistant_and_its_tools_form_one_block(self):
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            *_tool_round("c1", "r1"),
        ]
        blocks = BaseEngine._split_protocol_blocks(messages)
        assert [len(b) for b in blocks] == [1, 1, 2]
        assert blocks[2][0]["role"] == "assistant"
        assert blocks[2][1]["role"] == "tool"

    def test_multiple_tools_stay_in_same_block(self):
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "a",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    },
                    {
                        "id": "b",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "a", "content": "ra"},
            {"role": "tool", "tool_call_id": "b", "content": "rb"},
        ]
        blocks = BaseEngine._split_protocol_blocks(messages)
        assert len(blocks) == 1
        assert len(blocks[0]) == 3

    def test_orphan_tool_message_becomes_own_block(self):
        messages = [{"role": "tool", "tool_call_id": "x", "content": "r"}]
        blocks = BaseEngine._split_protocol_blocks(messages)
        assert blocks == [[messages[0]]]


class TestCompactNativeToolMessages:
    def test_below_pressure_returns_unchanged(self):
        eng = _engine(window=100_000)
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            *_tool_round("c1", "r1"),
        ]
        assert eng._compact_native_tool_messages(messages) is messages

    def test_shrinks_when_near_window_and_stays_protocol_valid(self):
        eng = _engine(window=1000)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "the original task"},
        ]
        # 20 轮工具调用，每轮 payload 够大以触发窗口压力
        for i in range(20):
            messages.extend(_tool_round(f"c{i}", "x" * 200))

        out = eng._compact_native_tool_messages(messages, keep_recent_blocks=4)
        assert len(out) < len(messages), "逼近窗口时必须缩小"
        assert _protocol_is_valid(out), "压缩后 tool 协议必须仍然合法"
        # 系统提示与原始任务必须保留
        assert out[0]["content"] == "sys"
        assert out[1]["content"] == "the original task"
        # 折叠摘要就位，且是不需要配对的 user 消息
        assert any(
            m["role"] == "user" and "历史已压缩" in m.get("content", "") for m in out
        )
        # 最近若干轮保留
        assert out[-1]["role"] == "tool"

    def test_never_splits_a_tool_pair(self):
        eng = _engine(window=1000)
        messages: list[dict[str, Any]] = [{"role": "system", "content": "s"}]
        for i in range(30):
            messages.extend(_tool_round(f"c{i}", "y" * 150))
        for keep in (1, 2, 3, 5, 8):
            out = eng._compact_native_tool_messages(messages, keep_recent_blocks=keep)
            assert _protocol_is_valid(out), f"keep_recent_blocks={keep} 破坏了协议"

    def test_failure_falls_back_to_original(self, monkeypatch):
        eng = _engine(window=1000)
        messages = [{"role": "system", "content": "s"}]
        for i in range(20):
            messages.extend(_tool_round(f"c{i}", "z" * 200))

        def _explode(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(BaseEngine, "_split_protocol_blocks", _explode)
        # 压缩失败绝不能中断主循环
        assert eng._compact_native_tool_messages(messages) is messages


class TestMaybeCompactRouting:
    def test_native_messages_route_to_protocol_compactor(self):
        eng = _engine(window=100_000)
        seen: list[Any] = []
        eng._compact_native_tool_messages = (  # type: ignore[method-assign]
            lambda msgs, **kw: seen.append(msgs) or msgs
        )
        messages = [{"role": "system", "content": "s"}, *_tool_round("c1", "r")]
        eng._maybe_compact_messages(messages, turn=5)
        assert seen, "含 tool 协议消息时必须走 block 级压缩"

    def test_urgent_pressure_compacts_off_schedule(self):
        """逼近窗口时不能等下一个 every 周期——否则下一次调用就超限。"""
        eng = _engine(window=200)
        seen: list[Any] = []
        eng._compact_native_tool_messages = (  # type: ignore[method-assign]
            lambda msgs, **kw: seen.append(msgs) or msgs
        )
        messages = [{"role": "system", "content": "s"}]
        for i in range(10):
            messages.extend(_tool_round(f"c{i}", "q" * 200))
        # turn=3 不是 every=5 的倍数，但压力已到 → 必须压缩
        eng._maybe_compact_messages(messages, turn=3)
        assert seen, "窗口压力下必须立即压缩，不能等周期"

    def test_no_pressure_off_schedule_skips(self):
        eng = _engine(window=100_000)
        messages = [{"role": "system", "content": "s"}, *_tool_round("c1", "r")]
        assert eng._maybe_compact_messages(messages, turn=3) is messages
