"""#165 dogfood: the view states the execution boundary the policies enforce."""
from __future__ import annotations

from tests.kernel.test_north_star import FakeCatalog
from xenon.kernel.contracts import TaskSpec
from xenon.kernel.policies import PermissionMode, PermissionPolicy
from xenon.kernel.view import build_model_view
from xenon.ledger.store import Ledger


def _view(policy, tmp_path):
    ledger = Ledger.in_memory("b")
    return build_model_view(
        spec=TaskSpec(task_id="b", objective="o", workspace=str(tmp_path)),
        catalog=FakeCatalog(),
        ledger=ledger,
        boundary=policy.describe_boundary(),
    )


def test_aut_edit_boundary_says_shell_triggers_an_approval(tmp_path) -> None:
    policy = PermissionPolicy(
        mode=PermissionMode.AUTO_EDIT, workspace=str(tmp_path), allow_approval=True
    )
    text = _view(policy, tmp_path).system
    assert "Execution boundary:" in text
    assert "human approval prompt" in text
    assert "proposing them is allowed" in text
    assert "push is never approvable" in text
    assert "reads anywhere are free" in text


def test_non_interactive_boundary_says_refused(tmp_path) -> None:
    policy = PermissionPolicy(
        mode=PermissionMode.AUTO_EDIT, workspace=str(tmp_path), allow_approval=False
    )
    text = _view(policy, tmp_path).system
    assert "refused in this non-interactive run" in text


def test_read_only_boundary_has_no_execution(tmp_path) -> None:
    policy = PermissionPolicy(
        mode=PermissionMode.READ_ONLY, workspace=str(tmp_path), allow_approval=False
    )
    boundary = policy.describe_boundary()
    assert "read-only" in boundary
    assert "no edits" in boundary
    assert "no execution" in boundary
    # A read-only turn never asks for an execution; approval is for reads only.
    assert "human approval" not in boundary


def test_bypass_boundary_still_refuses_push(tmp_path) -> None:
    policy = PermissionPolicy(mode=PermissionMode.BYPASS, workspace=str(tmp_path))
    boundary = policy.describe_boundary()
    assert "bypass" in boundary and "push is still refused" in boundary


def test_boundary_changes_the_prefix_digest(tmp_path) -> None:
    ledger = Ledger.in_memory("d")
    a = build_model_view(
        spec=TaskSpec(task_id="d", objective="o", workspace=str(tmp_path)),
        catalog=FakeCatalog(), ledger=ledger,
        boundary=PermissionPolicy(mode=PermissionMode.READ_ONLY).describe_boundary(),
    )
    b = build_model_view(
        spec=TaskSpec(task_id="d", objective="o", workspace=str(tmp_path)),
        catalog=FakeCatalog(), ledger=ledger,
        boundary=PermissionPolicy(mode=PermissionMode.AUTO_EDIT).describe_boundary(),
    )
    assert a.prefix_digest != b.prefix_digest
