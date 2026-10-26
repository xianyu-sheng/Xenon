"""Gateway-level policy tests: bound spec closes the capability-only blind spot."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xenon.catalog.gateway import LegacyToolGateway
from xenon.kernel.contracts import (
    Capability,
    EffectKind,
    ReceiptStatus,
    Risk,
    ToolCall,
    ToolSpec,
)
from xenon.kernel.policies import PermissionPolicy
from xenon.nodes.tool_executor import ToolExecuteResult


class FakeExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def execute(
        self, tool: str, params: dict[str, Any], context: Any, tracker: Any
    ) -> ToolExecuteResult:
        self.calls += 1
        return ToolExecuteResult(
            tool_name=tool,
            success=True,
            observation="ok",
            raw={"success": True, "content": "hello"},
        )


@dataclass
class Services:
    workspace: str
    task_id: str = "t1"
    current_step_id: str | None = None


READ = ToolSpec(
    name="read",
    pack="fake",
    capabilities=frozenset({Capability.READ}),
    effects=frozenset({EffectKind.READ}),
    risk=Risk.INFO,
)
SNEAKY = ToolSpec(
    name="sneaky",
    pack="fake",
    capabilities=frozenset({Capability.EDIT}),
    effects=frozenset({EffectKind.NETWORK}),
    risk=Risk.SENSITIVE,
)
DELETE = ToolSpec(
    name="remove",
    pack="fake",
    capabilities=frozenset({Capability.EDIT}),
    effects=frozenset({EffectKind.FS_DELETE}),
    risk=Risk.WRITE,
)
BASH_LIKE = ToolSpec(
    name="multi",
    pack="fake",
    capabilities=frozenset({Capability.SHELL, Capability.VERIFY}),
    effects=frozenset({EffectKind.OPAQUE, EffectKind.TEST_RUN}),
    risk=Risk.SENSITIVE,
)


def _gateway(tmp_path: Path, specs: tuple[ToolSpec, ...]) -> tuple[LegacyToolGateway, FakeExecutor]:
    executor = FakeExecutor()
    gateway = LegacyToolGateway(
        executor=executor,
        specs=specs,
        workspace=tmp_path,
        policy=PermissionPolicy(workspace=str(tmp_path)),
    )
    return gateway, executor


def test_matching_bound_capability_is_allowed(tmp_path: Path) -> None:
    gateway, executor = _gateway(tmp_path, (READ,))
    receipt = gateway.execute(
        ToolCall(
            spec_name="read",
            params={"file_path": "a.py"},
            capability=Capability.READ,
        ),
        Services(str(tmp_path)),
    )
    assert receipt.status is ReceiptStatus.OK
    assert executor.calls == 1


def test_sneaky_catalog_mapping_is_denied_before_execution(tmp_path: Path) -> None:
    gateway, executor = _gateway(tmp_path, (SNEAKY,))
    receipt = gateway.execute(
        ToolCall(
            spec_name="sneaky",
            params={"file_path": "a.py"},
            capability=Capability.EDIT,
        ),
        Services(str(tmp_path)),
    )

    assert receipt.status is ReceiptStatus.DENIED
    assert "network effect" in receipt.error
    assert receipt.effects == ()
    assert executor.calls == 0


def test_delete_effect_requires_a_scoped_grant(tmp_path: Path) -> None:
    gateway, executor = _gateway(tmp_path, (DELETE,))
    receipt = gateway.execute(
        ToolCall(
            spec_name="remove",
            params={"path": "a.py"},
            capability=Capability.EDIT,
        ),
        Services(str(tmp_path)),
    )
    assert receipt.status is ReceiptStatus.DENIED
    assert "delete effect" in receipt.error
    assert executor.calls == 0


def test_ambiguous_bound_capability_is_denied(tmp_path: Path) -> None:
    gateway, executor = _gateway(tmp_path, (BASH_LIKE,))
    receipt = gateway.execute(
        ToolCall(spec_name="multi", params={"command": "pytest -q"}),
        Services(str(tmp_path)),
    )

    assert receipt.status is ReceiptStatus.DENIED
    assert "ambiguous" in receipt.error
    assert executor.calls == 0


def test_catalog_capability_mismatch_is_denied(tmp_path: Path) -> None:
    gateway, executor = _gateway(tmp_path, (READ,))
    receipt = gateway.execute(
        ToolCall(
            spec_name="read",
            params={"file_path": "a.py"},
            capability=Capability.EDIT,
        ),
        Services(str(tmp_path)),
    )

    assert receipt.status is ReceiptStatus.DENIED
    assert "does not advertise" in receipt.error
    assert executor.calls == 0


def test_unknown_spec_is_denied_when_policy_is_present(tmp_path: Path) -> None:
    gateway, executor = _gateway(tmp_path, ())
    receipt = gateway.execute(
        ToolCall(
            spec_name="missing",
            params={},
            capability=Capability.READ,
        ),
        Services(str(tmp_path)),
    )

    assert receipt.status is ReceiptStatus.DENIED
    assert "unknown tool spec" in receipt.error
    assert executor.calls == 0
