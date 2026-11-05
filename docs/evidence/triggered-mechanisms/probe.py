"""Exercise the three mechanisms an ordinary run never touches (#422).

The auditor reports `n/a` for mechanisms a task did not need — which is honest
but useless as evidence.  This script *triggers* them deterministically (no
model, no network) and writes a real ledger for each, so the audit output stops
being "not exercised" and becomes "ran, here is the event":

1. **failure → teaching**: a tool fails, the run continues, and the advice is
   in the ledger (not just in memory);
2. **loop → force finish**: the same call five times closes the tools, allows
   exactly one more call, then stops;
3. **approval → execution**: a shell escalation asks the human, the human says
   yes, and the call actually runs.

Usage (from the repo root):
    python docs/evidence/triggered-mechanisms/probe.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from xenon.kernel.contracts import (  # noqa: E402
    CallTool,
    Capability,
    Finish,
    ReceiptStatus,
    RequestEscalation,
    TaskSpec,
    ToolIntent,
    ToolReceipt,
)
from xenon.kernel.controller import VerificationController  # noqa: E402
from xenon.kernel.policies import PermissionMode, PermissionPolicy  # noqa: E402
from xenon.kernel.runtime import LoopRuntime  # noqa: E402
from xenon.ledger.store import Ledger  # noqa: E402

OUT = Path(__file__).resolve().parent


class ScriptedStrategy:
    def __init__(self, actions):
        self.actions = list(actions)

    def propose(self, view):
        return self.actions.pop(0) if self.actions else Finish("done")


class FakeCatalog:
    def __init__(self, specs):
        self._specs = tuple(specs)

    def specs(self, profile: str = "core"):
        return self._specs

    def bind(self, intent):
        from xenon.kernel.contracts import (
            BindingStatus,
            ToolBinding,
            ToolCall,
        )

        for spec in self._specs:
            if intent.capability in spec.capabilities:
                return ToolBinding(
                    status=BindingStatus.BOUND,
                    call=ToolCall(
                        spec_name=spec.name,
                        params=dict(intent.params),
                        capability=intent.capability,
                    ),
                )
        return ToolBinding(
            status=BindingStatus.UNRESOLVED, reason="no tool for capability"
        )


class Gateway:
    """Fails the first call, then succeeds; records what it executed."""

    def __init__(self, *, fail_first: bool = False):
        self.fail_first = fail_first
        self.calls: list[str] = []

    def execute(self, call, services):
        self.calls.append(call.spec_name)
        failed = self.fail_first and len(self.calls) == 1
        return ToolReceipt(
            receipt_id=f"r{len(self.calls)}",
            call_id=f"c{len(self.calls)}",
            task_id="probe",
            tool=call.spec_name,
            status=ReceiptStatus.FAILED if failed else ReceiptStatus.OK,
            effects=(),
            declared_effects=(),
            error="[exec_read] read_file 需要 file_path" if failed else "",
        )


def _specs():
    from xenon.kernel.contracts import EffectKind, Risk, ToolSpec

    return (
        ToolSpec(
            name="inspect",
            pack="core",
            capabilities=frozenset({Capability.READ}),
            effects=frozenset({EffectKind.READ}),
            risk=Risk.INFO,
        ),
        ToolSpec(
            name="shell",
            pack="core",
            capabilities=frozenset({Capability.SHELL}),
            effects=frozenset({EffectKind.OPAQUE}),
            risk=Risk.SENSITIVE,
        ),
    )


def _runtime(ledger: Ledger, *, gateway: Gateway, policies=()):
    return LoopRuntime(
        catalog=FakeCatalog(_specs()),
        gateway=gateway,
        controller=VerificationController(),
        ledger=ledger,
        policies=policies,
    )


def _spec(workspace: Path, task_id: str) -> TaskSpec:
    # A deterministic probe never calls a model; the marker lets the auditor
    # say "not applicable" instead of reporting a missing usage record.
    return TaskSpec(
        task_id=task_id,
        objective="probe",
        workspace=str(workspace),
        constraints=("probe:scripted",),
    )


def scenario_failure_teaching(tmp: Path) -> Path:
    ledger = Ledger(tmp / "probe-failure.jsonl", "probe-failure")
    runtime = _runtime(ledger, gateway=Gateway(fail_first=True))
    runtime.run(
        _spec(tmp, "probe-failure"),
        ScriptedStrategy(
            [
                CallTool(ToolIntent(capability=Capability.READ)),
                Finish("answered with what I have"),
            ]
        ),
    )
    return ledger.path


def scenario_loop_finish(tmp: Path) -> Path:
    ledger = Ledger(tmp / "probe-loop.jsonl", "probe-loop")
    runtime = _runtime(ledger, gateway=Gateway())
    call = CallTool(ToolIntent(capability=Capability.READ))
    runtime.run(
        _spec(tmp, "probe-loop"),
        ScriptedStrategy([call, call, call, call, call, call, Finish("done")]),
    )
    return ledger.path


class YesApprover:
    """The runtime calls ``request(view, action, decision)`` and reads
    ``last_scope`` afterwards."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.last_scope = "once"

    def request(self, view, action, decision):
        self.asked.append(str(getattr(decision, "reason", "")))
        return True


def scenario_approval(tmp: Path) -> Path:
    ledger = Ledger(tmp / "probe-approval.jsonl", "probe-approval")
    policy = PermissionPolicy(
        workspace=str(tmp),
        mode=PermissionMode.AUTO_EDIT,
        allow_approval=True,
    )
    runtime = _runtime(ledger, gateway=Gateway(), policies=(policy,))
    runtime._approval = YesApprover()
    runtime.run(
        _spec(tmp, "probe-approval"),
        ScriptedStrategy(
            [
                RequestEscalation(
                    ToolIntent(
                        capability=Capability.SHELL, params={"command": "echo hi"}
                    ),
                    "need a shell",
                ),
                CallTool(
                    ToolIntent(
                        capability=Capability.SHELL, params={"command": "echo hi"}
                    )
                ),
                Finish("done"),
            ]
        ),
    )
    return ledger.path


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="xenon-probe-"))
    produced: list[tuple[str, Path]] = []
    for name, fn in (
        ("failure", scenario_failure_teaching),
        ("loop", scenario_loop_finish),
        ("approval", scenario_approval),
    ):
        try:
            path = fn(tmp)
        except Exception as exc:  # noqa: BLE001 — a probe that crashes is a result
            print(f"{name}: ERROR {type(exc).__name__}: {exc}")
            continue
        target = OUT / path.name
        shutil.copyfile(path, target)
        kinds: dict[str, int] = {}
        for line in target.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            kind = str(json.loads(line).get("kind"))
            kinds[kind] = kinds.get(kind, 0) + 1
        produced.append((name, target))
        print(f"{name}: {target.name} -> {kinds}")
    print("\nledgers:", ", ".join(path.name for _, path in produced))
    print(f"tmp: {tmp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
