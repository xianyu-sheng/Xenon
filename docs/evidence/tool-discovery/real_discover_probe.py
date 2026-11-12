"""Real discovery probe: find_tools prevents blind tool guessing (defect 6).

A real API run against a catalog whose extra tools have **cryptic names** and
index-only one-liners — the progressive-loading shape.  The task requires one
specific tool.  A well-oriented model should call ``find_tools`` once, read the
candidate's full parameters, and then bind the right tool — instead of guessing
cryptic names and burning calls on UNRESOLVED bindings.

We do not provide executors for the cryptic tools: the probe ends at the moment
the model names the correct tool, which is the behavior under test.  Every model
call hits the real endpoint.

Reproduce:

    PYTHONIOENCODING=utf-8 python docs/evidence/tool-discovery/real_discover_probe.py \
        deepseek/deepseek-chat
"""

from __future__ import annotations

import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, r"D:\Xenon")

from xenon.catalog.catalog import ToolCatalog, build_catalog
from xenon.catalog.gateway import LegacyToolGateway
from xenon.harness.llm import ModelConfig, build_llm_loop_strategy
from xenon.harness.runner import _bind_accounting
from xenon.kernel.contracts import (
    Budget,
    Capability,
    EffectKind,
    Risk,
    TaskSpec,
    ToolSpec,
)
from xenon.kernel.policies import PermissionMode, PermissionPolicy
from xenon.ledger.events import EventKind

MODEL = sys.argv[1] if len(sys.argv) > 1 else "deepseek/deepseek-chat"

#: Cryptic tools: the name gives nothing away; only the description/params say
#: which intent each serves — exactly the progressive-loading blind spot.
_EXTRA_SPECS = (
    ToolSpec(
        name="zpx_alpha",
        pack="plugin",
        capabilities=frozenset({Capability.REMOTE}),
        effects=frozenset({EffectKind.NETWORK}),
        risk=Risk.SENSITIVE,
        params_schema={"endpoint_id": "target endpoint", "region": "region code"},
        description="Convert a markdown document into a PDF file.",
    ),
    ToolSpec(
        name="zpx_beta",
        pack="plugin",
        capabilities=frozenset({Capability.REMOTE}),
        effects=frozenset({EffectKind.NETWORK}),
        risk=Risk.SENSITIVE,
        params_schema={"image_id": "source image"},
        description="Resize an image to a given width and height.",
    ),
    ToolSpec(
        name="zpx_gamma",
        pack="plugin",
        capabilities=frozenset({Capability.REMOTE}),
        effects=frozenset({EffectKind.NETWORK}),
        risk=Risk.SENSITIVE,
        params_schema={"repo_url": "repository url"},
        description="List the open pull requests of a repository.",
    ),
)

TARGET_TOOL = "zpx_alpha"


def main() -> int:
    from xenon.kernel.runtime import LoopRuntime
    from xenon.kernel.strategies import LoopProfile

    tmp = Path(tempfile.mkdtemp())
    base = build_catalog("core")
    merged = tuple(base.specs("all")) + _EXTRA_SPECS
    catalog = ToolCatalog(merged, "all")
    policy = PermissionPolicy(mode=PermissionMode.BYPASS, workspace=str(tmp))
    real_gateway = LegacyToolGateway(
        specs=catalog.specs("all"),
        workspace=tmp,
        policy=policy,
        catalog=catalog,
    )
    gateway = _RecordingGateway(real_gateway, _EXTRA_NAMES)

    from xenon.kernel.controller import VerificationController
    from xenon.ledger.store import Ledger

    runtime = LoopRuntime(
        catalog=catalog,
        gateway=gateway,
        controller=VerificationController(),
        ledger=Ledger.in_memory("discovery-probe"),
        policies=(policy,),
    )
    config = ModelConfig(model_id=MODEL)
    strategy = build_llm_loop_strategy(config, LoopProfile())
    _bind_accounting(strategy, runtime)

    spec = TaskSpec(
        task_id="discovery-probe",
        objective=(
            "Turn the file report.md into a PDF. Before calling any tool "
            "whose name you are unsure about, use find_tools to inspect it. "
            "Then call the correct tool. You do not need the PDF to actually "
            "exist — naming and calling the right tool with correct params is "
            "enough."
        ),
        workspace=str(tmp),
        budget=Budget(max_steps=12),
    )

    result = runtime.run(spec, strategy)
    calls = gateway.calls

    print(f"phase={result.phase.value} steps={int(result.metrics['steps'])}")
    print("tool calls in order:")
    for index, entry in enumerate(calls, 1):
        print(f"  {index}. {entry}")

    used_find = any(entry == "find_tools" for entry in calls)
    called_target = any(entry == TARGET_TOOL for entry in calls)
    print(f"used find_tools: {used_find}; called the correct tool {TARGET_TOOL}: {called_target}")
    ok = used_find and called_target
    print("PASS: the model discovered before binding" if ok else "NOTE: the model did not use the discover-then-bind path")
    return 0 if ok else 1


_EXTRA_NAMES = {spec.name for spec in _EXTRA_SPECS}


class _RecordingGateway:
    """Record call order; short-circuit cryptic tools as completed intents."""

    def __init__(self, inner, extra_names) -> None:
        self._inner = inner
        self._extra_names = extra_names
        self.calls: list[str] = []

    def execute(self, call, services):
        self.calls.append(call.spec_name)
        if call.spec_name in self._extra_names:
            from xenon.kernel.contracts import (
                NetworkFact,
                ReceiptStatus,
                ToolReceipt,
            )

            return ToolReceipt(
                receipt_id=f"called-{call.spec_name}",
                call_id=call.spec_name,
                task_id="discovery-probe",
                tool=call.spec_name,
                status=ReceiptStatus.OK,
                effects=(NetworkFact(url="", status=200),),
            )
        return self._inner.execute(call, services)


if __name__ == "__main__":
    raise SystemExit(main())
