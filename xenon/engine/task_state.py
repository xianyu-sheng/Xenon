"""Durable task state shared by the REPL and all engines.

This is execution state, not evidence.  It records what the user asked for,
what the agent planned, which artifacts/tests/commit/PR already exist, and
what is still missing for delivery.  It is stored inside AgentContext (and
therefore auto-save/resume) as plain JSON.
"""

from __future__ import annotations

import copy
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
_TEST_CREATE_RE = re.compile(
    r"(?:新增|添加|补(?:一个|充)?|写(?:一个|入)?(?!死)|创建|加(?:一个)?).{0,30}"
    r"(?:pytest|测试|test)",
    re.IGNORECASE,
)
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
_COMMIT_OUTPUT_RE = re.compile(
    r"\[[^]]*?([0-9a-f]{7,40})[^]]*?\]",
    re.IGNORECASE,
)


def _commit_hash(text: str) -> str:
    """Return the hash from a git commit summary line, not an old log hash."""
    match = _COMMIT_OUTPUT_RE.search(text)
    return match.group(1) if match else ""


def _status_is_dirty(text: str) -> bool:
    """Parse git status --short (or porcelain) output."""
    for line in text.splitlines():
        value = line.strip()
        if not value:
            continue
        if value.startswith(("??", "M ", "A ", "D ", "R ", "C ", "U ", "AM", "MM", "AD", "AU")):
            return True
    return False


def _status_paths(text: str) -> list[str]:
    """Extract changed paths from git status --short output."""
    paths: list[str] = []
    for line in text.splitlines():
        value = line.rstrip()
        if len(value) < 4:
            continue
        marker = value[:2]
        if marker not in {
            "??",
            "M ",
            " M",
            "A ",
            " A",
            "D ",
            " D",
            "R ",
            " R",
            "C ",
            " C",
            "U ",
            " U",
            "AM",
            "MM",
            "AD",
            "AU",
        }:
            continue
        path = value[3:].strip()
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[-1]
        path = path.strip('\"').strip("'")
        if path and path not in paths:
            paths.append(path)
    return paths


def _verification_text_failed(text: str) -> bool:
    """Whether test output explicitly contains a failure marker."""
    return bool(re.search(r"(?:\d+\s+)?failed|FAILED|ERROR", text, re.I))


def _verification_passed(text: str) -> bool:
    """Conservative evidence check for a successful test command."""
    if not text.strip():
        return False
    if _verification_text_failed(text):
        return False
    return bool(
        re.search(
            r"(?:\d+\s+)?passed|PASSED|OK|ok\s+\S+",
            text,
            re.I,
        )
    )


