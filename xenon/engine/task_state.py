"""Durable task state shared by the REPL and all engines.

This is execution state, not evidence.  It records what the user asked for,
what the agent planned, which artifacts/tests/commit/PR already exist, and
what is still missing for delivery.  It is stored inside AgentContext (and
therefore auto-save/resume) as plain JSON.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

_WRITE_TOOLS = {
    "write_file",
    "edit_file",
    "append_file",
    "batch_write",
    "batch_edit",
    "create_directory",
    "refactor",
    "edit_with_llm",
    "clone_repo",
}
_VERIFY_RE = re.compile(
    r"(?:\bpytest\b|\bpython\s+-m\s+pytest\b|\bunittest\b|\btox\b|"
    r"\bnpm\s+test\b|\bpnpm\s+test\b|\bcargo\s+test\b|\bgo\s+test\b|"
    r"\b(?:run|execute|运行|跑).{0,20}(?:test|测试))",
    re.IGNORECASE,
)
_COMMIT_RE = re.compile(r"\bgit\s+commit\b", re.IGNORECASE)
_PUSH_RE = re.compile(r"\bgit\s+push\b", re.IGNORECASE)
_PR_RE = re.compile(r"(?:\bgh\s+pr\s+create\b|create_pull_request)", re.IGNORECASE)
_COMMIT_HASH_RE = re.compile(r"\b([0-9a-f]{7,40})\b", re.IGNORECASE)
_PR_URL_RE = re.compile(r"https://github\.com/[^\s]+/pull/\d+")


def _text(value: Any) -> str:
    return str(value or "")


def _tool_command(tool: str, params: dict[str, Any]) -> str:
    for key in ("action", "command", "code", "script", "cmd"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _paths_from_params(tool: str, params: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in ("file_path", "file_paths", "path", "target_dir", "target_directory"):
        value = params.get(key)
        if isinstance(value, str):
            paths.append(value)
        elif isinstance(value, list):
            paths.extend(str(item) for item in value if isinstance(item, str))
    if tool == "batch_write":
        for item in params.get("files", []):
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                paths.append(item["path"])
    return paths


@dataclass
class TaskState:
    """One user task that may span several engine runs."""

    objective: str
    intent: str | None = None
    execution_level: int = 0
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "active"  # active | completed | blocked
    steps: list[dict[str, str]] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    commit: str = ""
    pushed: bool = False
    pr_url: str = ""
    required: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @classmethod
    def from_context(cls, ctx: Any) -> "TaskState | None":
        if ctx is None or not hasattr(ctx, "get"):
            return None
        raw = ctx.get("_task_state")
        if not isinstance(raw, dict):
            return None
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TaskState":
        return cls(
            objective=_text(raw.get("objective")),
            intent=raw.get("intent") if isinstance(raw.get("intent"), str) else None,
            execution_level=int(raw.get("execution_level") or 0),
            task_id=_text(raw.get("task_id")) or uuid.uuid4().hex[:12],
            status=_text(raw.get("status")) or "active",
            steps=[
                {"step": _text(s.get("step")), "status": _text(s.get("status"))}
                for s in raw.get("steps", [])
                if isinstance(s, dict) and s.get("step")
            ],
            artifacts=[str(p) for p in raw.get("artifacts", []) if p],
            tests=[str(t) for t in raw.get("tests", []) if t],
            commit=_text(raw.get("commit")),
            pushed=bool(raw.get("pushed")),
            pr_url=_text(raw.get("pr_url")),
            required=[str(x) for x in raw.get("required", []) if x],
            created_at=float(raw.get("created_at") or time.time()),
            updated_at=float(raw.get("updated_at") or time.time()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "objective": self.objective,
            "intent": self.intent,
            "execution_level": self.execution_level,
            "status": self.status,
            "steps": self.steps,
            "artifacts": self.artifacts[-30:],
            "tests": self.tests[-20:],
            "commit": self.commit,
            "pushed": self.pushed,
            "pr_url": self.pr_url,
            "required": self.required,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @property
    def active(self) -> bool:
        return self.status == "active"

    def incomplete_steps(self) -> list[dict[str, str]]:
        return [s for s in self.steps if s.get("status") != "completed"]

    def set_plan(self, steps: Iterable[dict[str, Any]]) -> None:
        self.steps = [
            {
                "step": _text(s.get("step") or s.get("task")),
                "status": _text(s.get("status")) or "pending",
            }
            for s in steps
            if isinstance(s, dict) and (s.get("step") or s.get("task"))
        ]
        self.updated_at = time.time()

    def required_delivery(self) -> list[str]:
        """Return the delivery gates that are still missing."""
        missing: list[str] = []
        if "verify" in self.required and not self.tests:
            missing.append("verify")
        if "commit" in self.required and not self.commit:
            missing.append("commit")
        if "pr" in self.required and not self.pr_url:
            missing.append("pr")
        return missing

    def missing_delivery(self) -> list[str]:
        return self.required_delivery()

    def maybe_complete(self) -> bool:
        """Mark completed only when plan and delivery are both finished."""
        if self.incomplete_steps() or self.missing_delivery():
            return False
        self.status = "completed"
        self.updated_at = time.time()
        return True

    def record_tool(
        self,
        tool: str,
        params: dict[str, Any],
        *,
        success: bool,
        result: str = "",
        error: str = "",
    ) -> None:
        """Update state from one actual tool call (failed calls don't count)."""
        self.updated_at = time.time()
        tool = _text(tool)
        if not success:
            return
        command = _tool_command(tool, params)
        result_text = _text(result)

        if tool in _WRITE_TOOLS:
            for path in _paths_from_params(tool, params):
                if path not in self.artifacts:
                    self.artifacts.append(path)

        if tool == "update_plan":
            raw_steps = params.get("steps")
            if isinstance(raw_steps, list):
                self.set_plan(raw_steps)
            return

        if tool == "create_pull_request":
            match = _PR_URL_RE.search(result_text)
            if match:
                self.pr_url = match.group(0)
            return

        if tool in {"command", "run_script"}:
            text = f"{command}\n{result_text}"
            if _VERIFY_RE.search(command):
                label = command.strip().replace("\n", " ")[:240]
                if label and label not in self.tests:
                    self.tests.append(label)
            if _COMMIT_RE.search(command):
                hashes = _COMMIT_HASH_RE.findall(result_text)
                self.commit = hashes[-1] if hashes else "committed"
            if _PUSH_RE.search(text):
                self.pushed = True
            if _PR_RE.search(text):
                match = _PR_URL_RE.search(result_text)
                if match:
                    self.pr_url = match.group(0)

        if tool == "git":
            action = _text(params.get("action")).lower()
            text = f"{action} {result_text}"
            if "commit" in action:
                hashes = _COMMIT_HASH_RE.findall(result_text)
                self.commit = hashes[-1] if hashes else "committed"
            if "push" in action:
                self.pushed = True

    def update_from_calls(self, calls: Iterable[Any]) -> None:
        for call in calls:
            params = getattr(call, "params", {}) or {}
            if not isinstance(params, dict):
                params = {}
            self.record_tool(
                _text(getattr(call, "tool_name", "")),
                params,
                success=bool(getattr(call, "success", False)),
                result=_text(getattr(call, "result_summary", "")),
                error=_text(getattr(call, "error", "")),
            )

    def delivery_prompt(self) -> str:
        """Compact state block injected before an agentic turn."""
        lines = [
            "## 任务状态（跨回合，必须延续）",
            f"- 目标: {self.objective[:300]}",
        ]
        if self.steps:
            lines.append("- 计划:")
            for step in self.steps[:10]:
                mark = "x" if step.get("status") == "completed" else " "
                lines.append(f"  - [{mark}] {step.get('step', '')[:160]}")
        if self.artifacts:
            lines.append("- 已修改产物: " + ", ".join(self.artifacts[-8:]))
        if self.tests:
            lines.append("- 已执行验证: " + ", ".join(self.tests[-3:]))
        if self.commit:
            lines.append(f"- 已提交: {self.commit}")
        if self.pr_url:
            lines.append(f"- PR: {self.pr_url}")
        missing = self.missing_delivery()
        if missing:
            labels = {
                "verify": "运行相关测试并记录真实结果",
                "commit": "创建 git commit",
                "pr": "push 并创建 PR",
            }
            lines.append("- 未完成交付: " + "；".join(labels[m] for m in missing))
        return "\n".join(lines)


def required_delivery_for(
    objective: str,
    *,
    level: int,
    write_patterns: Iterable[str] = (),
    intent: str | None = None,
) -> list[str]:
    """Calculate delivery requirements for a new task.

    This is a delivery contract, not an intent classifier.  It only decides
    whether a task that already needs writes must also be verified/committed.
    """
    text = _text(objective)
    required: set[str] = set()
    if level >= 2 and intent not in {"write_doc", "write_report"}:
        required.add("verify")
    if "task_delivery" in set(write_patterns):
        required.update({"verify", "commit"})
    if re.search(r"(?:\bPR\b|pull\s+request|创建\s*PR|开\s*PR|提\s*PR|push)", text, re.I):
        required.update({"verify", "commit", "pr"})
    if re.search(r"(?:不要|无需|不用).{0,8}(?:提交|commit|PR)", text, re.I):
        required.discard("commit")
        required.discard("pr")
    return sorted(required)


def task_state_block(ctx: Any) -> str:
    state = TaskState.from_context(ctx)
    return state.delivery_prompt() if state and state.active else ""
