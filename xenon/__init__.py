"""Xenon — Agent Harness：运行、约束、评测 AI 编程 Agent 的开源运行时。

公共 API
========

高层入口（推荐）
----------------
- ``run_task(task, *, workspace=..., engine=..., model_priority=...)`` —
  在受约束的工作区里跑一个任务，返回结果文本。默认绑定工作区围栏。

底层构件（需要自己组装时用）
----------------------------
- ``list_engines()`` — 列出可选 thinking mode（原“引擎”名，现为同一 loop 的阶段配置）
- ``register_tool(...)`` — 注册自定义工具（带风险等级，纳入权限门）
- ``AgentContext`` — 单次运行的状态总线
- ``SilentCallback`` / ``EngineCallback`` — 事件回调基类

安全默认
========
``run_task`` **默认绑定工作区围栏**：文件副作用被限制在 ``workspace`` 内，
模型提供的 ``cwd`` 会被可信根覆盖。库调用不产生交付提交——提交权在调用方。

懒加载
======
``import xenon`` 不触发循环/REPL 子模块导入，保持 CLI 启动路径零开销；
首次访问某个名字时才解析对应实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅供类型检查器，运行时不导入（保持懒加载）
    from xenon.engine.callbacks import EngineCallback, SilentCallback
    from xenon.engine.context import AgentContext

__version__ = "0.9.7"

__all__ = [
    "__version__",
    # 高层
    "run_task",
    # 高层（带判定）：accepted / evidence / debt / ledger 指针
    "run_task_result",
    "TaskOutcome",
    # 模式（原“引擎”入口，现在是同一 loop 的 thinking modes）
    "list_engines",
    # 工具
    "register_tool",
    # 类型
    "AgentContext",
    "EngineCallback",
    "SilentCallback",
]

# 名字 -> (模块路径, 模块内属性名)。懒加载直接转发的成员。
_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "AgentContext": ("xenon.engine.context", "AgentContext"),
    "EngineCallback": ("xenon.engine.callbacks", "EngineCallback"),
    "SilentCallback": ("xenon.engine.callbacks", "SilentCallback"),
    "register_tool": ("xenon.nodes.tool_registry", "register_tool_handler"),
    "TaskOutcome": ("xenon", "TaskOutcome"),
}


def _list_modes() -> tuple[str, ...]:
    """List the selectable thinking modes (the retired "engines" surface).

    Kept under the old name so existing callers keep working; the values are
    modes now, because there is exactly one loop.
    """

    from xenon.modes import MODE_CATALOG

    return tuple(MODE_CATALOG)


@dataclass(frozen=True)
class TaskOutcome:
    """What a library run actually established, not just what it said.

    ``str(outcome)`` is the answer, so call sites that only want text keep
    working; everything else (did the harness accept it? what evidence level?
    which ledger?) is available without scraping a ledger by hand.
    """

    task_id: str
    answer: str
    accepted: bool
    phase: str
    evidence: str
    debt: tuple[str, ...]
    finalize_sha: str | None
    ledger_path: str
    # Why it stopped, when it stopped without debt to explain it.  "blocked"
    # alone is not an answer a caller can act on.
    reason: str = ""

    def __str__(self) -> str:
        return self.answer


def _run_task(
    task: str,
    *,
    workspace: str | Any = ".",
    engine: str = "react",
    model_priority: list[str] | None = None,
    callback: Any = None,
    context: Any = None,
    permission: Any = None,
    allow_approval: bool = False,
    _want_outcome: bool = False,
    **engine_kwargs: Any,
) -> Any:
    """在受约束的工作区里跑一个任务，返回结果文本。

    这是把 Xenon 当库用的推荐入口——它负责组装循环、绑定工作区围栏、
    准备运行上下文，等价于 REPL 交互路径的约束强度。

    Args:
        task: 自然语言任务描述。
        workspace: 工作区根目录（默认当前目录）。文件副作用限制在此目录内。
        mode: 模式名（默认 "react"，见 ``list_engines()``）；同一循环的阶段配置。
        model_priority: 模型优先级列表；``None`` 时从本地配置读取。
        callback: 事件回调；``None`` 时用 ``SilentCallback``（不输出到终端）。
        context: 可选的 ``AgentContext``；``None`` 时新建一个。
        **engine_kwargs: 其余参数透传给循环工厂（如 temperature、max_iterations）。

    Returns:
        循环的最终回答文本。

    Raises:
        ValueError: ``workspace`` 不是已存在的目录，或 ``task`` 为空。
        KeyError: ``engine`` 模式名未注册。
        RuntimeError: 所有模型均调用失败。

    Example::

        import xenon
        result = xenon.run_task(
            "给 utils.py 里的 parse_config 补类型标注",
            workspace="/path/to/repo",
            engine="react",
        )
    """
    from pathlib import Path as _Path

    if not isinstance(task, str) or not task.strip():
        raise ValueError("task 必须是非空字符串")
    workspace_dir = _Path(str(workspace))
    if not workspace_dir.is_dir():
        raise ValueError(f"workspace 不是已存在的目录: {workspace_dir}")

    resolved_models = model_priority or _default_model_priority()
    if not resolved_models:
        raise ValueError(
            "未提供 model_priority，且本地无可用模型配置。"
            "请显式传入（如 ['deepseek/deepseek-chat']）或先运行 `xenon` 的 /setup。"
        )

    from xenon.harness.llm import build_llm_loop_strategy
    from xenon.harness.runner import run_task as _kernel_run_task
    from xenon.kernel.strategies import LoopProfile
    from xenon.modes import loop_profile_for

    profile = loop_profile_for(engine) or LoopProfile()
    strategy = build_llm_loop_strategy(
        _sdk_model_config(resolved_models[0]), profile
    )
    run = _kernel_run_task(
        task,
        user_objective=task,
        strategy=strategy,
        workspace=workspace_dir,
        permission=_sdk_permission(permission),
        # A library call must not create commits; the caller owns delivery.
        finalize=False,
        max_steps=int(engine_kwargs.get("max_steps", 24) or 24),
        gateway_context=context,
        allow_approval=bool(allow_approval),
    )
    del engine_kwargs, callback
    if _want_outcome:
        return _outcome(run)  # type: ignore[return-value]
    return run.result.answer


def run_task_result(
    task: str,
    *,
    workspace: str | Any = ".",
    engine: str = "react",
    model_priority: list[str] | None = None,
    permission: Any = None,
    allow_approval: bool = False,
    **engine_kwargs: Any,
) -> Any:
    """Like :func:`run_task`, but return the verdict instead of the prose.

    A harness that can say "accepted" must expose it.  Returning only the
    answer string forces callers to re-open the ledger to learn whether the
    run actually delivered, which defeats the point of the ledger.
    """

    return _run_task(
        task,
        workspace=workspace,
        engine=engine,
        model_priority=model_priority,
        permission=permission,
        allow_approval=allow_approval,
        _want_outcome=True,
        **engine_kwargs,
    )


def _outcome(run: Any) -> Any:
    result = run.result
    reason = ""
    if not result.accepted and not getattr(result, "debt", ()):
        from xenon.replay import blocking_reason

        reason = blocking_reason(run.ledger_path, task_id=str(run.task_id))
    return TaskOutcome(
        task_id=str(run.task_id),
        answer=str(result.answer),
        accepted=bool(result.accepted),
        phase=getattr(getattr(result, "phase", None), "value", ""),
        evidence=getattr(
            getattr(result, "evidence_status", None), "value", ""
        ),
        debt=tuple(str(item.reason) for item in getattr(result, "debt", ())),
        finalize_sha=getattr(result, "finalize_sha", None),
        ledger_path=str(run.ledger_path),
        reason=reason,
    )


def _sdk_model_config(model_id: str) -> Any:
    """Model config for a library call, carrying the model's real window.

    The registry knows each model's context window (1M for DeepSeek V4, 128K
    for a GLM-class model).  A library call must not fall back to an unknown
    window, or the agent sizes its working set by guesswork.
    """

    from xenon.harness.llm import ModelConfig

    try:
        from xenon.repl.model_registry import ModelRegistry

        configured = ModelRegistry().get_model_by_id(model_id)
    except Exception:  # noqa: BLE001 — the registry is optional for library use
        configured = None
    if configured is None:
        return ModelConfig(model_id=model_id)
    return ModelConfig(
        model_id=getattr(configured, "model_id", model_id),
        base_url=getattr(configured, "base_url", ""),
        api_key=getattr(configured, "api_key", ""),
        max_tokens=max(256, int(getattr(configured, "max_tokens", 8192) or 8192)),
        temperature=0.0,
        reasoning_effort=getattr(configured, "reasoning_effort", ""),
        context_window=int(getattr(configured, "context_window", 0) or 0),
    )


def _sdk_permission(value: Any = None) -> Any:
    """Resolve the SDK permission mode.

    Default is ``AUTO_EDIT``: worktree edits are allowed, but running tests or
    a shell command needs an explicit grant.  A library caller has no UI to
    approve anything, so the default makes ``verify`` fail closed — pass
    ``permission="bypass"`` (or build an approval provider) when the task is
    expected to run commands.
    """

    from xenon.kernel.policies import PermissionMode

    if value is None:
        return PermissionMode.AUTO_EDIT
    if isinstance(value, PermissionMode):
        return value
    return PermissionMode(str(value))


def _default_model_priority() -> list[str]:
    """从 ``~/.xenon/models.yaml`` 推断模型优先级；读不到时返回空列表。

    库调用方通常不想重复声明模型；能复用 CLI ``/setup`` 的结果最省事。
    读取失败（无配置/格式变动）不抛异常——由 ``run_task`` 给出可操作的报错。
    """
    try:
        from pathlib import Path

        from xenon.repl.model_registry import ModelRegistry

        config = Path.home() / ".xenon" / "models.yaml"
        if not config.is_file():
            return []
        registry = ModelRegistry()
        registry.load_from_file(config)
        # 无显式角色分配时，get_role_priority 回退为全部模型的默认顺序。
        return list(registry.get_role_priority("planner"))
    except Exception:  # noqa: BLE001 — 配置缺失是正常情形，不是错误
        return []


_FACTORIES: dict[str, Any] = {
    "run_task": _run_task,
    "list_engines": _list_modes,
}


def __getattr__(name: str) -> Any:
    """PEP 562 懒加载——只响应 ``__all__`` 声明的公共成员。"""
    if name in _FACTORIES:
        return _FACTORIES[name]
    target = _LAZY_EXPORTS.get(name)
    if target is not None:
        import importlib

        return getattr(importlib.import_module(target[0]), target[1])
    raise AttributeError(f"module 'xenon' has no attribute '{name}'")


def __dir__() -> list[str]:
    return sorted(__all__)
