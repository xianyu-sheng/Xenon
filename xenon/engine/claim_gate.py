"""Deterministic claim verification and criterion-level goal auditing.

A claim is an *expectation* emitted by the execution layer (normally derived
from the structured plan contract).  It is never trusted on its own: the gate
compares it with real tool observations, TaskState facts and - for worktree
state - a fresh Git probe.  Only the gate may turn a claim into ``verified``.

The module contains no LLM calls.  It is deliberately independent from the
engines so the same validators can be used by Plan-Execute, ReAct and the
delivery runtime.
"""

from __future__ import annotations

import re
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from xenon.engine.evidence_contracts import (
    IGNORED_DIRTY_RE,
    command_is_test_like,
    display_path,
    is_non_test_invocation,
    is_test_path,
    paths_match,
    tests_run_from_metadata,
)
from xenon.engine.evidence_contracts import (
    WRITE_TOOL_NAMES as WRITE_TOOLS,
)

CLAIM_TOOL_SUCCEEDED = "tool_succeeded"
CLAIM_FILE_WRITTEN = "file_written"
CLAIM_SOURCE_WRITE = "source_write"
CLAIM_COMMAND_PASSED = "command_passed"
CLAIM_COMMIT_CREATED = "commit_created"
CLAIM_COMMIT_PRESENT = "commit_present"
CLAIM_WORKTREE_CLEAN = "worktree_clean"
CLAIM_TEST_ARTIFACT = "test_artifact_nonempty"
CLAIM_PR_CREATED = "pr_created"

VERIFIED = "verified"
REJECTED = "rejected"
UNVERIFIABLE = "unverifiable"

_FAILURE_TEXT = re.compile(r"(?:\d+\s+)?failed|FAILED|ERROR|Traceback", re.I)
_COMMIT_ACTION = re.compile(
    r"(?:git\s+(?:add|commit)|创建.{0,8}提交|提交.{0,8}(?:更改|修改|代码)|git\s+commit)",
    re.I,
)
_COMMIT_CONFIRM = re.compile(
    r"(?:确认|查看|检查|验证|是否|存在|已有|git\s+(?:log|status)|待提交)",
    re.I,
)
_CLEAN_WORDS = re.compile(r"(?:工作区|干净|clean|git\s+status)", re.I)


def _commit_step_is_confirmation(task: str) -> bool:
    """A ``kind=commit`` step may only confirm an existing commit.

    Planner kinds describe the topic, not always the action.  Without this
    distinction a "确认提交存在" step would be forced to create a second
    commit and fail forever.
    """
    text = _text(task)
    if not text:
        return False
    if _COMMIT_ACTION.search(text):
        return False
    return bool(_COMMIT_CONFIRM.search(text))


@dataclass(frozen=True, slots=True)
class Claim:
    """A machine-checkable expectation for one step or goal criterion."""

    claim_id: str
    step_id: str
    kind: str
    subject: str = ""
    expected: str = ""
    origin: str = "plan_contract"
    plan_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "step_id": self.step_id,
            "kind": self.kind,
            "subject": self.subject,
            "expected": self.expected,
            "origin": self.origin,
            "plan_version": self.plan_version,
        }


@dataclass(frozen=True, slots=True)
class ClaimBaseline:
    """Facts captured before a step starts.

    Keeping the call cursor and the previous commit prevents a claim from
    being satisfied by work that happened in an earlier step or task.
    """

    calls_start: int = 0
    commit_before: str = ""
    artifacts_revision_before: int = 0


@dataclass(slots=True)
class ClaimVerdict:
    claim: Claim
    status: str
    actual: str = ""
    reason: str = ""
    evidence_ids: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        # ``unverifiable`` is not a success, but it must not be converted into
        # a hard step failure: the final goal audit decides whether the open
        # obligation is acceptable.
        return self.status == VERIFIED

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.claim.to_dict(),
            "status": self.status,
            "actual": self.actual[:500],
            "reason": self.reason[:500],
            "evidence_ids": list(self.evidence_ids),
        }

    @property
    def repair_hint(self) -> str:
        target = f" {self.claim.subject}" if self.claim.subject else ""
        actual = f"；实际: {self.actual}" if self.actual else ""
        return (
            f"[{self.claim.claim_id}] {self.claim.kind}{target}: "
            f"期望 {self.claim.expected or self.claim.kind}，"
            f"未通过（{self.reason or self.status}）{actual}"
        )


def _text(value: Any) -> str:
    return str(value or "")