def _metadata_exit_code(metadata: dict[str, Any]) -> int | None:
    """Read a real process exit code from structured tool metadata."""
    for key in ("exit_code", "returncode"):
        value = metadata.get(key)
        if isinstance(value, bool) or value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _failure_output(metadata: dict[str, Any], result: str) -> str:
    """Keep the actionable tail of a failed verification command."""
    parts: list[str] = []
    for key in ("stdout_tail", "stderr_tail"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    if not parts and result.strip():
        parts.append(result.strip())
    return chr(10).join(parts)[-2000:]


def _text(value: Any) -> str:
    return str(value or "")


def _tool_command(tool: str, params: dict[str, Any]) -> str:
    for key in ("action", "command", "code", "script", "cmd"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _paths_from_metadata(metadata: dict[str, Any]) -> list[str]:
    """Paths reported by the tool itself, before falling back to params."""
    paths: list[str] = []
    for key in ("changed_paths", "file_path", "path"):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            paths.append(value)
        elif isinstance(value, list):
            paths.extend(str(item) for item in value if isinstance(item, str) and item)
    return paths


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


def _is_test_path(path: str) -> bool:
    normalized = str(path).replace("\\", "/").lower()
    parts = [part for part in normalized.split("/") if part]
    if "tests" in parts or "test" in parts:
        return True
    return bool(parts and parts[-1].startswith("test_"))


def _path_key(path: str) -> str:
    return str(path).replace("\\", "/").strip().lower()


@dataclass
class TaskState:
    """One user task that may span several engine runs."""

    objective: str
    intent: str | None = None
    execution_level: int = 0
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "active"  # active | completed | blocked
    steps: list[dict[str, Any]] = field(default_factory=list)
    plan_version: int = 1
    plan_history: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    artifacts_revision: int = 0
    verified_revision: int = 0
    tests: list[str] = field(default_factory=list)
    failed_tests: list[str] = field(default_factory=list)
    test_artifacts: list[str] = field(default_factory=list)
    invalid_test_artifacts: list[str] = field(default_factory=list)
    verification_failures: list[dict[str, Any]] = field(default_factory=list)
    processed_tool_ids: list[str] = field(default_factory=list)
    commit: str = ""
    committed_artifacts: list[str] = field(default_factory=list)
    committed_source_paths: list[str] = field(default_factory=list)
    source_commit_tracking: bool = False
    commit_paths_known: bool = False
    committed_revision: int = 0
    dirty_after_commit: bool | None = None
    dirty_paths: list[str] = field(default_factory=list)
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
                {
                    "step": _text(s.get("step") or s.get("task")),
                    "status": _text(s.get("status")) or "pending",
                    **(
                        {"id": str(s["id"])}
                        if s.get("id") not in (None, "")
                        else {}
                    ),
                    **{
                        key: copy.deepcopy(s[key])
                        for key in (
                            "kind",
                            "requires_write",
                            "verify_cmd",
                            "target_paths",
                            "origin",
                            "parent_step_id",
                            "plan_version",
                            "attempt",
                            "attempts",
                            "last_error",
                            "last_summary",
                            "superseded_by",
                        )
                        if key in s
                    },
                }
                for s in raw.get("steps", [])
                if isinstance(s, dict) and (s.get("step") or s.get("task"))
            ],
            plan_version=max(1, int(raw.get("plan_version") or 1)),
            plan_history=[
                item
                for item in raw.get("plan_history", [])
                if isinstance(item, dict)
            ],
            artifacts=[str(p) for p in raw.get("artifacts", []) if p],
            artifacts_revision=max(0, int(raw.get("artifacts_revision") or 0)),
            verified_revision=max(0, int(raw.get("verified_revision") or 0)),
            tests=[str(t) for t in raw.get("tests", []) if t],
            failed_tests=[str(t) for t in raw.get("failed_tests", []) if t],
            test_artifacts=[str(p) for p in raw.get("test_artifacts", []) if p],
            invalid_test_artifacts=[
                str(p) for p in raw.get("invalid_test_artifacts", []) if p
            ],
            verification_failures=[
                item
                for item in raw.get("verification_failures", [])
                if isinstance(item, dict)
            ],
            processed_tool_ids=[
                str(item) for item in raw.get("processed_tool_ids", []) if item
            ],
            commit=_text(raw.get("commit")),
            committed_artifacts=[
                str(p) for p in raw.get("committed_artifacts", []) if p
            ],
            committed_source_paths=[
                str(p) for p in raw.get("committed_source_paths", []) if p
            ],
            source_commit_tracking=bool(raw.get("source_commit_tracking")),
            commit_paths_known=bool(raw.get("commit_paths_known")),
            committed_revision=max(0, int(raw.get("committed_revision") or 0)),
            dirty_after_commit=(
                bool(raw["dirty_after_commit"])
                if "dirty_after_commit" in raw
                else None
            ),
            dirty_paths=[
                str(path) for path in raw.get("dirty_paths", []) if path
            ],
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
            "plan_version": self.plan_version,
            "plan_history": self.plan_history[-10:],
            "artifacts": self.artifacts[-30:],
            "artifacts_revision": self.artifacts_revision,
            "verified_revision": self.verified_revision,
            "tests": self.tests[-20:],
            "failed_tests": self.failed_tests[-10:],
            "test_artifacts": self.test_artifacts[-20:],
            "invalid_test_artifacts": self.invalid_test_artifacts[-20:],
            "verification_failures": self.verification_failures[-3:],
            "processed_tool_ids": self.processed_tool_ids[-200:],
            "commit": self.commit,
            "committed_artifacts": self.committed_artifacts[-30:],
            "committed_source_paths": self.committed_source_paths[-30:],
            "source_commit_tracking": self.source_commit_tracking,
            "commit_paths_known": self.commit_paths_known,
            "committed_revision": self.committed_revision,
            "dirty_after_commit": self.dirty_after_commit,
            "dirty_paths": self.dirty_paths[-20:],
            "pushed": self.pushed,
            "pr_url": self.pr_url,
            "required": self.required,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @property
    def active(self) -> bool:
        return self.status == "active"

    def incomplete_steps(self) -> list[dict[str, Any]]:
        return [
            step
            for step in self.steps
            if step.get("status") != "completed"
            and int(step.get("plan_version") or self.plan_version)
            == self.plan_version
        ]

    def set_plan(
        self,
        steps: Iterable[dict[str, Any]],
        *,
        version: int | None = None,
        reason: str = "",
    ) -> None:
        """Replace the current plan while retaining a bounded revision log.

        A revision keeps the prior plan visible for audit and display.  A
        normal ``update_plan`` at the same version is an in-place refresh and
        does not create a bogus revision.
        """
        new_version = max(1, int(version or self.plan_version))
        if new_version < self.plan_version:
            # A replay/retry must not overwrite a newer plan with an older
            # ledger snapshot.
            return
        if new_version > self.plan_version and self.steps:
            self.plan_history.append(
                {
                    "version": self.plan_version,
                    "reason": _text(reason)[:240],
                    "steps": copy.deepcopy(self.steps),
                }
            )
            self.plan_history = self.plan_history[-10:]
        existing = {
            _text(step.get("id")): step
            for step in self.steps
            if _text(step.get("plan_version")) == str(new_version)
            and _text(step.get("id"))
        }
        rows: list[dict[str, Any]] = []
        for raw in steps:
            if not isinstance(raw, dict):
                continue
            task = _text(raw.get("step") or raw.get("task"))
            if not task:
                continue
            row: dict[str, Any] = {
                "step": task,
                "status": _text(raw.get("status")) or "pending",
                "plan_version": new_version,
            }
            if raw.get("id") not in (None, ""):
                row["id"] = str(raw["id"])
                prior = existing.get(row["id"])
                if prior and not raw.get("status"):
                    row["status"] = _text(prior.get("status")) or "pending"
            for key in (
                "kind",
                "requires_write",
                "verify_cmd",
                "target_paths",
                "origin",
                "parent_step_id",
                "attempt",
                "attempts",
                "last_error",
                "last_summary",
                "superseded_by",
            ):
                if key in raw:
                    row[key] = copy.deepcopy(raw[key])
            rows.append(row)
        self.steps = rows
        self.plan_version = new_version
        self.updated_at = time.time()

    def upsert_step(
        self,
        step: dict[str, Any],
        *,
        status: str | None = None,
    ) -> bool:
        """Add or update a step, including remediation steps absent from plan.

        Remediation and replanned steps must be visible in the same execution
        record.  Returning False used to make them disappear from the plan view
        even though their tools ran and changed the workspace.
        """
        if not isinstance(step, dict):
            return False
        key = _text(step.get("id"))
        task = _text(step.get("task") or step.get("step"))
        if not key and not task:
            return False
        for row in self.steps:
            row_key = _text(row.get("id"))
            if key and row_key == key:
                if task:
                    row["step"] = task
                if status is not None:
                    row["status"] = _text(status) or "pending"
                for field_name in (
                    "kind",
                    "requires_write",
                    "verify_cmd",
                    "target_paths",
                    "origin",
                    "parent_step_id",
                    "attempt",
                    "attempts",
                    "last_error",
                    "last_summary",
                ):
                    if field_name in step:
                        row[field_name] = copy.deepcopy(step[field_name])
                row.setdefault("plan_version", self.plan_version)
                self.updated_at = time.time()
                return True
        row = {
            "step": task,
            "status": _text(status or step.get("status")) or "pending",
            "plan_version": int(step.get("plan_version") or self.plan_version),
            **{
                k: copy.deepcopy(v)
                for k, v in step.items()
                if k not in {"step", "task", "status", "plan_version"}
                and k
                in {
                    "id",
                    "kind",
                    "requires_write",
                    "verify_cmd",
                    "target_paths",
                    "origin",
                    "parent_step_id",
                    "attempt",
                    "attempts",
                    "last_error",
                    "last_summary",
                }
            },
        }
        if key:
            row["id"] = key
        self.steps.append(row)
        self.updated_at = time.time()
        return True

    def set_step_status(
        self,
        step_id: Any,
        status: str,
        **details: Any,
    ) -> bool:
        """Update one step by stable id; return False when it is not present."""
        key = _text(step_id)
        if not key:
            return False
        for index, step in enumerate(self.steps, start=1):
            row_key = _text(step.get("id"))
            if row_key == key or (not row_key and str(index) == key):
                step["status"] = _text(status) or "pending"
                for name, value in details.items():
                    if name in {
                        "attempt",
                        "attempts",
                        "last_error",
                        "last_summary",
                        "kind",
                        "verify_cmd",
                        "parent_step_id",
                    }:
                        step[name] = copy.deepcopy(value)
                self.updated_at = time.time()
                return True
        return False

    def plan_summary(self) -> str:
        """Stable text view used by CLI/REPL plan display and tests."""
        icons = {
            "completed": "OK",
            "failed": "X",
            "running": ">",
            "skipped": "-",
            "pending": ".",
        }
        revision_note = (
            f" ({len(self.plan_history)} revisions)"
            if self.plan_history
            else ""
        )
        lines = [f"Plan v{self.plan_version}{revision_note}"]
        for step in self.steps:
            status = _text(step.get("status")) or "pending"
            icon = icons.get(status, "?")
            text = _text(step.get("step"))[:160]
            attempts = step.get("attempts") or step.get("attempt")
            suffix = (
                f" (attempt {attempts})"
                if attempts and int(attempts) > 1
                else ""
            )
            lines.append(f"[{icon}] {text}{suffix}")
            if status == "failed" and step.get("last_error"):
                lines.append(f"    ! {_text(step['last_error'])[:200]}")
        if self.required:
            missing = self.missing_delivery()
            if missing:
                lines.append("Delivery: missing " + ", ".join(missing))
            elif self.plan_version:
                lines.append("Delivery: complete")
        return chr(10).join(lines)

    def requires_new_test(self) -> bool:
        return bool(_TEST_CREATE_RE.search(self.objective))

    def has_source_artifact(self, paths: list[str] | None = None) -> bool:
        """Whether a successful write touched a non-test file."""
        for path in self.artifacts if paths is None else paths:
            if not _is_test_path(path):
                return True
        return False

    def required_delivery(self) -> list[str]:
        """Return the delivery gates that are still missing."""
        missing: list[str] = []
        if self.requires_new_test() and not self.test_artifacts:
            missing.append("test")
        if "verify" in self.required and (
            not self.tests or self.verified_revision < self.artifacts_revision
        ):
            # Tests must be newer than the last write.  A passing test before
            # a post-test edit is stale evidence, not verification.
            missing.append("verify")
        # A commit that only captures tests (or an empty change) must not
        # satisfy a code-delivery task.  This is execution state, not proof;
        # it only prevents ``maybe_complete`` from blessing a no-op commit.
        committed = (
            self.committed_artifacts
            if self.commit_paths_known
            else (self.committed_artifacts or self.artifacts)
        )
        has_source = (
            bool(self.committed_source_paths)
            if self.source_commit_tracking
            else self.has_source_artifact(committed)
        )
        if (
            "commit" in self.required
            and self.intent not in {"write_doc", "write_report"}
            and not has_source
        ):
            missing.append("changes")
        if "commit" in self.required and not self.commit:
            missing.append("commit")
        if "commit" in self.required and (
            self.dirty_after_commit is True
            or (
                self.commit
                and self.committed_revision < self.artifacts_revision
                # A real clean-worktree probe is stronger than the revision
                # counter: rewriting a file with identical content is not a
                # stale commit.  Without a probe, remain conservative.
                and self.dirty_after_commit is not False
            )
        ):
            missing.append("dirty")
        if "pr" in self.required and not self.pr_url:
            missing.append("pr")
        return missing

    def missing_delivery(self) -> list[str]:
        return self.required_delivery()

    def maybe_complete(self) -> bool:
        """Mark completed only when plan and delivery are both finished."""
        if self.missing_delivery():
            return False
        self.reconcile_satisfied_steps()
        if self.incomplete_steps():
            return False
        self.status = "completed"
        self.updated_at = time.time()
        return True

    def reconcile_satisfied_steps(self) -> None:
        """Retire failed plan rows whose delivery dimension is now satisfied.

        A remediation row may fail, then a later command cleans the worktree or
        reruns the test.  The delivery facts are authoritative; the old failed
        row must not keep an otherwise complete task active forever.
        """
        if self.missing_delivery():
            return
        satisfied = {
            "edit": True,
            "verify": bool(self.tests),
            "commit": bool(self.commit),
            "clean": self.dirty_after_commit is False,
        }
        for step in self.steps:
            if step.get("status") not in {"failed", "skipped"}:
                continue
            if satisfied.get(_text(step.get("kind")).lower()):
                step["status"] = "completed"
                step["last_summary"] = "reconciled by delivery facts"
        self.updated_at = time.time()

    def reconcile_delivery_steps(self, missing: list[str]) -> None:
        """Fail plan rows that claim a missing delivery dimension.

        Plan text is not evidence.  When the durable delivery ledger says a
        dimension is missing, a step that explicitly claimed to run tests,
        commit, clean the worktree or perform the source write must not remain
        completed.
        """
        if not missing:
            return
        rules = {
            "verify": re.compile(r"(?:pytest|测试|验证|verify|test)", re.I),
            "dirty": re.compile(r"(?:工作区|干净|git\s+status|clean|dirty)", re.I),
            "commit": re.compile(r"(?:commit|提交)", re.I),
            "changes": re.compile(
                r"(?:write_file|edit_file|修改|写入|插入|补齐|落盘)", re.I
            ),
        }
        kind_to_dimension = {
            "verify": "verify",
            "commit": "commit",
            "clean": "dirty",
            "edit": "changes",
        }
        for step in self.steps:
            if step.get("status") != "completed":
                continue
            if int(step.get("plan_version") or self.plan_version) != self.plan_version:
                continue
            kind = _text(step.get("kind")).lower()
            dimension = kind_to_dimension.get(kind)
            if dimension:
                if dimension in missing:
                    step["status"] = "failed"
                continue
            text = str(step.get("step") or "")
            if any(
                dimension in missing
                and rule.search(text)
                for dimension, rule in rules.items()
            ):
                step["status"] = "failed"
        self.updated_at = time.time()

    def _record_commit(
        self, commit_hash: str, metadata: dict[str, Any]
    ) -> None:
        """Record a real commit and the paths captured in that commit."""
        self.commit = commit_hash
        committed_paths = metadata.get("committed_paths")
        if "committed_paths" in metadata and isinstance(committed_paths, list):
            # An explicit empty list means the commit captured no paths
            # (e.g. --allow-empty); it must not fall back to the pre-commit
            # artifact snapshot and bless a no-op commit.
            self.committed_artifacts = [
                str(path) for path in committed_paths if path
            ]
            self.commit_paths_known = True
        else:
            self.committed_artifacts = list(self.artifacts)
            self.commit_paths_known = False
        # Keep source paths from earlier commits: a later test/docs commit
        # must not erase the fact that the current source was committed.
        # A post-commit source write removes its path below, so a stale
        # commit cannot satisfy the gate after new source changes.
        for path in self.committed_artifacts:
            if _is_test_path(path):
                continue
            key = _path_key(path)
            if key not in {_path_key(p) for p in self.committed_source_paths}:
                self.committed_source_paths.append(path)
        self.source_commit_tracking = True
        self.committed_revision = self.artifacts_revision
        self.dirty_after_commit = None
        self.dirty_paths = []

    def record_tool(
        self,
        tool: str,
        params: dict[str, Any],
        *,
        success: bool,
        result: str = "",
        error: str = "",
        metadata: dict[str, Any] | None = None,
        call_id: str | None = None,
    ) -> None:
        """Update state from one actual tool call.

        Failed verification commands still matter: they invalidate an earlier
        pass.  Other failed calls must not create artifacts or commits.
        """
        self.updated_at = time.time()
        tool = _text(tool)
        call_key = _text(call_id)
        if call_key:
            if call_key in self.processed_tool_ids:
                return
            self.processed_tool_ids.append(call_key)
            self.processed_tool_ids = self.processed_tool_ids[-200:]
        metadata = metadata if isinstance(metadata, dict) else {}
        command = _tool_command(tool, params)
        result_text = _text(result)

        # A test run is the one failed action that changes delivery state.
        # A previous pass cannot survive a later failure or a code edit.
        if tool in {"command", "run_script"} and _VERIFY_RE.search(command):
            label = command.strip().replace(chr(10), " ")[:240]
            exit_code = _metadata_exit_code(metadata)
            if exit_code is not None:
                passed = (
                    success
                    and exit_code == 0
                    and not _verification_text_failed(result_text)
                )
            else:
                passed = success and _verification_passed(result_text)
            if passed:
                if label and label not in self.tests:
                    self.tests.append(label)
                self.verified_revision = self.artifacts_revision
                if label in self.failed_tests:
                    self.failed_tests.remove(label)
                self.verification_failures = []
            else:
                self.tests = []
                self.verified_revision = 0
                if label and label not in self.failed_tests:
                    self.failed_tests.append(label)
                    self.failed_tests = self.failed_tests[-10:]
                self.verification_failures.append(
                    {
                        "command": label,
                        "exit_code": exit_code,
                        "output": _failure_output(metadata, result_text),
                    }
                )
                self.verification_failures = self.verification_failures[-3:]

        if not success:
            return

        if tool in _WRITE_TOOLS:
            paths = _paths_from_metadata(metadata) or _paths_from_params(tool, params)
            if paths:
                for path in paths:
                    if path not in self.artifacts:
                        self.artifacts.append(path)
                    if _is_test_path(path):
                        by_path = metadata.get("content_chars_by_path")
                        if isinstance(by_path, dict):
                            chars = {
                                _path_key(str(k)): v for k, v in by_path.items()
                            }.get(_path_key(path))
                        else:
                            chars = metadata.get("content_chars")
                        if isinstance(chars, int) and not isinstance(chars, bool):
                            if chars > 0:
                                if path not in self.test_artifacts:
                                    self.test_artifacts.append(path)
                                self.invalid_test_artifacts = [
                                    item
                                    for item in self.invalid_test_artifacts
                                    if _path_key(item) != _path_key(path)
                                ]
                            else:
                                # Rewriting a previously valid test as an
                                # empty file must invalidate the old fact.
                                self.test_artifacts = [
                                    item
                                    for item in self.test_artifacts
                                    if _path_key(item) != _path_key(path)
                                ]
                                if path not in self.invalid_test_artifacts:
                                    self.invalid_test_artifacts.append(path)
                    if not _is_test_path(path):
                        # The committed source snapshot is stale as soon as
                        # this path is rewritten; a later test-only commit
                        # must not re-bless it.
                        self.source_commit_tracking = True
                        key = _path_key(path)
                        self.committed_source_paths = [
                            item
                            for item in self.committed_source_paths
                            if _path_key(item) != key
                        ]
                # Every successful write makes an earlier test result stale.
                self.artifacts_revision += 1

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
            if _COMMIT_RE.search(command):
                commit_hash = _commit_hash(result_text)
                if commit_hash:
                    self._record_commit(commit_hash, metadata)
            if "git status" in command:
                self.dirty_after_commit = _status_is_dirty(result_text)
                self.dirty_paths = _status_paths(result_text)
            if _PUSH_RE.search(text):
                self.pushed = True
            if _PR_RE.search(text):
                match = _PR_URL_RE.search(result_text)
                if match:
                    self.pr_url = match.group(0)

        if tool == "git":
            # The git tool may receive the command through either the
            # canonical ``git_command`` field or the generic ``action`` field.
            # A harmless ``git status`` must never satisfy a commit gate just
            # because the word "commit" appeared in the model's action label.
            action = _text(
                params.get("git_command") or params.get("action")
            ).lower()
            if action.startswith("git "):
                action = action[4:].strip()
            if action.startswith("commit"):
                commit_hash = _commit_hash(result_text)
                if commit_hash:
                    self._record_commit(commit_hash, metadata)
            if action.startswith("status"):
                self.dirty_after_commit = _status_is_dirty(result_text)
                self.dirty_paths = _status_paths(result_text)
            if action.startswith("push") and "rejected" not in result_text.lower():
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
                metadata=getattr(call, "metadata", {}) or {},
                call_id=_text(getattr(call, "result_id", "")),
            )

    def verification_failure_prompt(self, limit: int = 2) -> str:
        """Render real failing command output for the next repair turn."""
        lines: list[str] = []
        for failure in self.verification_failures[-limit:]:
            command = _text(failure.get("command"))[:240]
            exit_code = failure.get("exit_code")
            output = _text(failure.get("output"))[:1200]
            lines.append(f"- 命令: {command}")
            if exit_code is not None:
                lines.append(f"  exit_code: {exit_code}")
            if output:
                lines.append("  输出尾部:")
                lines.append("```")
                lines.append(output)
                lines.append("```")
        return chr(10).join(lines)

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
        if self.invalid_test_artifacts:
            lines.append(
                "- 空测试文件（必须写入真实断言）: "
                + ", ".join(self.invalid_test_artifacts[:8])
            )
        if self.dirty_after_commit:
            paths = ", ".join(self.dirty_paths[:8])
            if paths:
                lines.append("- 工作区未清理路径: " + paths)
        if self.failed_tests:
            lines.append("- 最近失败验证: " + ", ".join(self.failed_tests[-3:]))
        failure_block = self.verification_failure_prompt()
        if failure_block:
            lines.append("- 失败验证的真实输出（先修复，再重跑同一命令）:")
            lines.append(failure_block)
        if self.tests and self.verified_revision < self.artifacts_revision:
            lines.append(
                "- 验证已过期：最近一次通过后又修改了源码，必须重跑验证命令: "
                + self.tests[-1][:240]
            )
        if self.commit:
            lines.append(f"- 已提交: {self.commit}")
        if self.pr_url:
            lines.append(f"- PR: {self.pr_url}")
        missing = self.missing_delivery()
        if missing:
            labels = {
                "test": "新增非空回归测试文件并真实运行",
                "verify": "运行相关测试并记录真实结果",
                "changes": "实际修改非测试源码文件（不能只提交空改动或测试）",
                "commit": "创建 git commit",
                "dirty": "确认 commit 后工作区干净（没有未提交改动）",
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
