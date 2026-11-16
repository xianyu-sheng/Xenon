"""Regression tests for the agent slash-command group boundary."""

from __future__ import annotations

from xenon.repl.command_groups.agent import (
    _cmd_ask,
    _cmd_code,
    _cmd_run,
    _cmd_sub_agent,
    _ordered_model_candidates,
)
from xenon.repl.commands import (
    _cmd_ask as legacy_ask,
)
from xenon.repl.commands import (
    _cmd_code as legacy_code,
)
from xenon.repl.commands import (
    _cmd_run as legacy_run,
)
from xenon.repl.commands import (
    _cmd_sub_agent as legacy_sub_agent,
)


def test_agent_group_preserves_legacy_command_exports():
    assert legacy_ask is _cmd_ask
    assert legacy_code is _cmd_code
    assert legacy_run is _cmd_run
    assert legacy_sub_agent is _cmd_sub_agent


def test_ask_needs_model():
    from xenon.repl.context_manager import ContextManager
    from xenon.repl.model_registry import ModelRegistry

    reg = ModelRegistry()
    ctx = ContextManager()
    result = _cmd_ask(args="", registry=reg, ctx_mgr=ctx)
    assert "用法" in result


def test_ask_uses_model_output_budget_and_endpoint(monkeypatch):
    from xenon.repl.context_manager import ContextManager
    from xenon.repl.model_registry import ModelRegistry

    reg = ModelRegistry()
    reg.add_model(
        "openai/deepseek-v4-pro",
        "pro",
        api_key="sk-private",
        base_url="https://relay.example/v1",
        max_tokens=256000,
        reasoning_effort="max",
    )
    captured = {}

    def fake_chat(model_id, messages, **kwargs):
        captured.update(kwargs)
        return "完整回答。"

    monkeypatch.setattr("xenon.utils.llm_client.chat_completion", fake_chat)

    assert (
        _cmd_ask(
            args="pro 解释架构",
            registry=reg,
            ctx_mgr=ContextManager(),
        )
        == "完整回答。"
    )
    assert captured["max_tokens"] == 256000
    assert captured["credentials"] == {"openai": "sk-private"}
    assert captured["base_url"] == "https://relay.example/v1"
    assert captured["reasoning_effort"] == "max"


def test_code_needs_task():
    from xenon.repl.context_manager import ContextManager
    from xenon.repl.model_registry import ModelRegistry

    reg = ModelRegistry()
    ctx = ContextManager()
    result = _cmd_code(args="", registry=reg, ctx_mgr=ctx, session_state={})
    assert "用法" in result


def test_ordered_model_candidates_lead_with_the_session_choice():
    """The model the user selected (alias form) must be tried first.

    ``--model`` / ``/set_model`` write ``_preferred_model_ids``; a command that
    reads only the role list ran on a dead proxy while the status bar showed a
    working model.
    """

    from types import SimpleNamespace

    from xenon.repl.model_registry import ModelRegistry

    reg = ModelRegistry()
    reg.add_model("vendor/dead", "dead")
    reg.add_model("vendor/live", "live")

    candidates = _ordered_model_candidates(
        reg, SimpleNamespace(_preferred_model_ids=["live"]), "coder"
    )
    assert candidates == ["vendor/live", "vendor/dead"]