def _call_paths(call: Any) -> list[str]:
    params = getattr(call, "params", {}) or {}
    if not isinstance(params, dict):
        return []
    paths: list[str] = []
    for key in ("file_path", "path"):
        value = params.get(key)
        if isinstance(value, str) and value:
            paths.append(value)
    for key in ("files", "edits"):
        for item in params.get(key) or []:
            if not isinstance(item, dict):
                continue
            value = item.get("file_path") or item.get("path")
            if isinstance(value, str) and value:
                paths.append(value)
    metadata = getattr(call, "metadata", {}) or {}
    if isinstance(metadata, dict):
        for key in ("changed_paths", "file_path", "path"):
            value = metadata.get(key)
            if isinstance(value, str) and value:
                paths.append(value)
            elif isinstance(value, list):
                paths.extend(
                    str(item) for item in value if isinstance(item, str) and item
                )
    return list(dict.fromkeys(paths))


def _call_command(call: Any) -> str:
    if _text(getattr(call, "tool_name", "")) == "run_tests":
        return "run_tests"
    params = getattr(call, "params", {}) or {}
    if not isinstance(params, dict):
        return ""
    for key in ("command", "action", "cmd", "script", "code"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _normalize_command(value: str) -> str:
    """Normalize executor prefixes without erasing test targets.

    ``python -m pytest tests/a.py`` and ``pytest tests/a.py`` are equivalent
    intents.  The target tokens stay in the string, so a plan targeting one
    test file cannot be satisfied by a different file or by ``--version``.
    """
    text = re.sub(r"\s+", " ", value.strip().lower()).strip()
    text = re.sub(
        r"^(?:(?:python|py)(?:\.exe)?(?:\s+-m)?\s+)?(?:pytest|py\.test)\b",
        "pytest",
        text,
    )
    return text


def _command_matches(expected: str, actual: str) -> bool:
    exp = _normalize_command(expected)
    act = _normalize_command(actual)
    if not exp:
        return command_is_test_like(act)
    if not act or is_non_test_invocation(act):
        return False
    if exp == act or exp in act:
        return True
    # A generic actual command must not satisfy a plan that named a specific
    # test target.  ``act in exp`` is only safe for non-test commands.
    if act in exp and not command_is_test_like(exp) and not command_is_test_like(
        act
    ):
        return True
    # Compare whole argument tokens.  This still allows ``pytest -q`` to match
    # ``pytest -q tests/test_a.py``, but rejects a missing target (for example
    # a plan for ``tests/test_a.py`` satisfied by ``pytest tests/test_b.py``).
    exp_tokens = set(re.findall(r"[a-z0-9_./-]+", exp))
    act_tokens = set(re.findall(r"[a-z0-9_./-]+", act))
    return bool(exp_tokens) and exp_tokens <= act_tokens


def _exit_code(call: Any) -> int | None:
    metadata = getattr(call, "metadata", {}) or {}
    if isinstance(metadata, dict):
        for key in ("exit_code", "returncode"):
            value = metadata.get(key)
            if isinstance(value, bool) or value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


def _result_id(call: Any) -> str:
    return _text(getattr(call, "result_id", ""))


def _call_tests_run(call: Any) -> int:
    """Structured test count, or a parse of the receipt summary."""
    metadata = getattr(call, "metadata", {}) or {}
    return tests_run_from_metadata(
        metadata if isinstance(metadata, dict) else {},
        getattr(call, "result_summary", ""),
    )


def _test_claim_requires_execution(claim: Claim, call: Any) -> bool:
    """Whether this claim is specifically about running tests."""
    return bool(
        claim.subject == "run_tests"
        or _text(getattr(call, "tool_name", "")) == "run_tests"
        or command_is_test_like(claim.subject)
        or command_is_test_like(_call_command(call))
    )


def _test_content_for_call(call: Any, subject: str) -> str:
    """Extract the written test text from a write tool's parameters."""
    params = getattr(call, "params", {}) or {}
    if not isinstance(params, dict):
        return ""
    if isinstance(params.get("content"), str):
        return params["content"]
    for key in ("files", "edits"):
        items = params.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            path = item.get("file_path") or item.get("path")
            content = item.get("content") or item.get("new_text")
            if (
                isinstance(path, str)
                and isinstance(content, str)
                and paths_match(subject, path)
            ):
                return content
    return ""


def _has_test_assertion(content: str, path: str) -> bool:
    """Structural check that a test artifact contains an assertion.

    This is not a semantic test-quality judge.  It rejects empty or
    assertion-free files that used to satisfy "non-empty test file".
    """
    text = str(content or "")
    if not text.strip():
        return False
    if not str(path).lower().endswith(".py"):
        return bool(
            re.search(r"\b(?:assert|expect|should)\b", text, re.IGNORECASE)
        )
    import ast

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    assertion_methods = {
        "raises",
        "warns",
        "fail",
        "failIf",
        "assertEqual",
        "assertTrue",
        "assertFalse",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            return True
        if isinstance(node, ast.Call):
            func = node.func
            name = ""
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name.startswith("assert") or name in assertion_methods:
                return True
    return False


def _calls_since(calls: Iterable[Any], start: int) -> list[Any]:
    rows = list(calls)
    return rows[max(0, int(start or 0)) :]


def _write_calls_after(calls: Iterable[Any], start: int) -> list[Any]:
    return [
        call
        for call in _calls_since(calls, start)
        if getattr(call, "success", False)
        and getattr(call, "tool_name", "") in WRITE_TOOLS
        and not (
            isinstance(getattr(call, "metadata", None), dict)
            and getattr(call, "metadata", {}).get("changed") is False
        )
    ]


def _file_exists(workspace_root: Any, path: str) -> bool:
    target = Path(_text(path))
    candidates = [target]
    root = _text(workspace_root).strip()
    if root:
        candidates.append(Path(root) / path)
    for candidate in candidates:
        try:
            if candidate.exists():
                return True
        except (OSError, RuntimeError, ValueError):
            continue
    return False


def probe_worktree(
    workspace_root: Any,
) -> tuple[bool | None, list[str]]:
    """Return a fresh clean/dirty probe, ignoring generated caches."""
    root = _text(workspace_root).strip()
    if not root:
        return None, []
    try:
        proc = subprocess.run(
            ["git", "-C", root, "status", "--porcelain", "-uall"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None, []
    if proc.returncode != 0:
        return None, []
    lines = [
        line
        for line in proc.stdout.splitlines()
        if line.strip() and not IGNORED_DIRTY_RE.search(line)
    ]
    paths: list[str] = []
    for line in lines:
        value = line.rstrip()
        if len(value) >= 4:
            path = value[3:].strip()
            if " -> " in path:
                path = path.rsplit(" -> ", 1)[-1]
            path = path.strip('"').strip("'")
            if path:
                paths.append(path)
    return bool(lines), paths


def claims_for_step(
    step: dict[str, Any],
    *,
    requires_new_test: bool = False,
) -> list[Claim]:
    """Derive deterministic obligations from one structured plan step."""
    if not isinstance(step, dict):
        return []
    step_id = _text(step.get("id"))
    if not step_id:
        step_id = "?"
    plan_version = max(1, int(step.get("plan_version") or 1))
    kind = _text(step.get("kind")).lower()
    task_text = _text(step.get("task") or step.get("step"))
    target_paths = [
        display_path(path)
        for path in step.get("target_paths") or []
        if display_path(path)
    ]
    claims: list[Claim] = []

    def add(claim_kind: str, subject: str = "", expected: str = "") -> None:
        key = f"step:{step_id}:{claim_kind}:{display_path(subject)}"
        if any(c.claim_id == key for c in claims):
            return
        claims.append(
            Claim(
                claim_id=key,
                step_id=step_id,
                kind=claim_kind,
                subject=subject,
                expected=expected,
                origin="plan_contract",
                plan_version=plan_version,
            )
        )

    requires_write = bool(step.get("requires_write")) or kind == "edit"
    if requires_write:
        if target_paths:
            for path in target_paths:
                add(CLAIM_FILE_WRITTEN, path, f"文件 {path} 由本步骤真实写入")
                if requires_new_test and is_test_path(path):
                    add(CLAIM_TEST_ARTIFACT, path, "测试文件内容必须非空")
        else:
            add(
                CLAIM_SOURCE_WRITE,
                "",
                "本步骤必须新增成功的非测试源码写入",
            )

    verify_cmd = _text(step.get("verify_cmd")).strip()
    if kind == "verify" and verify_cmd:
        add(CLAIM_COMMAND_PASSED, verify_cmd, f"命令必须 exit=0：{verify_cmd}")
    if kind == "commit":
        if _commit_step_is_confirmation(task_text):
            add(CLAIM_COMMIT_PRESENT, "", "本步骤必须确认真实 commit 存在")
            if _CLEAN_WORDS.search(task_text):
                add(CLAIM_WORKTREE_CLEAN, "", "确认工作区干净")
        else:
            add(CLAIM_COMMIT_CREATED, "", "本步骤必须产生新的真实 commit")
    if kind == "clean" or (kind == "commit" and step.get("requires_clean")):
        add(CLAIM_WORKTREE_CLEAN, "", "commit 后工作区必须干净")
    return claims


class SimpleCall:
    """Minimal adapter so tool params reuse the same path/command extractors."""

    def __init__(self, tool_name: str, params: dict[str, Any]) -> None:
        self.tool_name = tool_name
        self.params = params
        self.metadata: dict[str, Any] = {}


def claims_for_tool(tool: str, params: dict[str, Any] | None = None) -> list[Claim]:
    """Derive structured claims from one ReAct tool call.

    Every call gets a ``tool_succeeded`` claim; side-effecting calls add their
    concrete effects (file/command/commit/PR).  This is the ReAct analogue of
    ``claims_for_step`` and deliberately uses only structured action data.
    """
    params = params if isinstance(params, dict) else {}
    tool = _text(tool)
    if not tool:
        return []
    claims: list[Claim] = []
    suffix = uuid.uuid4().hex[:10]

    def add(kind: str, subject: str = "", expected: str = "") -> None:
        claims.append(
            Claim(
                claim_id=f"tool:{tool}:{suffix}:{kind}:{display_path(subject)}",
                step_id="",
                kind=kind,
                subject=subject,
                expected=expected,
                origin="react_tool",
                plan_version=1,
            )
        )

    add(CLAIM_TOOL_SUCCEEDED, tool, f"工具 {tool} 必须真实执行成功")

    paths = _call_paths(SimpleCall(tool, params))
    if tool in WRITE_TOOLS:
        for path in paths:
            add(CLAIM_FILE_WRITTEN, path, f"工具 {tool} 必须真实写入 {path}")
            if is_test_path(path) and tool in {"write_file", "batch_write"}:
                add(CLAIM_TEST_ARTIFACT, path, "测试文件内容必须非空")

    command = _call_command(SimpleCall(tool, params))
    if tool == "run_tests":
        add(CLAIM_COMMAND_PASSED, "run_tests", "run_tests 必须 exit=0")
    if tool in {"command", "run_script"} and command:
        if re.search(r"(?:pytest|unittest|tox|\btest\b)", command, re.I):
            add(CLAIM_COMMAND_PASSED, command, f"验证命令必须 exit=0：{command}")
        if re.search(r"\bgit\s+commit\b", command, re.I):
            add(CLAIM_COMMIT_CREATED, "", "command 必须产生新的真实 commit")
    if tool == "git":
        action = _text(params.get("git_command") or params.get("action"))
        if action.lower().lstrip().startswith("commit"):
            add(CLAIM_COMMIT_CREATED, "", "git 工具必须产生新的真实 commit")
    if tool == "create_pull_request":
        add(CLAIM_PR_CREATED, "", "必须返回真实 PR URL")
    return claims


def goal_claims(state: Any) -> list[Claim]:
    """Build criterion-level obligations from durable TaskState facts."""
    if state is None:
        return []
    plan_version = max(1, int(getattr(state, "plan_version", 1) or 1))
    claims: list[Claim] = []

    def add(
        claim_id: str,
        kind: str,
        subject: str = "",
        expected: str = "",
        step_id: str = "",
    ) -> None:
        if any(c.claim_id == claim_id for c in claims):
            return
        claims.append(
            Claim(
                claim_id=claim_id,
                step_id=step_id,
                kind=kind,
                subject=subject,
                expected=expected,
                origin="goal_audit",
                plan_version=plan_version,
            )
        )

    required = set(getattr(state, "required", []) or [])
    if "test" in required or getattr(state, "requires_new_test", lambda: False)():
        add("goal:test_artifact", CLAIM_TEST_ARTIFACT, "", "需要非空测试文件")
    if "verify" in required:
        add(
            "goal:verify",
            CLAIM_COMMAND_PASSED,
            "",
            "需要真实测试命令通过，且结果不早于最后一次源码修改",
        )
    if "changes" in required or (
        "commit" in required
        and getattr(state, "intent", None) not in {"write_doc", "write_report"}
    ):
        add("goal:changes", CLAIM_SOURCE_WRITE, "", "需要真实非测试源码变更")
    if "commit" in required:
        add("goal:commit", CLAIM_COMMIT_CREATED, "", "需要真实 commit")
    if "dirty" in required or ("commit" in required and "dirty" not in required):
        add("goal:dirty", CLAIM_WORKTREE_CLEAN, "", "commit 后工作区干净")
    if "pr" in required:
        add("goal:pr", CLAIM_PR_CREATED, "", "需要 push 并创建 PR")

    for step in getattr(state, "steps", []) or []:
        if not isinstance(step, dict):
            continue
        if int(step.get("plan_version") or getattr(state, "plan_version", 1)) != int(
            getattr(state, "plan_version", 1)
        ):
            continue
        step_id = _text(step.get("id"))
        kind = _text(step.get("kind")).lower()
        if not step_id:
            continue
        if kind == "verify":
            command = _text(step.get("verify_cmd")).strip()
            if command:
                add(
                    f"goal:step:{step_id}:verify",
                    CLAIM_COMMAND_PASSED,
                    command,
                    f"计划验证命令必须通过：{command}",
                    step_id,
                )
        if kind == "commit":
            task_text = _text(step.get("task") or step.get("step"))
            add(
                f"goal:step:{step_id}:commit",
                CLAIM_COMMIT_PRESENT
                if _commit_step_is_confirmation(task_text)
                else CLAIM_COMMIT_CREATED,
                "",
                "计划提交步骤必须对应真实 commit",
                step_id,
            )
        if kind == "clean":
            add(
                f"goal:step:{step_id}:clean",
                CLAIM_WORKTREE_CLEAN,
                "",
                "计划清理步骤必须确认工作区干净",
                step_id,
            )
        if bool(step.get("requires_write")) or kind == "edit":
            targets = [
                display_path(path)
                for path in step.get("target_paths") or []
                if display_path(path)
            ]
            if targets:
                for path in targets:
                    add(
                        f"goal:step:{step_id}:file:{path}",
                        CLAIM_FILE_WRITTEN,
                        path,
                        f"计划目标文件必须真实存在：{path}",
                        step_id,
                    )
            else:
                add(
                    f"goal:step:{step_id}:write",
                    CLAIM_SOURCE_WRITE,
                    "",
                    "计划写步骤必须产生真实源码变更",
                    step_id,
                )

    # User-declared acceptance criteria (CI green, tests pass, build ok) were
    # previously only a non-blocking TurnGate reminder.  Bring the
    # test/CI-shaped ones into the same criterion audit so a targeted repair
    # can actually satisfy them instead of merely reminding the user.
    try:
        from xenon.engine.task_verifier import extract_acceptance_criteria

        acceptance = extract_acceptance_criteria(
            _text(getattr(state, "objective", ""))
        )
    except Exception:  # noqa: BLE001 — optional legacy extractor
        acceptance = []
    if not any(c.kind == CLAIM_COMMAND_PASSED for c in claims):
        for index, criterion in enumerate(acceptance):
            add(
                f"goal:acceptance:{index}",
                CLAIM_COMMAND_PASSED,
                "",
                f"用户验收条件：{criterion}",
            )
    return claims


def _verify_step_claim(
    claim: Claim,
    *,
    calls: list[Any],
    state: Any,
    baseline: ClaimBaseline,
    workspace_root: Any,
) -> ClaimVerdict:
    recent = _calls_since(calls, baseline.calls_start)
    writes = _write_calls_after(calls, baseline.calls_start)

    if claim.kind in {CLAIM_FILE_WRITTEN, CLAIM_TEST_ARTIFACT}:
        matching = [
            call
            for call in writes
            for path in _call_paths(call)
            if paths_match(claim.subject, path)
        ]
        if not matching:
            return ClaimVerdict(
                claim,
                REJECTED,
                "本步骤没有匹配的真实写工具调用",
                "缺少写入证据",
            )
        paths = [
            path
            for call in matching
            for path in _call_paths(call)
            if paths_match(claim.subject, path)
        ]
        if claim.kind == CLAIM_TEST_ARTIFACT:
            chars = 0
            for call in matching:
                metadata = getattr(call, "metadata", {}) or {}
                by_path = metadata.get("content_chars_by_path")
                if isinstance(by_path, dict):
                    chars = max(
                        chars,
                        int(
                            next(
                                (
                                    value
                                    for key, value in by_path.items()
                                    if paths_match(claim.subject, str(key))
                                ),
                                0,
                            )
                            or 0
                        ),
                    )
                value = metadata.get("content_chars")
                if isinstance(value, int) and not isinstance(value, bool):
                    chars = max(chars, value)
            if chars <= 0 and workspace_root and _file_exists(
                workspace_root, claim.subject
            ):
                chars = Path(_text(workspace_root), claim.subject).stat().st_size
            if chars <= 0:
                return ClaimVerdict(
                    claim,
                    REJECTED,
                    "测试文件为空或内容长度未知",
                    "空测试不算验证",
                    [_result_id(call) for call in matching if _result_id(call)],
                )
            content = next(
                (
                    _test_content_for_call(call, claim.subject)
                    for call in matching
                    if _test_content_for_call(call, claim.subject)
                ),
                "",
            )
            if not content and workspace_root:
                try:
                    content = Path(
                        _text(workspace_root), claim.subject
                    ).read_text(encoding="utf-8", errors="replace")
                except (OSError, ValueError):
                    content = ""
            if content and not _has_test_assertion(content, claim.subject):
                return ClaimVerdict(
                    claim,
                    REJECTED,
                    "测试文件没有可识别的断言",
                    "测试文件只包含注释/空操作",
                    [_result_id(call) for call in matching if _result_id(call)],
                )
        if workspace_root and not _file_exists(workspace_root, claim.subject):
            return ClaimVerdict(
                claim,
                REJECTED,
                "工具声称写入但文件在磁盘上不存在",
                "文件缺失",
                [_result_id(call) for call in matching if _result_id(call)],
            )
        return ClaimVerdict(
            claim,
            VERIFIED,
            "真实写入: " + ", ".join(dict.fromkeys(paths)),
            "写入证据存在",
            [_result_id(call) for call in matching if _result_id(call)],
        )

    if claim.kind == CLAIM_SOURCE_WRITE:
        source_calls = [
            call
            for call in writes
            if any(not is_test_path(path) for path in _call_paths(call))
        ]
        if not source_calls:
            return ClaimVerdict(
                claim,
                REJECTED,
                "本步骤没有新增成功的非测试源码写入",
                "缺少源码写入证据",
            )
        return ClaimVerdict(
            claim,
            VERIFIED,
            "真实源码写入",
            "本步骤产生了源码写入",
            [_result_id(call) for call in source_calls if _result_id(call)],
        )

    if claim.kind == CLAIM_COMMAND_PASSED:
        matching = [
            call
            for call in recent
            if _command_matches(claim.subject, _call_command(call))
        ]
        if not matching:
            return ClaimVerdict(
                claim,
                REJECTED,
                "本步骤没有运行计划指定的验证命令",
                "缺少同命令证据",
            )
        for call in matching:
            if not getattr(call, "success", False):
                continue
            code = _exit_code(call)
            summary = _text(getattr(call, "result_summary", ""))
            if code is not None and code != 0:
                continue
            if _FAILURE_TEXT.search(summary):
                continue
            if _test_claim_requires_execution(claim, call) and _call_tests_run(
                call
            ) <= 0:
                continue
            return ClaimVerdict(
                claim,
                VERIFIED,
                f"真实命令通过（exit={code if code is not None else 'unknown'}）",
                "命令成功",
                [_result_id(call) if _result_id(call) else ""],
            )
        return ClaimVerdict(
            claim,
            REJECTED,
            "验证命令未成功：exit code 非零或输出包含失败标记",
            "验证失败",
            [_result_id(call) for call in matching if _result_id(call)],
        )

    if claim.kind == CLAIM_TOOL_SUCCEEDED:
        matching = [
            call
            for call in recent
            if _text(getattr(call, "tool_name", "")) == claim.subject
        ]
        if not matching:
            return ClaimVerdict(
                claim,
                UNVERIFIABLE,
                "没有找到对应工具调用记录",
                "无法确认工具执行",
            )
        succeeded = [call for call in matching if getattr(call, "success", False)]
        if succeeded:
            return ClaimVerdict(
                claim,
                VERIFIED,
                f"工具 {claim.subject} 真实执行成功",
                "工具调用成功",
                [_result_id(call) for call in succeeded if _result_id(call)],
            )
        last = matching[-1]
        return ClaimVerdict(
            claim,
            REJECTED,
            _text(getattr(last, "error", ""))
            or _text(getattr(last, "result_summary", ""))
            or "工具执行失败",
            "工具调用失败",
            [_result_id(last)] if _result_id(last) else [],
        )

    if claim.kind == CLAIM_COMMIT_PRESENT:
        commit = _text(getattr(state, "commit", ""))
        if not commit:
            return ClaimVerdict(claim, REJECTED, "TaskState 没有真实 commit", "缺少 commit")
        return ClaimVerdict(
            claim, VERIFIED, f"真实 commit {commit}", "commit 存在"
        )

    if claim.kind == CLAIM_COMMIT_CREATED:
        commit = _text(getattr(state, "commit", ""))
        if not commit:
            return ClaimVerdict(claim, REJECTED, "TaskState 没有真实 commit", "缺少 commit")
        if baseline.commit_before and commit == baseline.commit_before:
            return ClaimVerdict(
                claim,
                REJECTED,
                "本步骤没有产生新的 commit（仍是旧 commit）",
                "commit 未更新",
            )
        matching = [
            call
            for call in recent
            if getattr(call, "success", False)
            and (
                "commit" in _normalize_command(_call_command(call))
                or _text(getattr(call, "tool_name", "")) == "git"
            )
        ]
        return ClaimVerdict(
            claim,
            VERIFIED,
            f"真实 commit {commit}",
            "commit 证据存在",
            [_result_id(call) for call in matching if _result_id(call)],
        )

    if claim.kind == CLAIM_WORKTREE_CLEAN:
        dirty, paths = probe_worktree(workspace_root)
        if dirty is True:
            detail = ", ".join(paths[:8])
            return ClaimVerdict(
                claim,
                REJECTED,
                "工作区仍有未提交路径" + (f": {detail}" if detail else ""),
                "工作区不干净",
            )
        if dirty is False:
            return ClaimVerdict(claim, VERIFIED, "git status 干净", "工作区干净")
        value = getattr(state, "dirty_after_commit", None)
        dirty_paths = list(getattr(state, "dirty_paths", []) or [])
        if value is False and not dirty_paths:
            return ClaimVerdict(
                claim, VERIFIED, "TaskState 记录为 clean", "工作区干净"
            )
        if value is True:
            return ClaimVerdict(
                claim,
                REJECTED,
                "TaskState 记录工作区不干净: " + ", ".join(dirty_paths[:8]),
                "工作区不干净",
            )
        return ClaimVerdict(
            claim,
            UNVERIFIABLE,
            "没有可用的 worktree 探测结果",
            "无法确认工作区状态",
        )

    return ClaimVerdict(
        claim,
        UNVERIFIABLE,
        "未知 claim 类型",
        "没有对应的确定性校验器",
    )


def verify_step_claims(
    claims: Iterable[Claim],
    *,
    calls: Iterable[Any],
    state: Any,
    baseline: ClaimBaseline,
    workspace_root: Any = None,
) -> list[ClaimVerdict]:
    """Verify claims emitted for one step, never trusting model prose."""
    rows = list(calls)
    return [
        _verify_step_claim(
            claim,
            calls=rows,
            state=state,
            baseline=baseline,
            workspace_root=workspace_root,
        )
        for claim in claims
    ]


def _goal_verdict(
    claim: Claim,
    *,
    state: Any,
    workspace_root: Any,
) -> ClaimVerdict:
    if claim.kind == CLAIM_TEST_ARTIFACT:
        paths = list(getattr(state, "test_artifacts", []) or [])
        if not paths:
            return ClaimVerdict(claim, REJECTED, "没有有效测试文件", "缺少测试文件")
        if workspace_root:
            nonempty = []
            for path in paths:
                try:
                    candidate = Path(_text(workspace_root), path)
                    if candidate.exists() and candidate.stat().st_size > 0:
                        nonempty.append(path)
                except (OSError, RuntimeError, ValueError):
                    continue
            if not nonempty:
                return ClaimVerdict(
                    claim, REJECTED, "测试文件存在但为空/不可读", "测试文件无效"
                )
            for path in nonempty:
                try:
                    content = Path(_text(workspace_root), path).read_text(
                        encoding="utf-8", errors="replace"
                    )
                except (OSError, ValueError):
                    content = ""
                if not _has_test_assertion(content, path):
                    return ClaimVerdict(
                        claim,
                        REJECTED,
                        f"测试文件没有可识别的断言: {path}",
                        "测试文件只有空操作/注释",
                    )
        return ClaimVerdict(
            claim, VERIFIED, "非空测试: " + ", ".join(paths[:8]), "测试证据存在"
        )

    if claim.kind == CLAIM_COMMAND_PASSED:
        tests = list(getattr(state, "tests", []) or [])
        failures = list(getattr(state, "failed_tests", []) or [])
        if not tests:
            return ClaimVerdict(
                claim, REJECTED, "没有真实验证通过记录", "缺少验证证据"
            )
        if failures:
            return ClaimVerdict(
                claim,
                REJECTED,
                "最近验证仍有失败: " + ", ".join(failures[:5]),
                "验证失败",
            )
        if claim.subject:
            matching_tests = [
                item for item in tests if _command_matches(claim.subject, item)
            ]
            if not matching_tests:
                return ClaimVerdict(
                    claim,
                    REJECTED,
                    "没有与目标匹配的验证记录: " + claim.subject,
                    "验证目标未执行",
                )
        if int(getattr(state, "verified_revision", 0)) < int(
            getattr(state, "artifacts_revision", 0)
        ):
            return ClaimVerdict(
                claim,
                REJECTED,
                "验证结果早于最后一次源码修改，已过期",
                "验证过期",
            )
        return ClaimVerdict(
            claim, VERIFIED, "验证通过: " + "; ".join(tests[-3:]), "验证证据存在"
        )

    if claim.kind == CLAIM_SOURCE_WRITE:
        if not getattr(state, "has_source_artifact", lambda: False)():
            return ClaimVerdict(
                claim, REJECTED, "没有非测试源码产物", "缺少源码变更"
            )
        return ClaimVerdict(
            claim,
            VERIFIED,
            "源码产物: " + ", ".join(list(getattr(state, "artifacts", []))[:8]),
            "源码变更存在",
        )

    if claim.kind == CLAIM_COMMIT_PRESENT:
        commit = _text(getattr(state, "commit", ""))
        if not commit:
            return ClaimVerdict(claim, REJECTED, "没有真实 commit", "缺少 commit")
        return ClaimVerdict(claim, VERIFIED, f"真实 commit {commit}", "commit 存在")

    if claim.kind == CLAIM_COMMIT_CREATED:
        commit = _text(getattr(state, "commit", ""))
        if not commit:
            return ClaimVerdict(claim, REJECTED, "没有真实 commit", "缺少 commit")
        return ClaimVerdict(claim, VERIFIED, f"真实 commit {commit}", "commit 存在")

    if claim.kind == CLAIM_WORKTREE_CLEAN:
        dirty, paths = probe_worktree(workspace_root)
        if dirty is True:
            return ClaimVerdict(
                claim,
                REJECTED,
                "工作区仍有未提交路径: " + ", ".join(paths[:8]),
                "工作区不干净",
            )
        if dirty is False:
            return ClaimVerdict(claim, VERIFIED, "git status 干净", "工作区干净")
        value = getattr(state, "dirty_after_commit", None)
        if value is False and not getattr(state, "dirty_paths", []):
            return ClaimVerdict(claim, VERIFIED, "TaskState 记录为 clean", "工作区干净")
        if value is True:
            return ClaimVerdict(
                claim,
                REJECTED,
                "TaskState 记录工作区不干净",
                "工作区不干净",
            )
        return ClaimVerdict(
            claim,
            UNVERIFIABLE,
            "没有可用 worktree 探测",
            "无法确认工作区状态",
        )

    if claim.kind == CLAIM_FILE_WRITTEN:
        artifacts = list(getattr(state, "artifacts", []) or [])
        matched = [path for path in artifacts if paths_match(claim.subject, path)]
        if not matched:
            return ClaimVerdict(
                claim, REJECTED, "TaskState 没有该文件写入记录", "缺少文件证据"
            )
        if workspace_root and not _file_exists(workspace_root, claim.subject):
            return ClaimVerdict(
                claim, REJECTED, "文件已不在磁盘上", "文件缺失"
            )
        return ClaimVerdict(
            claim, VERIFIED, "真实文件: " + ", ".join(matched[:5]), "文件存在"
        )

    if claim.kind == CLAIM_PR_CREATED:
        url = _text(getattr(state, "pr_url", ""))
        if not url:
            return ClaimVerdict(claim, REJECTED, "没有真实 PR URL", "缺少 PR")
        return ClaimVerdict(claim, VERIFIED, url, "PR 存在")

    return ClaimVerdict(
        claim, UNVERIFIABLE, "未知目标条件", "没有对应的确定性校验器"
    )


def audit_goal(
    state: Any,
    *,
    workspace_root: Any = None,
) -> list[ClaimVerdict]:
    """Criterion-level final audit; deterministic and replayable."""
    return [
        _goal_verdict(claim, state=state, workspace_root=workspace_root)
        for claim in goal_claims(state)
    ]


def record_pending_claims(
    ledger: Any,
    claims: Iterable[Claim],
    *,
    task_id: str = "",
    attempt: int = 0,
) -> list[Any]:
    """Register expectations before a step starts.

    The pending event makes the claim auditable before the action.  The later
    gate verdict reuses the same claim id and replaces the status.
    """
    if ledger is None or not hasattr(ledger, "append"):
        return []
    from xenon.engine.evidence_runtime import (
        EventKind,
        EvidenceSource,
        LifecyclePhase,
    )

    events = []
    for claim in claims:
        payload = claim.to_dict()
        payload.update(
            {
                "status": "pending",
                "actual": "",
                "reason": "",
                "evidence_ids": [],
                "attempt": int(attempt or 0),
            }
        )
        if task_id:
            payload["task_id"] = task_id
        events.append(
            ledger.append(
                LifecyclePhase.VALIDATION,
                EventKind.CLAIM,
                EvidenceSource.ENGINE,
                payload,
            )
        )
    return events


def record_claim_verdicts(
    ledger: Any,
    verdicts: Iterable[ClaimVerdict],
    *,
    task_id: str = "",
    attempt: int = 0,
) -> list[Any]:
    """Append claim verdicts to the single evidence ledger.

    Claim text is not evidence; the payload contains the claim expectation and
    the gate's actual observation, so a replay can reconstruct the decision.
    """
    if ledger is None or not hasattr(ledger, "append"):
        return []
    from xenon.engine.evidence_runtime import (
        EventKind,
        EvidenceSource,
        LifecyclePhase,
    )

    events = []
    for verdict in verdicts:
        payload = verdict.to_dict()
        payload["attempt"] = int(attempt or 0)
        if task_id:
            payload["task_id"] = task_id
        events.append(
            ledger.append(
                LifecyclePhase.VALIDATION,
                EventKind.CLAIM,
                EvidenceSource.GATE,
                payload,
            )
        )
    return events


def repair_prompt(verdicts: Iterable[ClaimVerdict], *, limit: int = 5) -> str:
    """Render only rejected claims for the next targeted repair turn."""
    rows = [v for v in verdicts if v.status == REJECTED]
    if not rows:
        return ""
    return "；".join(v.repair_hint for v in rows[:limit])


def goal_failure_prompt(verdicts: Iterable[ClaimVerdict], *, limit: int = 8) -> str:
    """Render criterion-level unmet goals for a targeted continuation."""
    rows = [
        v for v in verdicts if v.status in {REJECTED, UNVERIFIABLE}
    ]
    if not rows:
        return ""
    lines = []
    for verdict in rows[:limit]:
        lines.append(
            f"- [{verdict.claim.claim_id}] {verdict.claim.kind}"
            + (f" ({verdict.claim.subject})" if verdict.claim.subject else "")
            + f": 期望 {verdict.claim.expected or verdict.claim.kind}；"
            + f"实际 {verdict.actual or '无'}；状态 {verdict.status}"
        )
    return "\n".join(lines)
