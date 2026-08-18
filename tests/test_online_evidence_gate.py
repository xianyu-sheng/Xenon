"""Step 5: 在线 pre-tool evidence enforcement tests."""
from __future__ import annotations

from pathlib import Path

from xenon.engine.context import AgentContext
from xenon.engine.evidence_gate import FactBindingGate
from xenon.engine.evidence_runtime import EventKind, EvidenceLedger, LifecyclePhase
from xenon.engine.tool_tracker import ToolExecutionTracker
from xenon.nodes.tool_executor import ToolExecutor
from xenon.nodes.tool_node import ToolNode


def _executor(ledger: EvidenceLedger, mode: str = "enforce", **kw) -> ToolExecutor:
    return ToolExecutor(evidence_ledger=ledger, evidence_enforcement=mode, **kw)


def test_execute_enforcement_blocks_before_toolnode(monkeypatch, tmp_path: Path) -> None:
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
    ledger = EvidenceLedger("run-1")
    result = _executor(ledger, evidence_auto_read=False).execute(
        "edit_file", {"file_path": str(target), "old_text": "value = 1", "new_text": "value = 2"},
        AgentContext(), tracker=ToolExecutionTracker(),
    )
    assert result.success is False
    assert result.error == "写入前缺少目标文件读取证据: " + str(target)
    assert invoked is False
    assert len(ledger.query(kind=EventKind.TOOL_REQUEST)) == 1
    assert len(ledger.query(kind=EventKind.GATE_VERDICT)) == 1
    assert len(ledger.query(kind=EventKind.TOOL_OBSERVATION)) == 1


def test_execute_auto_read_remediates_blind_edit(monkeypatch, tmp_path: Path) -> None:
    """SWE-bench 实测修复（matplotlib-23562）：盲编辑被拦截后，
    自动补 read_file 再重试原工具 → 编辑成功。"""
    target = tmp_path / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    real_execute = ToolNode.execute

    def real_tool(self, context):
        return real_execute(self, context)

    monkeypatch.setattr("xenon.nodes.tool_executor.ToolNode.execute", real_tool)
    ledger = EvidenceLedger("run-1")
    tracker = ToolExecutionTracker()
    result = _executor(ledger).execute(  # evidence_auto_read=True（默认）
        "edit_file",
        {"file_path": str(target), "old_text": "value = 1", "new_text": "value = 2"},
        AgentContext(), tracker=tracker,
    )
    assert result.success is True, result.error
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    # 自动补读必须留下真实证据：tracker 里有 read_file + edit_file
    tools = [c.tool_name for c in tracker.calls if c.success]
    assert tools.count("read_file") >= 1
    assert "edit_file" in tools


def test_execute_records_shared_context_ledger_for_direct_user(monkeypatch) -> None:
    """未显式传 Ledger 的库调用也必须创建 Context 级统一账本。"""
    def fake_execute(self, context):
        return {"success": True, "content": "ok"}

    monkeypatch.setattr("xenon.nodes.tool_executor.ToolNode.execute", fake_execute)
    context = AgentContext()
    result = ToolExecutor().execute("read_file", {"file_path": "missing.py"}, context)
    assert result.success is True
    ledger = context.get("_evidence_ledger")
    assert isinstance(ledger, EvidenceLedger)
    assert ledger.verify_integrity()
    assert len(ledger.query(kind=EventKind.TOOL_REQUEST)) == 1
    assert len(ledger.query(kind=EventKind.TOOL_OBSERVATION)) == 1


def test_production_engines_default_to_enforce() -> None:
    from xenon.engine.plan_execute_engine import PlanExecuteEngine
    from xenon.engine.react_engine import ReActEngine

    assert ReActEngine(["mock/model"])._tool_executor.evidence_enforcement == "enforce"
    assert PlanExecuteEngine(["mock/model"], max_steps=1)._tool_executor.evidence_enforcement == "enforce"


def test_finalize_evidence_builds_delivery_pack() -> None:
    from xenon.engine.plan_execute_engine import PlanExecuteEngine

    engine = PlanExecuteEngine(["mock/model"], max_steps=1)
    context = AgentContext()
    engine._begin_run()
    engine._bind_evidence_ledger(context)
    pack = engine.finalize_evidence(context=context, output="完成")
    assert pack.integrity_verified is True
    assert pack.session_id == engine.run_id
    assert pack.event_count >= 3



def test_pre_tool_allows_edit_after_exact_read(tmp_path: Path) -> None:
    target = tmp_path / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    ledger = EvidenceLedger("run-1")
    tracker = ToolExecutionTracker()
    tracker.record("read_file", {"file_path": str(target)}, True, "value = 1")
    verdict = _executor(ledger).before_tool(
        "edit_file", {"file_path": str(target), "old_text": "value = 1", "new_text": "value = 2"},
        AgentContext(), tracker,
    )
    assert verdict.passed is True
    assert not ledger.query(kind=EventKind.GATE_VERDICT)


def test_pre_tool_blocks_blind_edit_before_node_execution(tmp_path: Path) -> None:
    target = tmp_path / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    ledger = EvidenceLedger("run-1")
    executor = _executor(ledger)
    verdict = executor.before_tool(
        "edit_file", {"file_path": str(target), "old_text": "value = 1", "new_text": "value = 2"},
        AgentContext(), ToolExecutionTracker(),
    )
    assert verdict.passed is False
    assert "读取" in verdict.reason or "盲写" in verdict.reason
    gate_events = ledger.query(phase=LifecyclePhase.PRE_TOOL, kind=EventKind.GATE_VERDICT)
    assert len(gate_events) == 1
    assert gate_events[0].payload["passed"] is False


def test_pre_tool_allows_new_file_without_prior_read(tmp_path: Path) -> None:
    target = tmp_path / "new.py"
    ledger = EvidenceLedger("run-1")
    verdict = _executor(ledger).before_tool(
        "write_file", {"file_path": str(target), "content": "value = 1\n"},
        AgentContext(), ToolExecutionTracker(),
    )
    assert verdict.passed is True


def test_observation_event_contains_sanitized_params() -> None:
    ledger = EvidenceLedger("run-1")
    executor = _executor(ledger)
    executor.record_tool_observation(
        "read_file", {"file_path": "a.py", "content": "secret"}, True,
        "source text", ToolExecutionTracker(),
    )
    events = ledger.query(kind=EventKind.TOOL_OBSERVATION)
    assert len(events) == 1
    assert events[0].payload["tool"] == "read_file"
    assert "secret" not in str(events[0].payload)
    assert ledger.verify_integrity()


def test_observe_mode_warns_but_does_not_block(tmp_path: Path) -> None:
    target = tmp_path / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    ledger = EvidenceLedger("run-1")
    verdict = _executor(ledger, "observe").before_tool(
        "edit_file", {"file_path": str(target), "old_text": "value = 1", "new_text": "value = 2"},
        AgentContext(), ToolExecutionTracker(),
    )
    assert verdict.passed is True
    gate_events = ledger.query(kind=EventKind.GATE_VERDICT)
    assert gate_events[0].payload["enforced"] is False


def test_executor_accepts_custom_gate() -> None:
    ledger = EvidenceLedger("run-1")
    executor = _executor(ledger)
    executor.register_evidence_gate(FactBindingGate())
    assert executor.evidence_gates
