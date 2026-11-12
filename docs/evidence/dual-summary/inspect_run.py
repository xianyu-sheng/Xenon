from __future__ import annotations

import json
import sys
from pathlib import Path

ledgers = sorted(
    Path("D:/tmp").glob("ds-ledger-*/dual-summary.jsonl"), key=lambda p: p.stat().st_mtime
)
path = ledgers[-1]
events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

reads: list[str] = []
for event in events:
    if event["kind"] != "tool_receipt":
        continue
    receipt = event["payload"]["receipt"]
    if receipt["tool"] != "read":
        continue
    full = receipt["effects"][0]["path"]
    reads.append(full.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1])

print("ledger:", path)
print("read sequence:", reads)
print("distinct files:", sorted(set(reads)))

delivered = [e for e in events if e["kind"] == "delivered"]
if delivered:
    print("--- final answer ---")
    print(delivered[-1]["payload"]["answer"][:700])
