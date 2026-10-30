"""Gateway adapter over the legacy seven-stage ToolExecutor.

The adapter owns the translation from a tool result to typed effects.  Legacy
handlers keep working; the kernel only sees ``ToolReceipt`` facts.  The gateway
also turns off the legacy evidence ledger: the kernel ledger is the only
durable source.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from xenon.engine.context import AgentContext
from xenon.engine.tool_runtime import ToolRuntime
from xenon.engine.tool_tracker import ToolExecutionTracker
from xenon.kernel.contracts import (
    Capability,
    FsWrite,
    NetworkFact,
    Opaque,
    PolicyDecisionKind,
    ReadFact,
    ReceiptStatus,
    TestRun,
    ToolCall,
    ToolReceipt,
    ToolSpec,
    VcsCommit,
    VcsPush,
)
from xenon.kernel.protocols import BoundToolPolicy
from xenon.nodes.tool_executor import ToolExecuteResult, ToolExecutor

_WRITE_TOOLS = frozenset(
    {
        "write_file",
        "edit_file",
        "append_file",
        "batch_write",
        "batch_edit",
        "apply_patch",
        "refactor",
        "edit_with_llm",
    }
)
_READ_TOOLS = frozenset({"read", "read_file", "read_document"})
_TEST_TOOLS = frozenset({"run_tests"})
_SHELL_TOOLS = frozenset({"bash", "command", "run_script"})
_NETWORK_TOOLS = frozenset(
    {"web_fetch", "docs_fetch", "github_fetch", "mcp_call"}
)
_COMMIT_RE = re.compile(r"\[[^\]]*?([0-9a-f]{7,40})[^\]]*\]", re.IGNORECASE)
_COMMIT_META = (";", "&&", "||", "|", "`", "$(")


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item)]
    return []


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _targets(params: Mapping[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in ("file_path", "path"):
        value = params.get(key)
        if isinstance(value, str) and value:
            paths.append(value)
    for key in ("files", "edits"):
        values = params.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, Mapping):
                continue
            value = item.get("file_path") or item.get("path")
            if isinstance(value, str) and value:
                paths.append(value)
    seen: set[str] = set()
    result: list[str] = []
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        result.append(path)
    return result


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _test_target(
    params: Mapping[str, Any], meta: Mapping[str, Any]
) -> str:
    explicit = (
        meta.get("test_target")
        or meta.get("test_paths")
        or params.get("test_paths")
        or params.get("file_path")
    )
    if isinstance(explicit, (list, tuple)):
        explicit = ",".join(str(item) for item in explicit)
    if explicit:
        return str(explicit)
    command = str(meta.get("command") or params.get("command") or "")
    for token in command.replace("\\", "/").split():
        candidate = token.strip("\"'")
        if candidate.endswith(".py") and "test" in candidate.lower():
            return candidate
    return command


def _commit_sha(*texts: Any) -> str:
    for text in texts:
        if not isinstance(text, str):
            continue
        match = _COMMIT_RE.search(text)
        if match:
            return match.group(1)
    return ""


def _command_text(params: Mapping[str, Any]) -> str:
    return str(
        params.get("command")
        or params.get("git_command")
        or params.get("action")
        or ""
    ).strip()


def _commit_tokens(command: str) -> list[str]:
    if not command or any(marker in command for marker in _COMMIT_META):
        return []
    try:
        return [
            item.strip("\"'").casefold()
            for item in shlex.split(command, posix=False)
            if item.strip()
        ]
    except ValueError:
        return []


def _looks_like_commit(tokens: list[str]) -> bool:
    if not tokens:
        return False
    if tokens[:2] == ["git", "commit"] or tokens[:1] == ["commit"]:
        return True
    return "commit" in tokens and "git" in tokens


def _normalize_repo_path(value: str, workspace: Path) -> str:
    candidate = Path(value)
    if candidate.is_absolute():
        try:
            candidate = candidate.resolve(strict=False).relative_to(
                workspace.resolve()
            )
        except (OSError, ValueError):
            return candidate.as_posix()
    text = candidate.as_posix()
    while text.startswith("./"):
        text = text[2:]
    return text


class LegacyToolGateway:
    """Translate ``ToolExecutor`` results into kernel receipts."""

    def __init__(
        self,
        executor: ToolExecutor | None = None,
        *,
        specs: Sequence[ToolSpec] = (),
        workspace: str | Path | None = None,
        context: AgentContext | None = None,
        tracker: ToolExecutionTracker | None = None,
        policy: BoundToolPolicy | None = None,
        runtime: Any = None,
    ) -> None:
        self._executor = executor or ToolExecutor(record_evidence=False)
        self._specs = {spec.name: spec for spec in specs}
        self._workspace = Path(workspace).resolve() if workspace else None
        self._context = context or AgentContext()
        self._tracker = tracker or ToolExecutionTracker()
        self._policy = policy
        self._runtime = runtime
        if self._runtime is not None:
            # An injected runtime (e.g. an eval sandbox / container) wins over
            # the host default: the kernel still decides *what* runs, the
            # injected runtime decides *where* it runs.
            if hasattr(self._executor, "set_runtime"):
                self._executor.set_runtime(self._runtime)
        elif self._workspace is not None and hasattr(
            self._executor, "set_runtime"
        ):
            self._executor.set_runtime(
                ToolRuntime(workspace_root=self._workspace)
            )

    def execute(self, call: ToolCall, services: Any) -> ToolReceipt:
        started = time.time()
        if self._workspace is None:
            raw_workspace = str(getattr(services, "workspace", "") or "")
            if raw_workspace:
                candidate = Path(raw_workspace).resolve()
                if candidate.is_dir():
                    self._workspace = candidate
                    if hasattr(self._executor, "set_runtime"):
                        self._executor.set_runtime(
                            ToolRuntime(workspace_root=candidate)
                        )
        workspace = self._workspace or Path(
            getattr(services, "workspace", "") or "."
        ).resolve()
        spec = self._specs.get(call.spec_name)
        if self._policy is not None:
            decision = self._policy.check_bound(spec, call)
            if decision.kind is not PolicyDecisionKind.ALLOW:
                return self._denied_receipt(
                    call,
                    services,
                    spec,
                    reason=decision.reason or "permission denied",
                    started=started,
                )
        commit_denied = self._precommit_denial(call, services, workspace)
        if commit_denied:
            return self._denied_receipt(
                call,
                services,
                spec,
                reason=commit_denied,
                started=started,
            )

        execution_call = self._execution_call(call)
        before: dict[str, bool] = {}
        if call.spec_name in _WRITE_TOOLS:
            for path in _targets(call.params):
                candidate = Path(path)
                if not candidate.is_absolute():
                    candidate = workspace / candidate
                exists = candidate.exists()
                before[path] = exists
                before[str(candidate.resolve())] = exists
        try:
            result = self._executor.execute(
                execution_call.spec_name,
                dict(execution_call.params),
                self._context,
                self._tracker,
            )
        except Exception as exc:  # noqa: BLE001 — a receipt must still exist
            result = ToolExecuteResult(
                tool_name=call.spec_name,
                success=False,
                observation="",
                error=str(exc),
            )
        return self._receipt(
            call,
            result,
            services,
            started=started,
            ended=time.time(),
            workspace=workspace,
            before=before,
        )

    @staticmethod
    def _execution_call(call: ToolCall) -> ToolCall:
        """Translate a per-tool MCP spec back to the one MCP executor entry."""

        if call.capability is Capability.REMOTE and call.spec_name != "mcp_call":
            params = dict(call.params)
            params["tool_name"] = call.spec_name
            return ToolCall(
                spec_name="mcp_call",
                params=params,
                capability=Capability.REMOTE,
            )
        return call

    def _precommit_denial(
        self, call: ToolCall, services: Any, workspace: Path
    ) -> str:
        command = _command_text(call.params)
        tokens = _commit_tokens(command)
        if not _looks_like_commit(tokens):
            if any(marker in command for marker in _COMMIT_META):
                lowered = command.casefold()
                if "commit" in lowered and "git" in lowered:
                    return "unsafe commit command"
            return ""
        if any(
            token
            in {
                "-c",
                "--git-dir",
                "--work-tree",
                "--no-verify",
            }
            for token in tokens
        ):
            return "commit uses an alternate repository or worktree"
        for key in ("repo_path", "cwd"):
            raw = call.params.get(key)
            if not raw:
                continue
            candidate = Path(str(raw))
            if not candidate.is_absolute():
                candidate = workspace / candidate
            try:
                if candidate.resolve(strict=False) != workspace.resolve():
                    return "commit uses an alternate repository or worktree"
            except OSError:
                return "commit uses an alternate repository or worktree"
        verified = getattr(services, "verified_paths", None)
        if verified is None:
            return ""
        staged = self._staged_paths(workspace)
        if staged is None:
            return "cannot inspect staged paths; refusing commit"
        if not staged:
            return "staged diff is empty; refusing commit"
        allowed = {
            _normalize_repo_path(path, workspace) for path in verified
        }
        staged_set = {
            _normalize_repo_path(path, workspace) for path in staged
        }
        unverified = sorted(staged_set - allowed)
        if unverified:
            return "commit contains unverified staged paths: " + ", ".join(
                unverified[:10]
            )
        return ""

    @staticmethod
    def _staged_paths(workspace: Path) -> tuple[str, ...] | None:
        try:
            process = subprocess.run(
                ["git", "diff", "--cached", "--name-only", "-z"],
                cwd=workspace,
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if process.returncode != 0:
            return None
        payload = process.stdout.decode("utf-8", errors="replace")
        return tuple(item for item in payload.split("\0") if item)

    def _denied_receipt(
        self,
        call: ToolCall,
        services: Any,
        spec: ToolSpec | None,
        *,
        reason: str,
        started: float,
    ) -> ToolReceipt:
        digest = hashlib.sha256(
            json.dumps(
                [call.spec_name, sorted(call.params.items(), key=str)],
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()[:12]
        receipt_id = f"denied-{digest}"
        declared = (
            tuple(sorted(spec.effects, key=lambda item: item.value))
            if spec is not None
            else ()
        )
        return ToolReceipt(
            receipt_id=receipt_id,
            call_id=f"{call.spec_name}-{receipt_id}",
            task_id=str(getattr(services, "task_id", "") or ""),
            step_id=getattr(services, "current_step_id", None),
            tool=call.spec_name,
            status=ReceiptStatus.DENIED,
            effects=(),
            declared_effects=declared,
            observed_effects=(),
            error=reason,
            started_at=started,
            ended_at=time.time(),
        )

    def _receipt(
        self,
        call: ToolCall,
        result: ToolExecuteResult,
        services: Any,
        *,
        started: float,
        ended: float,
        workspace: Path,
        before: Mapping[str, bool],
    ) -> ToolReceipt:
        meta = self._metadata(result)
        effects = tuple(
            self._effects(call, result, meta, workspace=workspace, before=before)
        )
        spec = self._specs.get(call.spec_name)
        declared = (
            tuple(sorted(spec.effects, key=lambda item: item.value))
            if spec is not None
            else tuple(effect.kind for effect in effects)
        )
        observed = tuple(effect.kind for effect in effects)
        receipt_id = result.result_id or f"receipt-{uuid.uuid4().hex[:12]}"
        status = (
            ReceiptStatus.TIMEOUT
            if result.timed_out
            else (
                ReceiptStatus.OK if result.success else ReceiptStatus.FAILED
            )
        )
        exit_code = _as_int(meta.get("exit_code"))
        if exit_code is None:
            exit_code = _as_int(meta.get("returncode"))
        return ToolReceipt(
            receipt_id=receipt_id,
            call_id=f"{call.spec_name}-{receipt_id}",
            task_id=str(getattr(services, "task_id", "") or ""),
            step_id=getattr(services, "current_step_id", None),
            tool=call.spec_name,
            status=status,
            exit_code=exit_code,
            effects=effects,
            declared_effects=declared,
            observed_effects=observed,
            raw_ref=result.result_id,
            error=result.error or "",
            started_at=started,
            ended_at=ended,
        )

    @staticmethod
    def _metadata(result: ToolExecuteResult) -> dict[str, Any]:
        raw = result.raw if isinstance(result.raw, dict) else {}
        structured = (
            result.structured.metadata
            if result.structured is not None
            else {}
        )
        meta: dict[str, Any] = dict(raw)
        meta.update(structured)
        return meta

    def _effects(
        self,
        call: ToolCall,
        result: ToolExecuteResult,
        meta: Mapping[str, Any],
        *,
        workspace: Path,
        before: Mapping[str, bool],
    ) -> list[Any]:
        params = call.params
        effects: list[Any] = []
        changed_paths = _as_list(meta.get("changed_paths"))
        targets = _targets(params)
        write_tool = call.spec_name in _WRITE_TOOLS
        if changed_paths or (write_tool and targets):
            paths = tuple(changed_paths or targets)
            changed = bool(meta.get("changed", True))
            def _absent(path: str) -> bool:
                if path in before:
                    return before[path] is False
                candidate = Path(path)
                if not candidate.is_absolute():
                    candidate = workspace / candidate
                return before.get(str(candidate.resolve())) is False

            created = [
                path
                for path in _as_list(meta.get("created_paths"))
                if _absent(path)
            ]
            if write_tool and not created:
                created = [path for path in paths if _absent(path)]
            hashes = {}
            for path in paths:
                candidate = Path(path)
                if not candidate.is_absolute():
                    candidate = workspace / candidate
                digest = _sha256_file(candidate)
                if digest:
                    hashes[path] = digest
            effects.append(
                FsWrite(
                    paths=paths,
                    hashes=hashes,
                    changed=changed,
                    created_paths=tuple(created),
                )
            )

        tests_run = _as_int(meta.get("tests_run"))
        if tests_run is not None or call.spec_name in _TEST_TOOLS:
            target = _test_target(params, meta)
            effects.append(
                TestRun(
                    target=target,
                    framework=str(
                        meta.get("framework")
                        or params.get("test_framework")
                        or "pytest"
                    ),
                    tests_run=int(tests_run or 0),
                    passed=int(_as_int(meta.get("tests_passed")) or 0),
                    failed=int(_as_int(meta.get("tests_failed")) or 0),
                    errors=int(_as_int(meta.get("tests_errors")) or 0),
                    skipped=int(_as_int(meta.get("tests_skipped")) or 0),
                    exit_code=int(_as_int(meta.get("exit_code")) or 0),
                    command=str(meta.get("command") or params.get("command") or ""),
                )
            )

        command = str(meta.get("command") or params.get("command") or "")
        committed_paths = _as_list(meta.get("committed_paths"))
        commit_like = bool(
            committed_paths or "commit" in command.lower()
        )
        if result.success and commit_like:
            sha = _commit_sha(
                meta.get("commit"),
                meta.get("sha"),
                meta.get("stdout"),
                meta.get("content"),
                meta.get("output"),
            )
            if sha or committed_paths:
                effects.append(
                    VcsCommit(
                        sha=sha,
                        files=tuple(committed_paths),
                    )
                )

        if result.success and "git push" in command.lower():
            effects.append(
                VcsPush(
                    remote=str(meta.get("remote") or "origin"),
                    branch=str(meta.get("branch") or ""),
                    sha=_commit_sha(meta.get("stdout"), meta.get("content")),
                )
            )

        if call.spec_name in _READ_TOOLS and result.success:
            path = str(
                meta.get("file_path")
                or meta.get("path")
                or params.get("file_path")
                or params.get("path")
                or ""
            )
            if path:
                content = (
                    result.raw.get("content")
                    if isinstance(result.raw, dict)
                    else None
                )
                digest = (
                    hashlib.sha256(content.encode("utf-8")).hexdigest()
                    if isinstance(content, str)
                    else ""
                )
                effects.append(
                    ReadFact(
                        path=path,
                        content_hash=digest,
                        content=content if isinstance(content, str) else "",
                    )
                )

        if call.capability is Capability.REMOTE or call.spec_name in _NETWORK_TOOLS:
            url = str(
                meta.get("url")
                or meta.get("source")
                or params.get("url")
                or ""
            )
            if call.spec_name == "mcp_call" or call.capability is Capability.REMOTE:
                tool_name = str(
                    params.get("tool_name")
                    or params.get("tool")
                    or call.spec_name
                    or ""
                ).strip()
                if not url or not url.startswith(
                    ("http://", "https://", "mcp://")
                ):
                    # MCP metadata often calls the tool name "source"; keep a
                    # stable remote URI instead of treating it as a web URL.
                    url = f"mcp://{tool_name or url or 'call'}"
            if url:
                content = meta.get("content")
                if not isinstance(content, str):
                    content = ""
                effects.append(
                    NetworkFact(
                        url=url,
                        status=int(
                            _as_int(meta.get("status_code"))
                            or (200 if result.success else 0)
                        ),
                        content=content[:8000],
                    )
                )

        if call.spec_name in _SHELL_TOOLS and command:
            effects.append(
                Opaque(
                    command=command,
                    exit_code=int(_as_int(meta.get("exit_code")) or 0),
                    workspace_delta=tuple(changed_paths),
                )
            )
        return effects
