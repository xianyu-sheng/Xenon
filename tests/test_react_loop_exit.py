"""Unlimited ReAct must still terminate on a high-confidence loop."""

from __future__ import annotations

import json

from xenon.engine.callbacks import EngineCallback
from xenon.engine.context import AgentContext
from xenon.engine.react_engine import ReActEngine
from xenon.engine.result import TerminationReason


class _Callback(EngineCallback):
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def on_warning(self, message: str) -> None:
        self.warnings.append(message)


def test_unlimited_react_terminates_on_loop_detector(tmp_path):
    target = tmp_path / "same.txt"
    target.write_text("unchanged\n", encoding="utf-8")

    callback = _Callback()
    engine = ReActEngine(
        ["fake/model"],
        max_iterations=0,
        native_fc=False,
        callback=callback,
    )
    response = json.dumps(
        {
            "thought": "read the same file again",
            "action": "read_file",
            "action_input": {"file_path": str(target)},
        }
    )
    engine._call_llm_for_phase = lambda *args, **kwargs: response  # type: ignore[method-assign]

    context = AgentContext({"_execution_level": 3})
    result = engine.run_task("反复读取同一个文件", context=context)

    assert "检测到 ReAct 引擎陷入循环" in result
    assert engine.max_iterations == 0
    assert result.reason is TerminationReason.LOOP
