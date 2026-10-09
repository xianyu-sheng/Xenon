"""TurnTree: conversation as a tree of turns (structure layer, Pi-style).

Every user turn is a node. Linear chat walks the trunk; /fork creates
siblings; /rewind moves the pointer up. Nodes hold summaries + event
pointers, never the full transcript — the full record lives in the event
log (truth layer). Node status is the single source for "继续" resolution.

Deterministic only: no LLM calls, no authority grants.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable

PASSED = "passed"
FAILED_RETRIED = "failed-retried"
FUSED = "fused"
INTERRUPTED = "interrupted"
RUNNING = "running"
PENDING_APPROVAL = "pending-approval"

RESUMABLE_STATUSES = frozenset({FAILED_RETRIED, FUSED, INTERRUPTED})


@dataclass
class TurnNode:
    """One user turn: structured summary + verdict, no full transcript."""

    turn_id: str
    seq: int  # 本枝上第几个用户回合
    user_text: str
    status: str = RUNNING
    engine: str = ""
    model: str = ""
    steps: int = 0
    tools: int = 0
    errors: int = 0
    verdict_reasons: list[str] = field(default_factory=list)
    retries_used: int = 0
    archive_summary: str = ""
    event_refs: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    parent: "TurnNode | None" = None
    children: list["TurnNode"] = field(default_factory=list)

    @property
    def is_resumable(self) -> bool:
        return self.status in RESUMABLE_STATUSES


class TurnTree:
    """In-memory turn tree; rebuildable from the event log (best effort)."""

    def __init__(self) -> None:
        self.root = TurnNode(turn_id="root", seq=0, user_text="", status=PASSED)
        self.current = self.root
        self._counter = 0

    # ── mutation ─────────────────────────────────────────────

    def append_turn(self, user_text: str) -> TurnNode:
        self._counter += 1
        node = TurnNode(
            turn_id=f"turn-{int(time.time() * 1000)}-{self._counter}",
            seq=self.current.seq + 1,
            user_text=user_text[:200],
            parent=self.current,
        )
        self.current.children.append(node)
        self.current = node
        return node

    def finish(self, node: TurnNode, status: str, **fields: Any) -> None:
        node.status = status
        for key, value in fields.items():
            if hasattr(node, key) and not key.startswith("_"):
                setattr(node, key, value)

    def move_to(self, node: TurnNode) -> None:
        self.current = node

    def ancestor_at(self, n: int) -> TurnNode | None:
        """当前枝上第 n 个用户回合节点（1-based）。"""

        chain = self.chain()
        if 1 <= n <= len(chain):
            return chain[n - 1]
        return None

    # ── query ────────────────────────────────────────────────

    def chain(self) -> list[TurnNode]:
        """从 root 到 current 的节点链（不含 root）。"""

        out: list[TurnNode] = []
        node = self.current
        while node is not None and node is not self.root:
            out.append(node)
            node = node.parent
        out.reverse()
        return out

    def tail(self) -> TurnNode:
        return self.current

    def tail_resumable(self) -> TurnNode | None:
        tail = self.current
        return tail if tail is not self.root and tail.is_resumable else None

    def status_suffix(self) -> str:
        """状态块追加行：树尾节点状态（单一来源，供分类器跨轮次绑定）。"""

        tail = self.current
        if tail is self.root or tail.status in (PASSED, RUNNING):
            return ""
        reason = tail.verdict_reasons[0][:60] if tail.verdict_reasons else "未完成"
        return f"上一轮: {tail.status}（{reason}；说“继续”可续接）"

    # ── rebuild (best effort from the truth layer) ───────────

    @classmethod
    def rebuild_from_events(cls, events: Iterable[dict[str, Any]]) -> "TurnTree":
        """从事件日志重建树（turn/user 建节点，turn/verdict 写状态）。"""

        tree = cls()
        nodes_by_id: dict[str, TurnNode] = {}
        for event in events:
            if not isinstance(event, dict):
                continue
            etype = event.get("type")
            if etype == "turn/user":
                node = tree.append_turn(str(event.get("text", ""))[:200])
                tid = str(event.get("turn_id", ""))
                if tid:
                    node.turn_id = tid
                    nodes_by_id[tid] = node
            elif etype == "turn/verdict":
                tid = str(event.get("turn_id", ""))
                node = nodes_by_id.get(tid) or tree.tail()
                tree.finish(
                    node,
                    str(event.get("status", INTERRUPTED)),
                    verdict_reasons=list(event.get("reasons") or []),
                    engine=str(event.get("engine", "")),
                    steps=int(event.get("steps", 0) or 0),
                    tools=int(event.get("tools", 0) or 0),
                    errors=int(event.get("errors", 0) or 0),
                )
        return tree
