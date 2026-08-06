"""跨层 EvidenceRuntime 门面测试（REPL/Engine/ToolExecutor/Node/MCP/Session 统一入口）。"""
from __future__ import annotations

from pathlib import Path

from xenon.engine.context import AgentContext
from xenon.engine.evidence_gate import FileClaimGate, GateVerdict

from xenon.engine.evidence_runtime import (
    EvidenceLedger,
    EvidencePack,
    EvidenceRuntime,
    EventKind,
    LifecyclePhase,
)
from xenon.nodes.tool_executor import ToolExecutor


class TestContextFacade:
    def test_context_evidence_lazily_creates_runtime(self) -> None:
        ctx = AgentContext()
        rt = ctx.evidence
        assert isinstance(rt, EvidenceRuntime)
        assert ctx.evidence is rt  # 同一实例

    def test_bind_evidence_reuses_ledger(self) -> None:
        ctx = AgentContext()
        ledger = EvidenceLedger("run-abc")
        ctx.bind_evidence(ledger)
        assert ctx.evidence.ledger is ledger
        assert ctx.evidence.ledger.session_id == "run-abc"

    def test_evidence_never_serialized_into_store(self) -> None:
        ctx = AgentContext()
        _ = ctx.evidence
        store = ctx.to_dict()
        assert "_evidence_runtime" not in store
        assert "evidence" not in store


class TestEvidenceRuntimeRecords:
    def test_start_task_records_task_fact(self) -> None:
        rt = EvidenceRuntime()
        event = rt.start_task(engine="react", user_input="修复 bug")
        assert event.kind == EventKind.TASK_FACT.value
        assert event.phase == LifecyclePhase.TASK.value
        assert event.payload["engine"] == "react"
        assert rt.ledger.verify_integrity()

    def test_record_claim_observation_verdict_validation(self) -> None:
        rt = EvidenceRuntime()
        rt.start_task(engine="plan-execute", user_input="x")
        rt.record_claim(text="我读完了")
        rt.record_tool_request(tool="read_file", params={"file_path": "a.py"})
        rt.record_tool_observation(tool="read_file", params={"file_path": "a.py"}, success=True, summary="ok")
        rt.record_gate_verdict(gate="FactBindingGate", passed=True, reason="通过", phase="pre_tool")
        rt.record_validation(kind="pytest", passed=True, detail="3 passed")
        kinds = [e.kind for e in rt.ledger.events]
        assert kinds == [
            EventKind.TASK_FACT.value,
            EventKind.CLAIM.value,
            EventKind.TOOL_REQUEST.value,
            EventKind.TOOL_OBSERVATION.value,
            EventKind.GATE_VERDICT.value,
            EventKind.VALIDATION_RESULT.value,
        ]

    def test_query_filters(self) -> None:
        rt = EvidenceRuntime()
        rt.start_task(engine="react", user_input="x")
        rt.record_tool_request(tool="read_file", params={})
        assert len(rt.query(kind=EventKind.TOOL_REQUEST)) == 1
        assert len(rt.query(phase=LifecyclePhase.TASK)) == 1


class TestEvidenceRuntimeGates:
    def test_register_and_run_gates(self) -> None:
        rt = EvidenceRuntime()
        rt.register_gate(FileClaimGate())
        verdicts = rt.run_gates("output", None, output="已创建 a.py")
        assert isinstance(verdicts, list)
        assert all(isinstance(v, GateVerdict) for v in verdicts)

    def test_gate_failed_returns_first_rejection(self) -> None:
        rt = EvidenceRuntime()

        class AlwaysRejectGate:
            phase = "output"

            def check(self, ctx, **kwargs):
                return GateVerdict("output", False, "确定性拒绝", "error")

        rt.register_gate(AlwaysRejectGate())
        verdict = rt.gate_failed("output", None, output="x")
        assert verdict is not None
        assert verdict.passed is False


class TestEvidenceRuntimeFinalize:
    def test_finalize_builds_pack(self) -> None:
        rt = EvidenceRuntime()
        rt.start_task(engine="react", user_input="x")
        pack = rt.finalize(output="完成")
        assert isinstance(pack, EvidencePack)
        assert pack.integrity_verified is True
        assert pack.event_count == rt.ledger.events.__len__()
        # delivery 事件已记录
        assert any(e.kind == EventKind.DELIVERY.value for e in rt.ledger.events)

    def test_finalize_is_idempotent(self) -> None:
        rt = EvidenceRuntime()
        rt.start_task(engine="react", user_input="x")
        first = rt.finalize(output="完成")
        second = rt.finalize(output="完成")
        assert second.event_count == first.event_count  # 不重复记录 delivery

    def test_persist_and_load_roundtrip(self, tmp_path: Path) -> None:
        rt = EvidenceRuntime()
        rt.start_task(engine="react", user_input="x")
        rt.record_tool_observation(tool="read_file", params={}, success=True, summary="s")
        path = tmp_path / "ev.jsonl"
        rt.persist(path)
        restored = EvidenceRuntime.load(path)
        assert restored.ledger.session_id == rt.ledger.session_id
        assert restored.ledger.verify_integrity()
        assert len(restored.ledger.events) == len(rt.ledger.events)


class TestToolExecutorViaContext:
    def test_execute_records_into_context_runtime(self, monkeypatch) -> None:
        def fake_execute(self, context):
            return {"success": True, "content": "ok"}

        monkeypatch.setattr("xenon.nodes.tool_executor.ToolNode.execute", fake_execute)
        ctx = AgentContext()
        rt = ctx.evidence
        result = ToolExecutor().execute("read_file", {"file_path": "a.py"}, ctx)
        assert result.success is True
        # request + observation 都进入同一 runtime
        assert len(rt.query(kind=EventKind.TOOL_REQUEST)) == 1
        assert len(rt.query(kind=EventKind.TOOL_OBSERVATION)) == 1
        assert rt.ledger.verify_integrity()


class TestMCPEvidence:
    def test_registry_call_tool_with_context_records_evidence(self) -> None:
        from xenon.mcp.registry import MCPRegistry

        registry = MCPRegistry.__new__(MCPRegistry)
        registry.tool_map = {"srv:fetch": ("srv", {"name": "fetch"})}
        registry.clients = {}

        class FakeClient:
            def call_tool(self, name, arguments=None):
                return {"content": [{"type": "text", "text": "ok"}]}

        registry.clients["srv"] = FakeClient()
        ctx = AgentContext()
        result = registry.call_tool("srv:fetch", {"url": "https://x"}, context=ctx)
        assert result == {"content": [{"type": "text", "text": "ok"}]}
        rt = ctx.evidence
        assert len(rt.query(kind=EventKind.TOOL_REQUEST)) == 1
        assert len(rt.query(kind=EventKind.TOOL_OBSERVATION)) == 1


class TestSessionEvidence:
    def test_save_session_embeds_evidence_snapshot(self, tmp_path: Path, monkeypatch) -> None:
        import xenon.repl.session as session_mod

        monkeypatch.setattr(session_mod, "SESSIONS_DIR", tmp_path)
        ctx = AgentContext()
        ctx.evidence.start_task(engine="react", user_input="x")
        path = session_mod.save_session(
            "s1",
            [{"role": "user", "content": "x"}],
            ctx.to_dict(),
            {"model": "m"},
            extra={"evidence": ctx.evidence.ledger.snapshot()},
        )
        assert path.exists()
        data = session_mod.load_session("s1")
        assert data["extra"]["evidence"]["session_id"] == ctx.evidence.ledger.session_id
