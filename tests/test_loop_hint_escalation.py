"""P1-4：连续低置信度循环提示达到上限后强制收束。"""

from __future__ import annotations

import json
from types import SimpleNamespace

from xenon.engine.react_engine import ReActEngine
from xenon.repl.model_registry import ModelConfig


def test_loop_hints_force_stop(tmp_path, monkeypatch) -> None:
    target = tmp_path / "x.txt"
    target.write_text("x", encoding="utf-8")

    model = "fake/model"
    engine = ReActEngine(
        [model],
        max_iterations=8,
        model_configs={model: ModelConfig(model_id=model, alias="fake")},
        native_fc=False,
    )

    def fake_llm(phase, messages, **kwargs):
        return json.dumps(
            {
                "thought": "read the same file again",
                "action": "read_file",
                "action_input": {"file_path": str(target)},
            }
        )

    monkeypatch.setattr(engine, "_call_llm_for_phase", fake_llm)
    monkeypatch.setattr(
        engine,
        "_loop_detector",
        SimpleNamespace(
            check=lambda: SimpleNamespace(
                is_loop=True,
                confidence=0.5,
                reason="工具调用模式重复",
                loop_length=1,
                similar_turns=2,
            ),
            reset=lambda: None,
            add_turn=lambda **kwargs: None,
        ),
    )

    result = engine.run("keep reading the same file")

    assert "连续 3 次检测到循环" in result
    assert "强制收束" in result
