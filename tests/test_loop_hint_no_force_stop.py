"""P0/E-2：低置信度循环只提示、且同一循环只提示一次。

v0.9.5 的「连续 3 次提示 → 强制收束」把正常的调研/实现多阶段任务在
18/120 步掐断（真实 SmartBench 会话）。执行层应与证据层正交：循环的
硬终止只保留高置信度分支，低置信度只注入一次提示，预算仍由 120 步兜底。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from xenon.engine.react_engine import ReActEngine
from xenon.repl.model_registry import ModelConfig


def test_loop_hints_do_not_force_stop(tmp_path, monkeypatch) -> None:
    target = tmp_path / "x.txt"
    target.write_text("x", encoding="utf-8")

    model = "fake/model"
    engine = ReActEngine(
        [model],
        max_iterations=6,
        model_configs={model: ModelConfig(model_id=model, alias="fake")},
        native_fc=False,
    )
    warnings: list[str] = []
    engine.callback.on_warning = warnings.append

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

    result = engine.run("implement the feature by reading the same file")

    # 低置信度循环不再提前结束：应该用满预算，由预算边界诚实收尾。
    assert "强制收束" not in result
    assert "已连续 3 次检测到循环" not in result
    assert "步数上限" in result
    loop_warnings = [w for w in warnings if "潜在循环" in w]
    assert len(loop_warnings) == 1  # 同一 (reason, loop_length) 只提示一次


def test_loop_hints_respect_cooldown_across_changing_signatures(tmp_path, monkeypatch) -> None:
    """C2: 循环信号时有时无/签名变化时，提示也不能每轮刷屏。"""

    target = tmp_path / "x.txt"
    target.write_text("x", encoding="utf-8")
    model = "fake/model"
    engine = ReActEngine(
        [model],
        max_iterations=12,
        model_configs={model: ModelConfig(model_id=model, alias="fake")},
        native_fc=False,
    )
    engine.loop_hint_cooldown = 4
    warnings: list[str] = []
    engine.callback.on_warning = warnings.append
    counter = {"n": 0}

    def fake_llm(phase, messages, **kwargs):
        return json.dumps(
            {
                "thought": "read again",
                "action": "read_file",
                "action_input": {"file_path": str(target)},
            }
        )

    def check():
        counter["n"] += 1
        return SimpleNamespace(
            is_loop=True,
            confidence=0.5,
            reason=f"loop-{counter['n']}",
            loop_length=1,
            similar_turns=2,
        )

    monkeypatch.setattr(engine, "_call_llm_for_phase", fake_llm)
    monkeypatch.setattr(
        engine,
        "_loop_detector",
        SimpleNamespace(
            check=check,
            reset=lambda: None,
            add_turn=lambda **kwargs: None,
        ),
    )

    result = engine.run("implement the feature")

    loop_warnings = [w for w in warnings if "潜在循环" in w]
    assert "强制收束" not in result
    # 12 步 / 冷却 4：最多约 3-4 次，而不是每步一次。
    assert 2 <= len(loop_warnings) <= 4
