"""Xenon — 面向实验、学习和社区协作的可扩展终端 AI 编程 Agent。

公共 API（v0.8.4 起）:

- ``__version__`` — 版本号（pyproject.toml 同步）
- ``create_engine(name, model_priority=..., **kwargs)`` — 按范式名构造引擎实例，
  走 EngineRegistry 的稳定扩展契约（docs/ARCHITECTURE.md）
- ``list_engines()`` — 列出全部已注册推理范式名

实现为懒加载：``import xenon`` 不触发引擎/REPL 子模块导入，保持 CLI
启动路径零开销；首次调用 ``create_engine`` 时才解析工厂。
"""

from __future__ import annotations

from typing import Any

__version__ = "0.8.5"

__all__ = ["__version__", "create_engine", "list_engines"]


def __getattr__(name: str) -> Any:
    """PEP 562 懒加载——仅响应公共工厂函数。"""
    if name == "create_engine":
        from xenon.engine.registry import ENGINE_REGISTRY

        def create_engine(
            name: str,
            model_priority: list[str],
            **kwargs: Any,
        ):
            """按范式名构造引擎实例。

            Args:
                name: 范式标识（见 ``list_engines()``，如 "react"、"plan-execute"）。
                model_priority: 模型优先级列表，元素为 "provider/model" 形式。
                **kwargs: 透传给引擎工厂（如 callback、model_pool、temperature）。

            Raises:
                KeyError: 范式名未注册或该模式不走引擎循环（"direct"）。
            """
            spec = ENGINE_REGISTRY.require(name)
            if spec.factory is None:
                raise KeyError(
                    f"范式 '{name}' 不走引擎循环（无 factory），无法创建实例"
                )
            return spec.factory(model_priority=model_priority, **kwargs)

        return create_engine
    if name == "list_engines":

        def list_engines() -> tuple[str, ...]:
            """列出全部已注册推理范式名（含内置 7 种与插件注册的）。"""
            from xenon.engine.registry import ENGINE_REGISTRY

            return ENGINE_REGISTRY.names()

        return list_engines
    raise AttributeError(f"module 'xenon' has no attribute '{name}'")


def __dir__() -> list[str]:
    return sorted(__all__)
