from __future__ import annotations

import subprocess
import time
from pathlib import Path

import httpx
import pytest

from xenon.engine.base import BaseEngine
from xenon.engine.coding_contract import finalize_coding_run
from xenon.engine.combined_engines import PlanReflectionEngine, ReactReflectionEngine
from xenon.engine.execution_policy import (
    EngineDeadlineExceeded,
    ExecutionPolicy,
    bind_execution_policy,
    walk_engine_graph,
)
from xenon.engine.tool_runtime import ToolRuntime, bind_tool_runtime
from xenon.nodes.tool_executor import ToolExecutor
from xenon.engine.plan_execute_engine import PlanExecuteEngine
from xenon.engine.react_engine import ReActEngine


class _Engine(BaseEngine):
    def run(self, user_input, context=None, ctx_mgr=None):
        return self._call_llm([{"role": "user", "content": user_input}])


def test_absolute_deadline_shrinks_later_request_budget(monkeypatch):
    clock = iter([100.0, 103.5])
    monkeypatch.setattr(
        "xenon.engine.execution_policy.time.monotonic", lambda: next(clock)
    )
    policy = ExecutionPolicy(deadline_at=110.0, request_timeout=8.0)

    assert policy.request_budget("first") == 8.0
    assert policy.request_budget("later") == 6.5


