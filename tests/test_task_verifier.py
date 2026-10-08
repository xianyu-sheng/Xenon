"""任务级校验：完成准则提取 + 发布门审计。"""

from __future__ import annotations

import io

from rich.console import Console

from xenon.engine.task_verifier import (
    extract_acceptance_criteria,
    verify_final_answer,
)
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.repl import REPL


def test_extract_acceptance_criteria():
    assert extract_acceptance_criteria("修好这个 bug，直到 CI 通过") == ["CI 通过"]
    assert extract_acceptance_criteria("保证测试全部通过") == ["测试全部通过"]
    assert extract_acceptance_criteria("帮我看看天气") == []


def test_failure_receipts_contradicting_success_claim():
    ok, reasons = verify_final_answer(
        "已完成，全部通过 ✅",
        tool_events=[{"tool": "command", "success": False, "error": "pytest 失败"}],
    )
    assert ok is False
    assert any("宣称成功" in r for r in reasons)


def test_ci_criterion_requires_successful_command_evidence():
    ok, reasons = verify_final_answer(
        "修复完成",
        criteria=["CI通过"],
        tool_events=[{"tool": "command", "success": False, "error": "exit 1"}],
    )
    assert ok is False
    assert any("完成准则" in r for r in reasons)

    ok, _ = verify_final_answer(
        "修复完成",
        criteria=["CI通过"],
        tool_events=[{"tool": "command", "success": True, "error": ""}],
    )
    assert ok is True


def test_clean_answer_passes():
    ok, reasons = verify_final_answer(
        "已完成写入", tool_events=[{"tool": "write_file", "success": True}]
    )
    assert ok is True
    assert reasons == []


def test_repl_verify_turn_returns_failure_and_records(monkeypatch, tmp_path):
    monkeypatch.setenv("XENON_SESSION_EVENTS_DIR", str(tmp_path))
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    repl = REPL(registry=registry, streaming=False)
    repl._last_user_text = "修到 CI 通过"

    class _Step:
        action = "command"
        is_error = True
        observation = "pytest 失败: 1 failed"

    class _Panel:
        steps = [_Step()]

    ok, reasons = repl._verify_turn(_Panel(), "已完成，全部通过 ✅")

    assert ok is False
    assert reasons
    events = repl._session_events.read()
    assert any(e["type"] == "verification/failed" for e in events)


def test_render_gate_suppresses_success_when_verification_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("XENON_SESSION_EVENTS_DIR", str(tmp_path))
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    repl = REPL(registry=registry, streaming=False)
    repl._last_user_text = "修到 CI 通过"

    output = io.StringIO()
    monkeypatch.setattr(
        "xenon.repl.repl.console", Console(file=output, width=120, force_terminal=False)
    )
    rendered: list[str] = []
    monkeypatch.setattr(
        repl, "_render_assistant_text", lambda content, **kw: rendered.append(content)
    )

    class _Step:
        action = "command"
        is_error = True
        action_input = {"command": "pytest"}
        observation = "pytest 失败"

    class _Panel:
        steps = [_Step()]
        errors: list = []
        tool_call_count = 1

    class _Callback:
        def finish_activity(self):
            pass

        def get_thinking_panel(self):
            return _Panel()

    repl._render_engine_result(_Callback(), "已完成，全部通过 ✅", "ReAct 结果")

    assert rendered == []  # 成功渲染被抑制
    assert "任务校验未通过" in output.getvalue()
    assert "草稿" in output.getvalue()
