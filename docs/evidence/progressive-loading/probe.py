"""Real probe: progressive loading (#370) with a real model.

A catalog with 10 MCP-shaped tools, one real objective.  Measures what #370
actually buys:

    - prompt bytes for the tools section: index rows vs fully-expanded schemas
    - tool calls the model makes on the real task

Reproduce:

    PYTHONIOENCODING=utf-8 python docs/evidence/progressive-loading/probe.py \\
        deepseek/deepseek-v4-flash
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, r"D:\Xenon")

from xenon.catalog.catalog import build_catalog
from xenon.catalog.specs import mcp_tool_specs
from xenon.harness.llm import ModelConfig
from xenon.kernel.contracts import TaskMode, TaskSpec, Budget
from xenon.kernel.view import build_model_view
from xenon.ledger.events import EventKind
from xenon.ledger.store import Ledger
from xenon.repl.runtime_adapter import RuntimeAdapter

MODEL = sys.argv[1] if len(sys.argv) > 1 else "deepseek/deepseek-v4-flash"

# 10 discovered MCP tools, each with a real parameter schema.
_TOOLS = {
    f"github:tool_{i:02d}": (
        "github",
        {
            "name": f"tool_{i:02d}",
            "description": f"GitHub operation {i:02d} with several parameters",
            "parameters": {
                "properties": {
                    "repo": {"type": "string", "description": "owner/name"},
                    "query": {"type": "string", "description": "search text"},
                    "limit": {"type": "integer", "description": "result cap"},
                    "cursor": {"type": "string", "description": "pagination"},
                },
                "required": ["repo"],
            },
        },
    )
    for i in range(10)
}


class _Registry:
    tool_map = _TOOLS

    def discover_tools(self) -> None:
        pass


def repo() -> Path:
    root = Path("/tmp/pl-" + uuid.uuid4().hex[:6])
    root.mkdir(parents=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=x@y", "-c", "user.name=x", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )
    return root


def _spec(root: Path, objective: str) -> TaskSpec:
    return TaskSpec(
        task_id="progressive-load",
        objective=objective,
        workspace=str(root),
        mode=TaskMode.AUTO,
        budget=Budget(max_tokens=4000, max_steps=20),
    )


class _AutoApproval:
    def __init__(self) -> None:
        self.last_scope = ""
        self.last_reason = "probe auto-approval"

    def request(self, view, action, decision) -> bool:
        return True


def main() -> int:
    root = repo()
    specs = mcp_tool_specs(_TOOLS)
    catalog = build_catalog("core+mcp", extra_specs=specs)

    # Real model, real task: how many MCP tools does it actually call?
    adapter = RuntimeAdapter(
        workspace=root,
        model_config=ModelConfig(model_id=MODEL),
        ledger_dir=Path("/tmp/pll-" + uuid.uuid4().hex[:6]),
        finalize=False,
        mcp_registry=_Registry(),
        approval=_AutoApproval(),
        allow_approval=True,
        max_steps=10,
    )
    run = adapter.run(
        "Use the github tools to list repositories matching 'xenon harness' "
        "and report the first result name.",
        task_id="progressive-load",
    )
    ledger = Ledger(run.ledger_path, "progressive-load")
    receipts = [
        event.payload["receipt"]["tool"]
        for event in ledger.read()
        if event.kind is EventKind.TOOL_RECEIPT
    ]

    # Same ledger, two view styles: the old full-expansion vs the index.
    view = build_model_view(
        spec=_spec(root, "query github for xenon harness"),
        catalog=catalog,
        ledger=ledger,
    )
    progressive_bytes = len(json.dumps(view.tools, ensure_ascii=False))
    expanded_tools = [
        {**tool, "parameters": {"repo": "owner/name", "query": "text", "limit": "int", "cursor": "pg"}}
        for tool in view.tools
        if tool.get("index")
    ]
    full_tools = [
        tool for tool in view.tools if not tool.get("index")
    ] + expanded_tools
    old_bytes = len(json.dumps(full_tools, ensure_ascii=False))

    mcp_index_rows = [
        tool for tool in view.tools
        if tool.get("index") and tool["name"].startswith(("github:", "mcp:"))
    ]
    mcp_expanded_rows = [
        tool for tool in view.tools
        if not tool.get("index") and tool.get("parameters")
        and tool["name"].startswith(("github:", "mcp:"))
    ]
    core_rows = [
        tool for tool in view.tools if not tool.get("index")
    ]

    print(f"task phase: {run.result.phase.value}  accepted: {run.result.accepted}")
    print(f"tools section bytes (progressive): {progressive_bytes}")
    print(f"tools section bytes (old full):    {old_bytes}")
    saved = old_bytes - progressive_bytes
    print(f"saved: {saved} bytes ({100 * saved / max(old_bytes, 1):.1f}%)")
    print(f"tools in view: {len(view.tools)} = core {len(core_rows)} + "
          f"mcp index {len(mcp_index_rows)} + mcp expanded {len(mcp_expanded_rows)}")
    mcp_called = sorted({t for t in receipts if ":" in t})
    print(f"tool receipts: {len(receipts)}; mcp tools actually called: {mcp_called}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