def test_shared_policy_paces_requests_across_engine_graph(monkeypatch):
    clock = iter([100.0, 101.0])
    sleeps = []
    monkeypatch.setattr(
        "xenon.engine.execution_policy.time.monotonic", lambda: next(clock)
    )
    monkeypatch.setattr(
        "xenon.engine.execution_policy.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )
    policy = ExecutionPolicy(
        deadline_at=None,
        request_timeout=8.0,
        min_request_interval=3.0,
    )

    policy.wait_for_request_slot("planner")
    policy.wait_for_request_slot("reactor")

    assert sleeps == [2.0]
    assert policy._next_request_at == 106.0


def test_negative_request_interval_is_rejected():
    with pytest.raises(ValueError, match="min_request_interval"):
        ExecutionPolicy(deadline_at=None, min_request_interval=-1)


def test_provider_and_engine_retries_do_not_multiply(monkeypatch):
    calls = []

    def fail(_model, _messages, **options):
        calls.append(options)
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr("xenon.engine.base.chat_completion", fail)
    engine = _Engine(["provider/model"])
    bind_execution_policy(
        engine,
        ExecutionPolicy.from_timeout(
            30, request_timeout=5, provider_attempts=1, chain_retries=0
        ),
    )

    with pytest.raises(RuntimeError):
        engine.run("hello")

    assert len(calls) == 1
    assert calls[0]["max_retries"] == 1
    assert 0 < calls[0]["timeout"] <= 5


def test_deadline_stops_native_fallback_tiers(monkeypatch):
    calls = []
    engine = _Engine(["provider/model"])
    policy = ExecutionPolicy.from_timeout(30, request_timeout=5)
    bind_execution_policy(engine, policy)

    def expire(*_args, **_kwargs):
        calls.append("tier")
        policy.deadline_at = time.monotonic() - 1
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr("xenon.engine.base.chat_completion_with_tools", expire)

    # v0.8.3: 瞬时失败不再 raise「native provider request failed」，而是熔断
    # 回退文本协议；deadline 已过期时文本回退的 policy.check 抛
    # EngineDeadlineExceeded（停止语义更准确）。核心断言不变：只调 1 次 tier。
    with pytest.raises(EngineDeadlineExceeded):
        engine._call_llm_native(
            [{"role": "user", "content": "x"}],
            tools_schema=[{"type": "function", "function": {"name": "x"}}],
            response_format={"type": "json_object"},
        )

    assert calls == ["tier"]


def test_expired_deadline_prevents_any_native_request(monkeypatch):
    engine = _Engine(["provider/model"])
    policy = ExecutionPolicy(
        deadline_at=time.monotonic() - 1,
        request_timeout=5,
    )
    bind_execution_policy(engine, policy)
    monkeypatch.setattr(
        "xenon.engine.base.chat_completion_with_tools",
        lambda *_args, **_kwargs: pytest.fail("provider must not be called"),
    )

    with pytest.raises(TimeoutError, match="deadline"):
        engine._call_llm_native(
            [{"role": "user", "content": "x"}],
            tools_schema=[{"type": "function", "function": {"name": "x"}}],
            response_format={"type": "json_object"},
        )


@pytest.mark.parametrize("factory", [PlanReflectionEngine, ReactReflectionEngine])
def test_policy_reaches_planner_reactor_and_reflector(factory):
    engine = factory(["provider/model"])
    policy = ExecutionPolicy.from_timeout(20)

    bind_execution_policy(engine, policy)

    nodes = list(walk_engine_graph(engine))
    assert getattr(engine, "reflector") in nodes
    assert all(node.execution_policy is policy for node in nodes)
    tool_owner = getattr(engine, "reactor", None) or getattr(engine, "planner", None)
    assert tool_owner._tool_executor.execution_policy is policy


@pytest.mark.parametrize("factory", [ReActEngine, PlanExecuteEngine])
def test_standalone_engine_binds_policy_to_tool_executor(factory):
    """Direct construction must retain the engine deadline without a binder."""
    engine = factory(["provider/model"])
    assert engine._tool_executor.execution_policy is engine.execution_policy
    assert engine.execution_policy.deadline_at is not None


def test_tool_runtime_overrides_model_cwd_and_prefix(tmp_path, monkeypatch):
    captured = {}

    def fake_execute(self, _context):
        captured["cwd"] = self.cwd
        captured["prefix"] = self.command_prefix
        return {"success": True, "stdout": "ok"}

    monkeypatch.setattr("xenon.nodes.tool_executor.ToolNode.execute", fake_execute)
    runtime = ToolRuntime(
        tmp_path,
        command_prefix=("docker", "exec", "safe-container"),
        backend_workdir="/testbed",
    )
    result = ToolExecutor(runtime=runtime).execute(
        "command",
        {
            "action": "pwd",
            "cwd": "/tmp/other",
            "command_prefix": ("evil",),
        },
        __import__("xenon.engine.context", fromlist=["AgentContext"]).AgentContext(),
        tools={"command": {"name": "command"}},
    )

    assert result.success is True
    assert captured == {
        "cwd": str(tmp_path.resolve()),
        "prefix": ("docker", "exec", "safe-container"),
    }


def test_runtime_is_bound_to_every_tool_executor(tmp_path):
    engine = ReactReflectionEngine(["provider/model"])
    runtime = ToolRuntime(tmp_path)

    bind_tool_runtime(engine, runtime)

    assert engine.reactor._tool_executor.runtime is runtime
    assert engine.repairer._tool_executor.runtime is runtime


def _repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "value.txt").write_text("old\n")
    subprocess.run(["git", "add", "value.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=path, check=True)


def test_coding_contract_prefers_real_workspace_diff(tmp_path):
    _repo(tmp_path)
    (tmp_path / "value.txt").write_text("workspace\n")

    result = finalize_coding_run(
        tmp_path,
        "diff --git a/value.txt b/value.txt\n--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-old\n+answer\n",
    )

    assert result.patch_source == "workspace"
    assert "+workspace" in result.patch
    assert "+answer" not in result.patch


def test_coding_contract_applies_only_valid_unified_diff(tmp_path):
    _repo(tmp_path)
    patch = """```diff
diff --git a/value.txt b/value.txt
index 3367afd..3e75765 100644
--- a/value.txt
+++ b/value.txt
@@ -1 +1 @@
-old
+new
```
"""

    result = finalize_coding_run(tmp_path, patch)

    assert result.patch_source == "unified_diff"
    assert result.apply_success is True
    assert (tmp_path / "value.txt").read_text() == "new\n"


def test_coding_contract_never_guesses_prose_or_bad_patch(tmp_path):
    _repo(tmp_path)
    prose = finalize_coding_run(tmp_path, "Change value.txt from old to new.")
    bad = finalize_coding_run(
        tmp_path,
        "diff --git a/value.txt b/value.txt\n--- a/value.txt\n+++ b/value.txt\n@@ -9 +9 @@\n-x\n+y\n",
    )

    assert prose.patch_source == bad.patch_source == "empty"
    assert (tmp_path / "value.txt").read_text() == "old\n"
