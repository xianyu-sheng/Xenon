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


def test_repl_verify_turn_prints_warning_and_records(monkeypatch, tmp_path):
    monkeypatch.setenv("XENON_SESSION_EVENTS_DIR", str(tmp_path))
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    repl = REPL(registry=registry, streaming=False)
    repl._last_user_text = "修到 CI 通过"

    output = io.StringIO()
    monkeypatch.setattr(
        "xenon.repl.repl.console", Console(file=output, width=120, force_terminal=False)
    )

    class _Step:
        action = "command"
        is_error = True
        observation = "pytest 失败: 1 failed"

    class _Panel:
        steps = [_Step()]

    repl._verify_turn(_Panel(), "已完成，全部通过 ✅")

    assert "任务校验未通过" in output.getvalue()
    events = repl._session_events.read()
    assert any(e["type"] == "verification/failed" for e in events)
