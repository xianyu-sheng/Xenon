"""Step 5: 在线 pre-tool gate 行为测试。

The legacy evidence ledger these tests once also asserted was retired on
2026-11-10 (docs/design/legacy-retirement-plan.md); the FactBindingGate
behavior itself — block a blind edit, auto-read remediation, observe vs
enforce — is still live and stays covered here.
"""

from __future__ import annotations

from pathlib import Path

from xenon.engine.context import AgentContext
from xenon.engine.tool_tracker import ToolExecutionTracker
from xenon.nodes.tool_executor import ToolExecutor
from xenon.nodes.tool_node import ToolNode


def test_execute_enforcement_blocks_before_toolnode(
    monkeypatch, tmp_path: Path
) -> None:
    """真正 execute 路径必须在 ToolNode 运行前阻断盲编辑
    （关闭 auto-read 时，验证拦截本身）。"""
    target = tmp_path / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    invoked = False

    def should_not_run(self, context):
        nonlocal invoked
        invoked = True
        return {"success": True, "result": "unexpected"}

    monkeypatch.setattr("xenon.nodes.tool_executor.ToolNode.execute", should_not_run)
    result = ToolExecutor(evidence_enforcement="enforce", evidence_auto_read=False).execute(
        "edit_file",
        {"file_path": str(target), "old_text": "value = 1", "new_text": "value = 2"},
        AgentContext(),
        tracker=ToolExecutionTracker(),
    )
    assert result.success is False
    assert result.error == "写入前缺少目标文件读取证据: " + str(target)
    assert invoked is False


def test_execute_auto_read_remediates_blind_edit(monkeypatch, tmp_path: Path) -> None:
    """SWE-bench 实测修复（matplotlib-23562）：盲编辑被拦截后，
    自动补 read_file 再重试原工具 → 编辑成功。"""
    target = tmp_path / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    real_execute = ToolNode.execute

    def real_tool(self, context):
        return real_execute(self, context)

    monkeypatch.setattr("xenon.nodes.tool_executor.ToolNode.execute", real_tool)
    tracker = ToolExecutionTracker()
    # enforce 才会拦截盲编辑并触发自动补读（默认 observe 直接放行）。
    result = ToolExecutor(evidence_enforcement="enforce").execute(
        "edit_file",
        {"file_path": str(target), "old_text": "value = 1", "new_text": "value = 2"},
        AgentContext(),
        tracker=tracker,
    )
    assert result.success is True, result.error
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    # 自动补读必须留下真实证据：tracker 里有 read_file + edit_file
    tools = [c.tool_name for c in tracker.calls if c.success]
    assert tools.count("read_file") >= 1
    assert "edit_file" in tools


def test_executor_defaults_to_observe() -> None:
    """默认 observe：不实时阻断。

    enforce 会在用户明确要求写文件时也硬拦（「⛔ 在线证据验证拒绝」），
    体验不可接受。enforce 能力保留，调用方仍可显式传 mode="enforce"。
    """

    assert ToolExecutor().evidence_enforcement == "observe"
    assert (
        ToolExecutor(evidence_enforcement="enforce").evidence_enforcement
        == "enforce"
    )
    try:
        ToolExecutor(evidence_enforcement="nonsense")
    except ValueError as exc:
        assert "observe" in str(exc)
    else:  # pragma: no cover - must fail closed
        raise AssertionError("an unknown enforcement mode must be rejected")


def test_pre_tool_allows_edit_after_exact_read(tmp_path: Path) -> None:
    target = tmp_path / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    tracker = ToolExecutionTracker()
    tracker.record("read_file", {"file_path": str(target)}, True, "value = 1")
    verdict = ToolExecutor().before_tool(
        "edit_file",
        {"file_path": str(target), "old_text": "value = 1", "new_text": "value = 2"},
        AgentContext(),
        tracker,
    )
    assert verdict.passed is True


def test_pre_tool_blocks_blind_edit_before_node_execution(tmp_path: Path) -> None:
    target = tmp_path / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    verdict = ToolExecutor(evidence_enforcement="enforce").before_tool(
        "edit_file",
        {"file_path": str(target), "old_text": "value = 1", "new_text": "value = 2"},
        AgentContext(),
        ToolExecutionTracker(),
    )
    assert verdict.passed is False
    assert "读取" in verdict.reason or "盲写" in verdict.reason


def test_pre_tool_allows_new_file_without_prior_read(tmp_path: Path) -> None:
    target = tmp_path / "new.py"
    verdict = ToolExecutor().before_tool(
        "write_file",
        {"file_path": str(target), "content": "value = 1\n"},
        AgentContext(),
        ToolExecutionTracker(),
    )
    assert verdict.passed is True


def test_observe_mode_warns_but_does_not_block(tmp_path: Path) -> None:
    target = tmp_path / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    verdict = ToolExecutor(evidence_enforcement="observe").before_tool(
        "edit_file",
        {"file_path": str(target), "old_text": "value = 1", "new_text": "value = 2"},
        AgentContext(),
        ToolExecutionTracker(),
    )
    assert verdict.passed is True


