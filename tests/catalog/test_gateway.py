"""Tests for the legacy ToolExecutor -> ToolReceipt adapter."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xenon.catalog.gateway import LegacyToolGateway
from xenon.catalog.specs import CORE_SPECS, LEGACY_SPECS
from xenon.kernel.contracts import (
    EffectKind,
    FsWrite,
    ReadFact,
    ReceiptStatus,
    SearchFact,
    TestRun,
    ToolCall,
    VcsCommit,
)
from xenon.nodes.tool_executor import ToolExecuteResult


class FakeExecutor:
    def __init__(self, raw: dict[str, Any], *, error: str | None = None) -> None:
        self.raw = raw
        self.error = error

    def execute(
        self,
        tool: str,
        params: dict[str, Any],
        context: Any,
        tracker: Any,
    ) -> ToolExecuteResult:
        if self.error:
            raise RuntimeError(self.error)
        return ToolExecuteResult(
            tool_name=tool,
            success=bool(self.raw.get("success", True)),
            observation="ok",
            raw=self.raw,
        )


@dataclass
class Services:
    workspace: str
    task_id: str = "t1"
    current_step_id: str | None = "s1"


def _gateway(
    tmp_path: Path, raw: dict[str, Any], *, error: str | None = None
) -> LegacyToolGateway:
    return LegacyToolGateway(
        executor=FakeExecutor(raw, error=error),
        specs=CORE_SPECS + LEGACY_SPECS,
        workspace=tmp_path,
    )


def test_the_gateway_honours_a_runtime_minted_call_id(tmp_path: Path) -> None:
    """The receipt must carry the id ``tool_started`` already recorded."""

    gateway = _gateway(tmp_path, {"success": True})
    call = ToolCall(
        spec_name="read",
        params={"file_path": str(tmp_path / "x.txt")},
        call_id="call-abc",
    )

    receipt = gateway.execute(call, Services(workspace=str(tmp_path)))

    assert receipt.call_id == "call-abc"


def test_apply_patch_becomes_fs_write_with_created_path(tmp_path: Path) -> None:
    gateway = _gateway(
        tmp_path,
        {"success": True, "changed": True, "changed_paths": ["a.py"]},
    )
    receipt = gateway.execute(
        ToolCall(
            spec_name="apply_patch",
            params={"files": [{"path": "a.py", "content": "x"}]},
        ),
        Services(workspace=str(tmp_path)),
    )
    assert receipt.status is ReceiptStatus.OK
    write = receipt.effects[0]
    assert isinstance(write, FsWrite)
    assert write.paths == ("a.py",)
    assert write.changed is True
    assert write.created_paths == ("a.py",)
    assert receipt.declared_effects == (EffectKind.FS_WRITE,)
    assert EffectKind.FS_WRITE in receipt.observed_effects


def test_apply_patch_created_path_accepts_absolute_changed_path(
    tmp_path: Path,
) -> None:
    target = str(tmp_path / "a.py")
    gateway = _gateway(
        tmp_path,
        {"success": True, "changed": True, "changed_paths": [target]},
    )
    receipt = gateway.execute(
        ToolCall(
            spec_name="apply_patch",
            params={"files": [{"path": "a.py", "content": "x"}]},
        ),
        Services(workspace=str(tmp_path)),
    )

    write = receipt.effects[0]
    assert isinstance(write, FsWrite)
    assert write.created_paths == (target,)


def test_noop_write_stays_a_noop_fact(tmp_path: Path) -> None:
    gateway = _gateway(
        tmp_path,
        {"success": True, "changed": False, "changed_paths": ["a.py"]},
    )
    receipt = gateway.execute(
        ToolCall(spec_name="apply_patch", params={"file_path": "a.py"}),
        Services(workspace=str(tmp_path)),
    )
    write = receipt.effects[0]
    assert isinstance(write, FsWrite)
    assert write.changed is False


def test_bash_pytest_becomes_test_run_with_real_target(tmp_path: Path) -> None:
    gateway = _gateway(
        tmp_path,
        {
            "success": True,
            "command": "python -m pytest tests/test_calc.py -q",
            "tests_run": 2,
            "tests_passed": 2,
            "tests_failed": 0,
            "tests_errors": 0,
            "exit_code": 0,
        },
    )
    receipt = gateway.execute(
        ToolCall(
            spec_name="bash",
            params={"command": "python -m pytest tests/test_calc.py -q"},
        ),
        Services(workspace=str(tmp_path)),
    )
    runs = [item for item in receipt.effects if isinstance(item, TestRun)]
    assert len(runs) == 1
    assert runs[0].target == "tests/test_calc.py"
    assert runs[0].tests_run == 2
    assert runs[0].passed == 2


def test_bash_git_commit_becomes_vcs_commit(tmp_path: Path) -> None:
    gateway = _gateway(
        tmp_path,
        {
            "success": True,
            "command": "git commit -m fix",
            "stdout": "[main abc1234] fix",
            "committed_paths": ["a.py"],
        },
    )
    receipt = gateway.execute(
        ToolCall(spec_name="bash", params={"command": "git commit -m fix"}),
        Services(workspace=str(tmp_path)),
    )
    commits = [item for item in receipt.effects if isinstance(item, VcsCommit)]
    assert len(commits) == 1
    assert commits[0].sha == "abc1234"
    assert commits[0].files == ("a.py",)


def test_git_status_is_not_a_commit(tmp_path: Path) -> None:
    gateway = _gateway(
        tmp_path,
        {"success": True, "command": "git status --short", "stdout": ""},
    )
    receipt = gateway.execute(
        ToolCall(spec_name="bash", params={"command": "git status --short"}),
        Services(workspace=str(tmp_path)),
    )
    assert not [item for item in receipt.effects if isinstance(item, VcsCommit)]


def test_read_becomes_read_fact_with_content_hash(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, {"success": True, "content": "hello"})
    receipt = gateway.execute(
        ToolCall(spec_name="read", params={"file_path": "a.py"}),
        Services(workspace=str(tmp_path)),
    )
    reads = [item for item in receipt.effects if isinstance(item, ReadFact)]
    assert len(reads) == 1
    assert reads[0].path == "a.py"
    assert reads[0].content_hash
    assert reads[0].content == "hello"


def test_executor_exception_becomes_a_failed_receipt(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, {}, error="boom")
    receipt = gateway.execute(
        ToolCall(spec_name="read", params={"file_path": "a.py"}),
        Services(workspace=str(tmp_path)),
    )
    assert receipt.status is ReceiptStatus.FAILED
    assert "boom" in receipt.error


def test_legacy_run_tests_receipt_uses_structured_target(tmp_path: Path) -> None:
    gateway = _gateway(
        tmp_path,
        {
            "success": True,
            "tests_run": 1,
            "tests_passed": 1,
            "exit_code": 0,
            "test_paths": ["tests/test_a.py"],
        },
    )
    receipt = gateway.execute(
        ToolCall(
            spec_name="run_tests",
            params={"test_paths": ["tests/test_a.py"]},
        ),
        Services(workspace=str(tmp_path)),
    )
    run = [item for item in receipt.effects if isinstance(item, TestRun)][0]
    assert run.target == "tests/test_a.py"


def test_injected_runtime_wins_over_the_host_default(tmp_path: Path) -> None:
    """An eval sandbox must be able to say *where* tools run."""

    from xenon.catalog.gateway import LegacyToolGateway

    class RecordingExecutor:
        def __init__(self) -> None:
            self.runtime = None

        def set_runtime(self, runtime) -> None:
            self.runtime = runtime

        def execute(self, name, params, context):  # pragma: no cover - unused
            raise AssertionError("not executed")

    executor = RecordingExecutor()
    injected = object()

    LegacyToolGateway(
        executor=executor,  # type: ignore[arg-type]
        workspace=tmp_path,
        runtime=injected,
    )

    assert executor.runtime is injected, "注入的 runtime 必须覆盖宿主默认"


def test_gateway_without_injection_binds_a_host_runtime(tmp_path: Path) -> None:
    from xenon.catalog.gateway import LegacyToolGateway

    class RecordingExecutor:
        def __init__(self) -> None:
            self.runtime = None

        def set_runtime(self, runtime) -> None:
            self.runtime = runtime

    executor = RecordingExecutor()

    LegacyToolGateway(executor=executor, workspace=tmp_path)  # type: ignore[arg-type]

    assert executor.runtime is not None
    assert getattr(executor.runtime, "workspace_root", None) == tmp_path.resolve()


def test_search_results_become_a_typed_fact(tmp_path: Path) -> None:
    """#432: a search that ran must receipt what it found, not just ``ok``.

    The receipt is the only thing the next step reads; a match list that lives
    only in the panel leaves "how many matches, in which files" unanswerable.
    """

    gateway = _gateway(
        tmp_path,
        {
            "success": True,
            "action_type": "search_files",
            "pattern": "mcpServers",
            "path": str(tmp_path),
            "matches": [
                {"file": "a.json", "line": 3, "content": '"mcpServers": {}'},
                {"file": "b.json", "line": 7, "content": '"mcpServers": {}'},
            ],
            "match_count": 9,
            "returned_count": 2,
            "files_scanned": 12,
            "truncated": True,
            "next_cursor": "2",
        },
    )

    receipt = gateway.execute(
        ToolCall(spec_name="search", params={"pattern": "mcpServers"}),
        Services(workspace=str(tmp_path)),
    )

    assert receipt.status is ReceiptStatus.OK
    facts = [item for item in receipt.effects if isinstance(item, SearchFact)]
    assert len(facts) == 1
    fact = facts[0]
    assert fact.query == "mcpServers"
    assert fact.count == 9, "全量命中数必须进回执，而不是只留本页条数"
    assert [hit["file"] for hit in fact.matches] == ["a.json", "b.json"]
    assert fact.truncated is True
    assert fact.scanned == 12
    assert receipt.declared_effects == (EffectKind.SEARCH,)
    assert receipt.observed_effects == (EffectKind.SEARCH,)


def test_search_fact_survives_the_ledger_payload(tmp_path: Path) -> None:
    from xenon.ledger.events import effect_from_payload, effect_to_payload

    gateway = _gateway(
        tmp_path,
        {
            "success": True,
            "matches": [{"file": "a.json", "line": 1, "content": "hit"}],
            "match_count": 1,
            "files_scanned": 3,
        },
    )
    receipt = gateway.execute(
        ToolCall(spec_name="search", params={"pattern": "hit"}),
        Services(workspace=str(tmp_path)),
    )
    fact = next(item for item in receipt.effects if isinstance(item, SearchFact))

    restored = effect_from_payload(effect_to_payload(fact))

    assert restored == fact


def test_search_without_a_pattern_is_a_listing_fact(tmp_path: Path) -> None:
    """``search`` degrades to a directory listing; the count still matters."""

    gateway = _gateway(
        tmp_path,
        {
            "success": True,
            "action_type": "list_files",
            "path": str(tmp_path),
            "files": ["a.py", "b.py"],
            "count": 2,
        },
    )
    receipt = gateway.execute(
        ToolCall(spec_name="search", params={"path": "."}),
        Services(workspace=str(tmp_path)),
    )

    fact = next(item for item in receipt.effects if isinstance(item, SearchFact))
    assert fact.count == 2
    assert [hit["file"] for hit in fact.matches] == ["a.py", "b.py"]
    assert "listing" in fact.note


def test_a_search_that_found_nothing_still_receipts_a_zero(tmp_path: Path) -> None:
    gateway = _gateway(
        tmp_path,
        {
            "success": True,
            "matches": [],
            "match_count": 0,
            "files_scanned": 4,
        },
    )
    receipt = gateway.execute(
        ToolCall(spec_name="search", params={"pattern": "nope"}),
        Services(workspace=str(tmp_path)),
    )

    fact = next(item for item in receipt.effects if isinstance(item, SearchFact))
    assert fact.count == 0
    assert fact.matches == ()
    assert receipt.observed_effects == (EffectKind.SEARCH,)


def test_search_fact_bounds_hits_and_normalizes_strings(tmp_path: Path) -> None:
    """A broad pattern cannot fill a ledger event, and hits are never bare strings."""

    long_line = "x" * 500
    gateway = _gateway(
        tmp_path,
        {
            "success": True,
            "matches": ["bare.py"]
            + [
                {"file": f"f{index}.py", "line": index + 1, "content": long_line}
                for index in range(60)
            ],
            "match_count": 61,
        },
    )
    receipt = gateway.execute(
        ToolCall(spec_name="search", params={"pattern": "x"}),
        Services(workspace=str(tmp_path)),
    )

    fact = next(item for item in receipt.effects if isinstance(item, SearchFact))
    assert len(fact.matches) == 50, "回执保留的命中必须有上限"
    assert fact.count == 61, "上限只作用在保留条数，不作用在全量计数"
    assert fact.matches[0] == {"file": "bare.py"}
    assert len(fact.matches[1]["content"]) == 300


def test_search_note_carries_every_caveat_the_tool_reported(
    tmp_path: Path,
) -> None:
    gateway = _gateway(
        tmp_path,
        {
            "success": True,
            "matches": [{"file": "a.py", "line": 1, "content": "x"}],
            "match_count": 900,
            "files_scanned": 2000,
            "scan_truncated": True,
            "skipped_large": 3,
            "next_cursor": "1",
        },
    )
    receipt = gateway.execute(
        ToolCall(spec_name="search", params={"pattern": "x"}),
        Services(workspace=str(tmp_path)),
    )

    fact = next(item for item in receipt.effects if isinstance(item, SearchFact))
    assert fact.truncated is True
    assert "stopped early" in fact.note
    assert "3 file(s) above the size limit" in fact.note
    assert "more matches exist" in fact.note
