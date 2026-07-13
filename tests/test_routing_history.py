"""v0.4.0 Step 9: RoutingHistory tests."""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest
from omniagent.repl.routing_history import (
    RoutingHistory, RoutingRecord, RingBuffer,
)


class TestRingBuffer:
    def test_append_and_recent(self):
        rb = RingBuffer(maxsize=5)
        for i in range(3):
            rb.append(RoutingRecord(
                timestamp=float(i), user_input_preview=f"test {i}",
                intent="chat", complexity=0.1, requires_reasoning=False,
                requires_code_generation=False, requires_tools=False,
                estimated_tokens=10, task_tier=1,
                selected_models=[], scores=[],
            ))
        assert len(rb) == 3
        recent = rb.recent(2)
        assert len(recent) == 2
        assert recent[0].user_input_preview == "test 1"

    def test_maxsize_wraps(self):
        rb = RingBuffer(maxsize=3)
        for i in range(5):
            rb.append(RoutingRecord(
                timestamp=float(i), user_input_preview=f"test {i}",
                intent="chat", complexity=0.1, requires_reasoning=False,
                requires_code_generation=False, requires_tools=False,
                estimated_tokens=10, task_tier=1,
                selected_models=[], scores=[],
            ))
        assert len(rb) == 3
        all_records = rb.all()
        assert all_records[0].user_input_preview == "test 2"
        assert all_records[-1].user_input_preview == "test 4"

    def test_clear(self):
        rb = RingBuffer(maxsize=10)
        rb.append(RoutingRecord(
            timestamp=0.0, user_input_preview="x", intent=None,
            complexity=0.0, requires_reasoning=False,
            requires_code_generation=False, requires_tools=False,
            estimated_tokens=0, task_tier=None, selected_models=[], scores=[],
        ))
        rb.clear()
        assert len(rb) == 0


class TestRoutingHistory:
    def test_record_and_retrieve(self):
        h = RoutingHistory(maxsize=10)
        h.record(RoutingRecord(
            timestamp=time.time(), user_input_preview="重构整个项目",
            intent="refactor", complexity=0.8, requires_reasoning=True,
            requires_code_generation=True, requires_tools=True,
            estimated_tokens=5000, task_tier=4,
            selected_models=["a/pro"], scores=[12.0],
        ))
        records = h.recent(5)
        assert len(records) == 1
        assert records[0].intent == "refactor"
        assert records[0].task_tier == 4

    def test_persistence_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            h = RoutingHistory(maxsize=10, persist_path=str(path))
            h.record(RoutingRecord(
                timestamp=time.time(), user_input_preview="hello",
                intent="chat", complexity=0.1, requires_reasoning=False,
                requires_code_generation=False, requires_tools=False,
                estimated_tokens=50, task_tier=1,
                selected_models=["b/mini"], scores=[5.0],
            ))
            # 重新加载
            h2 = RoutingHistory(maxsize=10, persist_path=str(path))
            records = h2.recent(5)
            assert len(records) == 1
            assert records[0].selected_models == ["b/mini"]

    def test_empty_history(self):
        h = RoutingHistory(maxsize=10)
        assert len(h.recent(10)) == 0
