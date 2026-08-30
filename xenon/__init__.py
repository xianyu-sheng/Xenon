"""Xenon — Agent Harness：运行、约束、评测 AI 编程 Agent 的开源运行时。

公共 API
========

高层入口（推荐）
----------------
- ``run_task(task, *, workspace=..., engine=..., model_priority=...)`` —
  在受约束的工作区里跑一个任务，返回结果文本。默认绑定工作区围栏。

底层构件（需要自己组装时用）
----------------------------
- ``create_engine(name, model_priority, **kwargs)`` — 按范式名构造引擎实例
- ``list_engines()`` — 列出全部已注册推理范式名
- ``register_engine(...)`` — 注册自定义推理范式
- ``register_tool(...)`` — 注册自定义工具（带风险等级，纳入权限门）
- ``bind_workspace(engine, workspace)`` — 给引擎绑定工作区围栏
- ``AgentContext`` — 单次运行的状态总线
- ``SilentCallback`` / ``EngineCallback`` — 事件回调基类

安全默认
========
``run_task`` **默认绑定工作区围栏**（``ToolRuntime``），文件副作用被限制在
``workspace`` 内，且模型提供的 ``cwd`` 会被可信根覆盖。直接用
``create_engine`` 时不会自动绑定——那是底层构件，调用方需自行调用
``bind_workspace``，否则围栏根退化为进程 CWD（见 v0.8.5 release notes）。

懒加载
======
``import xenon`` 不触发引擎/REPL 子模块导入，保持 CLI 启动路径零开销；
首次访问某个名字时才解析对应实现。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅供类型检查器，运行时不导入（保持懒加载）
    from xenon.engine.callbacks import EngineCallback, SilentCallback
    from xenon.engine.context import AgentContext

__version__ = "0.9.1"

__all__ = [
    "__version__",
    # 高层
    "run_task",
    # 引擎
    "create_engine",
    "list_engines",
    "register_engine",
    # 工具
    "register_tool",
    # 约束
    "bind_workspace",
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
    "register_engine": ("xenon.engine.registry", "register_engine"),
    "register_tool": ("xenon.nodes.tool_registry", "register_tool_handler"),
}


def _create_engine(name: str, model_priority: list[str], **kwargs: Any) -> Any:
    """按范式名构造引擎实例。

    这是**底层构件**：不绑定工作区围栏。需要围栏时调用 ``bind_workspace``，
    或直接用 ``run_task``（默认已绑定）。

    Args:
        name: 范式标识（见 ``list_engines()``，如 "react"、"plan-execute"）。
        model_priority: 模型优先级列表，元素为 "provider/model" 形式。
        **kwargs: 透传给引擎工厂（如 callback、model_pool、temperature）。

    Raises:
        KeyError: 范式名未注册或该模式不走引擎循环（"direct"）。
    """
    from xenon.engine.registry import ENGINE_REGISTRY

    spec = ENGINE_REGISTRY.require(name)
    if spec.factory is None:
        raise KeyError(f"范式 '{name}' 不走引擎循环（无 factory），无法创建实例")
    return spec.factory(model_priority=model_priority, **kwargs)


def _list_engines() -> tuple[str, ...]:
    """列出全部已注册推理范式名（含内置 7 种与插件注册的）。"""
    from xenon.engine.registry import ENGINE_REGISTRY

    return ENGINE_REGISTRY.names()


def _bind_workspace(engine: Any, workspace: str | Any) -> None:
    """给引擎（及其子引擎）绑定工作区围栏。

    绑定后，所有文件工具的副作用被限制在 ``workspace`` 内，且模型提供的
    ``cwd`` 会被该可信根覆盖——模型无法通过参数移动围栏本身。子代理经
    ``spawn_agent`` 派生时自动继承同一围栏。

    Args:
        engine: ``create_engine`` 返回的引擎实例。
        workspace: 工作区根目录，必须已存在。

    Raises:
        ValueError: 目录不存在或不是目录。
    """
    from pathlib import Path

    from xenon.engine.tool_runtime import ToolRuntime, bind_tool_runtime

    bind_tool_runtime(engine, ToolRuntime(workspace_root=Path(workspace)))


def _run_task(
    task: str,
    *,
    workspace: str | Any = ".",
    engine: str = "react",
    model_priority: list[str] | None = None,
    callback: Any = None,
    context: Any = None,
    **engine_kwargs: Any,
) -> str:
    """在受约束的工作区里跑一个任务，返回结果文本。

    这是把 Xenon 当库用的推荐入口——它负责组装引擎、绑定工作区围栏、
    准备运行上下文，等价于 REPL 交互路径的约束强度。

    Args:
        task: 自然语言任务描述。
        workspace: 工作区根目录（默认当前目录）。文件副作用限制在此目录内。
        engine: 推理范式名（默认 "react"，见 ``list_engines()``）。
        model_priority: 模型优先级列表；``None`` 时从本地配置读取。
        callback: 事件回调；``None`` 时用 ``SilentCallback``（不输出到终端）。
        context: 可选的 ``AgentContext``；``None`` 时新建一个。
        **engine_kwargs: 其余参数透传给引擎工厂（如 temperature、max_iterations）。

    Returns:
        引擎的最终回答文本。

    Raises:
        ValueError: ``workspace`` 不是已存在的目录，或 ``task`` 为空。
        KeyError: ``engine`` 范式名未注册。
        RuntimeError: 所有模型均调用失败。

    Example::

        import xenon
        result = xenon.run_task(
            "给 utils.py 里的 parse_config 补类型标注",
            workspace="/path/to/repo",
            engine="react",
        )
    """
    from xenon.engine.callbacks import SilentCallback
    from xenon.engine.context import AgentContext

    if not isinstance(task, str) or not task.strip():
        raise ValueError("task 必须是非空字符串")

    resolved_models = model_priority or _default_model_priority()
    if not resolved_models:
        raise ValueError(
            "未提供 model_priority，且本地无可用模型配置。"
            "请显式传入（如 ['deepseek/deepseek-chat']）或先运行 `xenon` 的 /setup。"
        )

    built = _create_engine(
        engine,
        resolved_models,
        callback=callback if callback is not None else SilentCallback(),
        **engine_kwargs,
    )
    # 先绑定围栏再 run——晚于 run 则首个工具调用不受约束。
    _bind_workspace(built, workspace)
    return built.run(task, context if context is not None else AgentContext())


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
    "create_engine": _create_engine,
    "list_engines": _list_engines,
    "bind_workspace": _bind_workspace,
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
