"""Run event recording for OmniAgent engine executions.

This module keeps the first observability layer intentionally small: every
interactive run gets a stable run_id and a JSONL event stream under
``.omniagent/runs/<run_id>/events.jsonl`` or a session-scoped
``.omniagent/sessions/<session_id>/runs/<run_id>/events.jsonl`` root.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from omniagent.engine.callbacks import EngineCallback

logger = logging.getLogger(__name__)

RUNS_ROOT = Path(".omniagent") / "runs"
MAX_EVENT_TEXT = 6000


def _sanitize_str(s: str) -> str:
    """移除 surrogate 字符（U+D800-U+DFFF），防止 UTF-8 写入崩溃。

    Windows 终端输出、Rich 渲染文本中可能混入 surrogate 字符，
    这些在 UTF-8 编码中不合法，json.dumps(ensure_ascii=False)
    遇到它们会引发 UnicodeEncodeError。
    """
    if not s:
        return s
    # 直接过滤 surrogate 范围的字符（最快路径）
    if any('\uD800' <= c <= '\uDFFF' for c in s):
        s = ''.join(c for c in s if not ('\uD800' <= c <= '\uDFFF'))
    return s


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def new_run_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def runs_root(root: Path | str | None = None) -> Path:
    return Path(root) if root is not None else RUNS_ROOT


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        value = _sanitize_str(value)
        if len(value) > MAX_EVENT_TEXT:
            return value[:MAX_EVENT_TEXT] + "... (truncated)"
        return value
    if isinstance(value, Path):
        return _sanitize_str(str(value))
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return repr(value)


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    session_id: str
    status: str
    mode: str
    goal: str
    started_at: str
    finished_at: str
    event_count: int
    events_path: Path


class RunRecorder:
    """Append-only event recorder for one agent run."""

    def __init__(
        self,
        *,
        goal: str,
        mode: str,
        model_ids: list[str],
        root: Path | str | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self.run_id = run_id or new_run_id()
        self.session_id = session_id or ""
        self.goal = goal
        self.mode = mode
        self.model_ids = list(model_ids)
        self.root = runs_root(root)
        self.run_dir = self.root / self.run_id
        self.events_path = self.run_dir / "events.jsonl"
        self._seq = 0
        self._started = False
        self._finished = False

    @property
    def is_finished(self) -> bool:
        return self._finished

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.emit(
            "run.started",
            goal=self.goal,
            mode=self.mode,
            model_ids=self.model_ids,
            session_id=self.session_id,
            cwd=str(Path.cwd()),
        )

    def emit(self, event_type: str, **payload: Any) -> dict[str, Any]:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._seq += 1
        event = {
            "type": event_type,
            "run_id": self.run_id,
            "seq": self._seq,
            "ts": _now_iso(),
            **_json_safe(payload),
        }
        with self.events_path.open("a", encoding="utf-8") as f:
            try:
                json_str = json.dumps(event, ensure_ascii=False, sort_keys=True)
            except (UnicodeEncodeError, UnicodeDecodeError):
                # 清理后仍有异常 → 回退到 ASCII 安全模式
                json_str = json.dumps(event, ensure_ascii=True, sort_keys=True)
            f.write(json_str + "\n")
        return event

    def finish(self, *, status: str, result: str = "", reason: str | None = None) -> None:
        if self._finished:
            return
        self._finished = True
        self.emit("run.finished", status=status, result=result, reason=reason)


class RecordingCallback(EngineCallback):
    """EngineCallback wrapper that mirrors callback activity to a RunRecorder."""

    def __init__(self, delegate: EngineCallback, recorder: RunRecorder) -> None:
        self.delegate = delegate
        self.recorder = recorder
        self._tool_count = 0
        self._current_tool_use_id: str | None = None
        self._current_tool_name: str | None = None

    def _emit(self, event_type: str, **payload: Any) -> None:
        try:
            self.recorder.emit(event_type, **payload)
        except Exception as e:
            logger.warning("failed to record run event %s: %s", event_type, e)

    def on_think(self, thought: str) -> None:
        self.delegate.on_think(thought)
        self._emit("agent.thought", thought=thought)

    def on_act(self, action: str, action_input: dict) -> None:
        self.delegate.on_act(action, action_input)
        self._tool_count += 1
        self._current_tool_use_id = f"tool-{self._tool_count}"
        self._current_tool_name = action
        self._emit(
            "tool.call_started",
            tool_use_id=self._current_tool_use_id,
            tool_name=action,
            params=action_input,
        )

    def on_observe(self, observation: str, card_data: dict | None = None) -> None:
        self.delegate.on_observe(observation, card_data=card_data)
        if self._current_tool_use_id:
            self._emit(
                "tool.call_finished",
                tool_use_id=self._current_tool_use_id,
                tool_name=self._current_tool_name or "",
                output=observation,
            )
            self._current_tool_use_id = None
            self._current_tool_name = None
        else:
            self._emit("agent.observation", observation=observation)

    def on_step(self, step_id: int, total: int, task: str) -> None:
        self.delegate.on_step(step_id, total, task)
        self._emit("step.started", step=step_id, total=total, task=task)

    def on_step_done(self, step_id: int, success: bool, summary: str) -> None:
        self.delegate.on_step_done(step_id, success, summary)
        self._emit("step.finished", step=step_id, success=success, summary=summary)

    def on_review(self, score: int, passed: bool, feedback: str) -> None:
        self.delegate.on_review(score, passed, feedback)
        self._emit("review.finished", score=score, passed=passed, feedback=feedback)

    def on_error(self, error: str) -> None:
        self.delegate.on_error(error)
        self._emit("run.error", error=error)

    def on_warning(self, warning: str) -> None:
        self.delegate.on_warning(warning)
        self._emit("run.warning", warning=warning)

    def on_finish(self, result: str) -> None:
        self.delegate.on_finish(result)
        self._emit("agent.final_answer", result=result)

    def get_thinking_panel(self):
        getter = getattr(self.delegate, "get_thinking_panel", None)
        return getter() if getter else None


def load_run_events(run_id: str, root: Path | str | None = None) -> list[dict[str, Any]]:
    path = runs_root(root) / run_id / "events.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("skipping invalid run event line in %s", path)
    return events


def summarize_run(run_id: str, root: Path | str | None = None) -> RunSummary | None:
    events = load_run_events(run_id, root=root)
    if not events:
        return None
    started = next((e for e in events if e.get("type") == "run.started"), events[0])
    finished = next((e for e in reversed(events) if e.get("type") == "run.finished"), None)
    return RunSummary(
        run_id=run_id,
        session_id=str(started.get("session_id", "")),
        status=str((finished or {}).get("status", "running")),
        mode=str(started.get("mode", "")),
        goal=str(started.get("goal", "")),
        started_at=str(started.get("ts", "")),
        finished_at=str((finished or {}).get("ts", "")),
        event_count=len(events),
        events_path=(runs_root(root) / run_id / "events.jsonl").resolve(),
    )


def list_runs(limit: int = 10, root: Path | str | None = None) -> list[RunSummary]:
    base = runs_root(root)
    if not base.exists():
        return []
    run_dirs = [p for p in base.iterdir() if p.is_dir()]
    run_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    summaries: list[RunSummary] = []
    for run_dir in run_dirs:
        summary = summarize_run(run_dir.name, root=base)
        if summary is not None:
            summaries.append(summary)
        if len(summaries) >= limit:
            break
    return summaries
