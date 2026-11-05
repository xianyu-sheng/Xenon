"""The truncated-result path must be usable end to end (#423).

The chain is: ToolExecutor records a result under ``result_id`` → gateway puts
it on the receipt as ``raw_ref`` → the model view exposes it → the model calls
``tool_result_read`` by that id.  A dogfood run showed the middle link missing:
the model was told to page a result it could not name, and the reader failed
with "no result cache".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from xenon.harness.runner import run_task
from xenon.kernel.contracts import CallTool, Capability, Finish, ToolIntent
from xenon.ledger.store import Ledger


class _ReadThenExpand:
    def __init__(self) -> None:
        self.phase = 0

    def propose(self, view: Any) -> Any:
        if self.phase == 0:
            self.phase = 1
            return CallTool(
                ToolIntent(
                    capability=Capability.READ,
                    params={"file_path": "big.txt"},
                )
            )
        if self.phase == 1:
            self.phase = 2
            for item in reversed(view.working_set):
                if item.get("kind") != "tool_receipt":
                    continue
                result_id = str(item.get("result_id") or "")
                if result_id:
                    return CallTool(
                        ToolIntent(
                            capability=Capability.READ,
                            prefer="tool_result_read",
                            params={"result_id": result_id},
                        )
                    )
            return Finish("no result id in the view")
        return Finish("done")


def test_result_id_reaches_the_view_and_the_reader_succeeds(
    tmp_path: Path,
) -> None:
    (tmp_path / "big.txt").write_text("x" * 12_000, encoding="utf-8")

    run = run_task(
        "read the big file and page its full result",
        strategy=_ReadThenExpand(),
        workspace=tmp_path,
        ledger_dir=tmp_path / "ledger",
        finalize=False,
        max_steps=0,
    )

    receipts = [
        event.payload["receipt"]
        for event in Ledger.open(run.ledger_path).read()
        if event.kind.value == "tool_receipt"
    ]
    read = next(item for item in receipts if item["tool"] == "read")
    assert read["raw_ref"], "the read receipt must expose a result_id"

    expanded = next(
        item for item in receipts if item["tool"] == "tool_result_read"
    )
    assert expanded["status"] == "ok", expanded.get("error")