def test_code_falls_back_when_the_first_model_is_rejected(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from xenon.repl.context_manager import ContextManager
    from xenon.repl.model_registry import ModelRegistry

    reg = ModelRegistry()
    reg.add_model("vendor/dead", "dead")
    reg.add_model("vendor/live", "live")
    calls: list[str] = []

    def fake_chat(model_id, messages, **kwargs):
        calls.append(model_id)
        if model_id == "vendor/dead":
            raise RuntimeError("402 Payment Required")
        return "print(sum(range(1, 6)))"

    monkeypatch.setattr("xenon.utils.llm_client.chat_completion", fake_chat)

    target = tmp_path / "sum5.py"
    ctx = ContextManager()
    result = _cmd_code(
        args=f"求和 --file {target}",
        registry=reg,
        ctx_mgr=ctx,
        session_state={},
        repl=SimpleNamespace(_preferred_model_ids=["dead"]),
    )

    assert "✅ 代码已写入" in result
    assert target.read_text(encoding="utf-8") == "print(sum(range(1, 6)))"
    # The session's model is tried first; a rejection falls through to the next.
    assert calls == ["vendor/dead", "vendor/live"]
    assert ctx.history[-1].model_used == "vendor/live"
    # /review reads this key; the writer must fill it.
    assert ctx.get_working_memory()["session_created_files"] == [
        str(target.absolute())
    ]


def test_run_needs_workflow():
    from xenon.repl.model_registry import ModelRegistry

    reg = ModelRegistry()
    result = _cmd_run(args="", session_state={}, registry=reg)
    assert isinstance(result, str)


def test_sub_agent_no_args():
    result = _cmd_sub_agent(args="", session_state={})
    assert "委派子 Agent" in result


def test_sub_agent_no_repl():
    result = _cmd_sub_agent(args="do something", session_state={}, repl=None)
    assert "无法获取 REPL 实例" in result


class _Ctx(dict):
    def get(self, key, default=None):
        return super().get(key, default)

    def set(self, key, value):
        self[key] = value


class _Pool:
    def __init__(self):
        from types import SimpleNamespace

        self._models = [SimpleNamespace(model_id="test/fake")]

    def get_healthy(self):
        return self._models

    def list_all(self):
        return self._models


def _sub_agent_repl(tmp_path, *, level: int, mode: str = ""):
    from types import SimpleNamespace

    from xenon.kernel.session_grants import GrantBook
    from xenon.repl.model_registry import ModelRegistry

    registry = ModelRegistry()
    registry.add_model("test/fake", "fake")
    ctx = _Ctx({"_execution_level": level})
    if mode:
        ctx.set("_permission_mode", mode)
    return SimpleNamespace(
        project_ctx=SimpleNamespace(root=tmp_path, working_dir=tmp_path),
        registry=registry,
        model_pool=_Pool(),
        agent_context=ctx,
        _grant_book=GrantBook(workspace=str(tmp_path)),
        _last_kernel_task_id="",
        _last_kernel_ledger=None,
    )


def test_sub_agent_starts_bounded_read_only_child(monkeypatch, tmp_path):
    """Regression: /agent used to read a nonexistent ``repl._execution_level``.

    The parent was therefore always READ_ONLY while the child kept its
    AUTO_EDIT default, the subset check raised, and no sub-task could start.
    The child must be read-only (architecture §5) and never above the parent.
    """

    from xenon.kernel.policies import PermissionMode

    captured: dict = {}

    from types import SimpleNamespace

    def fake_run_child_task(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            delivered=True,
            objective="研究一下项目结构",
            summary=lambda: "🧬 ok",
        )

    # The command imports the child API inside its body, so the patch target is
    # the source module.
    monkeypatch.setattr("xenon.harness.child.run_child_task", fake_run_child_task)
    monkeypatch.setattr(
        "xenon.harness.child.reconcile_child_records", lambda *a, **k: []
    )

    repl = _sub_agent_repl(tmp_path, level=3)  # an EXECUTE-level session
    result = _cmd_sub_agent(
        args="研究一下项目结构", session_state={"agent_context": repl.agent_context},
        repl=repl,
    )

    assert "子任务" in result
    assert captured["permission"] is PermissionMode.READ_ONLY
    assert captured["parent_permission"] is not None


class _TwoModelPool:
    def __init__(self):
        from types import SimpleNamespace

        self._models = [
            SimpleNamespace(model_id="test/fake"),
            SimpleNamespace(model_id="test/preferred"),
        ]

    def get_healthy(self):
        return self._models

    def list_all(self):
        return self._models


def test_sub_agent_runs_the_session_model_first(monkeypatch, tmp_path):
    """The child must inherit the model the user selected, not pool order."""

    from types import SimpleNamespace

    captured: dict = {}

    def fake_run_child_task(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            delivered=True,
            objective="读文件",
            summary=lambda: "🧬 ok",
        )

    monkeypatch.setattr("xenon.harness.child.run_child_task", fake_run_child_task)
    monkeypatch.setattr(
        "xenon.harness.child.reconcile_child_records", lambda *a, **k: []
    )

    repl = _sub_agent_repl(tmp_path, level=1)
    repl.registry.add_model("test/preferred", "preferred")
    repl.model_pool = _TwoModelPool()
    repl._preferred_model_ids = ["preferred"]

    result = _cmd_sub_agent(args="读文件", session_state={}, repl=repl)

    assert "子任务" in result
    assert captured["model"].model_id == "test/preferred"


def test_child_permission_never_exceeds_the_parent(tmp_path):
    from xenon.harness.child import child_permission_for
    from xenon.kernel.policies import PermissionMode

    assert child_permission_for(None) is PermissionMode.READ_ONLY
    assert child_permission_for(PermissionMode.READ_ONLY) is PermissionMode.READ_ONLY
    assert child_permission_for(PermissionMode.AUTO_EDIT) is PermissionMode.READ_ONLY
    assert child_permission_for(PermissionMode.BYPASS) is PermissionMode.READ_ONLY
