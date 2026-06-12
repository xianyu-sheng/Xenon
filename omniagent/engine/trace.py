"""三层 Trace 系统 — 借鉴 KamaClaude 的 IPC/Event/LLM 分层记录。

三层分离:
  Layer 1: IPC — 所有请求/响应 NDJSON 帧
  Layer 2: Event — EventBus 事件流 (step/tool/token)
  Layer 3: LLM — 模型调用的请求/响应/stream

使用方式:
  writer = TraceWriter(trace_dir)
  writer.emit_ipc(direction="CORE→CLI", data={...})
  writer.emit_event(event_model)
  writer.emit_llm(direction="CORE→LLM", model="deepseek", ...)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TRACE_DIR = Path(".omniagent/trace")


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class TraceRecord:
    """一条 trace 记录。"""
    ts: str = field(default_factory=_now)
    layer: str = ""          # "ipc" | "event" | "llm"
    direction: str = ""      # "CORE→LLM" | "LLM→CORE" | "CLI→CORE" | "CORE→CLI" | "CORE"
    kind: str = ""           # 事件类型 (event type)
    run_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "ts": self.ts,
            "layer": self.layer,
            "direction": self.direction,
            "kind": self.kind,
        }
        if self.run_id:
            d["run_id"] = self.run_id
        d.update(self.data)
        return d


class TraceWriter:
    """三层 Trace 写入器。

    每个 run 的 trace 写入到独立的 JSONL 文件。
    支持按 layer 过滤查询。
    """

    def __init__(self, trace_dir: Path | None = None) -> None:
        self._dir = trace_dir or TRACE_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        self._current_run_id: str | None = None
        self._file_path: Path | None = None

    # ── 写入 ────────────────────────────────────────────────

    def open_run(self, run_id: str) -> None:
        """为指定 run 打开 trace 文件。"""
        self._current_run_id = run_id
        run_dir = self._dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        self._file_path = run_dir / "trace.jsonl"

    def close_run(self) -> None:
        """关闭当前 run 的 trace。"""
        self._current_run_id = None
        self._file_path = None

    def emit(self, record: TraceRecord) -> None:
        """写入一条 trace 记录。"""
        if not self._file_path:
            return
        try:
            with self._file_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"写入 trace 失败: {e}")

    def emit_ipc(
        self, direction: str, data: dict[str, Any],
        *, run_id: str = "", kind: str = "",
    ) -> None:
        """记录 IPC 层 trace。"""
        self.emit(TraceRecord(
            layer="ipc", direction=direction, kind=kind or "ipc_frame",
            run_id=run_id, data=data,
        ))

    def emit_event(self, event: Any) -> None:
        """记录 EventBus 事件到 trace（event 层）。"""
        event_dict = event.model_dump() if hasattr(event, "model_dump") else {}
        run_id = event_dict.get("run_id", "")
        event_type = event_dict.get("event_type", type(event).__name__)
        self.emit(TraceRecord(
            layer="event", direction="CORE", kind=event_type,
            run_id=run_id, data=event_dict,
        ))

    def emit_llm(
        self, direction: str, model: str, *,
        run_id: str = "", kind: str = "", data: dict[str, Any] | None = None,
    ) -> None:
        """记录 LLM 层 trace。"""
        d: dict[str, Any] = data or {}
        d["model"] = model
        self.emit(TraceRecord(
            layer="llm", direction=direction, kind=kind or "llm_call",
            run_id=run_id, data=d,
        ))

    # ── 查询 ────────────────────────────────────────────────

    def read_run(self, run_id: str, *, layer: str | None = None) -> list[dict[str, Any]]:
        """读取指定 run 的 trace 记录，可按 layer 过滤。"""
        path = self._dir / run_id / "trace.jsonl"
        if not path.exists():
            return []

        records: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                if layer and rec.get("layer") != layer:
                    continue
                records.append(rec)
            except json.JSONDecodeError:
                continue
        return records

    def list_runs(self, limit: int = 20) -> list[str]:
        """列出最近 trace 的 run_id。"""
        if not self._dir.exists():
            return []
        run_dirs = sorted(
            [p for p in self._dir.iterdir() if p.is_dir()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return [p.name for p in run_dirs[:limit]]


# ── 便捷函数 ────────────────────────────────────────────────

_trace_writer: TraceWriter | None = None


def get_trace_writer() -> TraceWriter:
    """获取全局 trace writer 单例。"""
    global _trace_writer
    if _trace_writer is None:
        _trace_writer = TraceWriter()
    return _trace_writer
