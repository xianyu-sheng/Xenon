"""引擎注册表 — 推理范式的稳定扩展契约。

``docs/ARCHITECTURE.md`` 把 ``register_engine()`` 列为四条扩展契约之一。本模块
落地它：新增一种推理范式只需注册一个 ``EngineSpec``，不必再逐一修改 REPL 的
dispatch 分支、``BUILTIN_MODES``、setup wizard 文案和 evals 白名单。

设计要点：

* **只描述差异。** 七种内置范式的运行流程完全同构（日志捕获 → 构造引擎 →
  注入 MCP 工具 → ``run()`` → 落 trace → 渲染 → 异常收尾）。差异只有引擎类、
  少量调参 kwargs、模式行文案和结果面板标题。``EngineSpec`` 只承载这些差异，
  公共流程由 REPL 的单一 runner 负责。
* **惰性导入。** ``factory`` 在调用时才 import 引擎模块，保持 ``xenon`` 的
  零启动开销特性（见 ARCHITECTURE.md「惰性加载」）。
* **direct 不是引擎。** ``direct`` 模式直接调 LLM、不走引擎循环，因此其
  ``factory`` 为 ``None``；REPL 据此走独立分支。把它也放进注册表，是为了让
  ``/mode`` 列表和 evals 白名单有唯一来源。

新增一种范式：

    from xenon.engine.registry import register_engine

    register_engine(
        "tot",
        factory=lambda **kw: TreeOfThoughtsEngine(branches=3, **kw),
        description="Tree-of-Thoughts 多路径探索",
        mode_line="· ToT 多路径展开 → 评分 → 收敛",
        result_title="ToT 结果",
    )
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# factory 收到 REPL 组装好的公共 kwargs（model_priority / model_pool /
# auto_router / callback / model_configs / permission_gate），返回引擎实例。
EngineFactory = Callable[..., Any]


@dataclass(frozen=True)
class EngineSpec:
    """一种推理范式的完整描述。

    Attributes:
        name: 范式标识，``/mode`` 与 ``--engine`` 使用（如 ``"plan-execute"``）。
        description: ``/mode`` 列表与 setup wizard 展示的一句话说明。
        factory: 构造引擎实例；``None`` 表示该模式不走引擎循环（仅 ``direct``）。
        mode_line: 运行时的模式提示行（Ctrl+O 展开时显示）。
        result_title: 结果面板标题。
        error_label: 异常提示里的范式名；缺省时由 ``result_title`` 推导。
        preserve_thinking_panel: 异常时是否保留 thinking 面板。ReAct 需要它，
            否则重试/LLM 报错后 Ctrl+O 只剩原始日志、工具时间线看起来丢了。
        log_result_diagnostics: 是否在 ``run()`` 返回后记录结果诊断日志。
            用于排查空白面板类问题。
    """

    name: str
    description: str = ""
    factory: EngineFactory | None = None
    mode_line: str = ""
    result_title: str = ""
    error_label: str = ""
    preserve_thinking_panel: bool = False
    log_result_diagnostics: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """异常与日志里使用的范式名。"""
        if self.error_label:
            return self.error_label
        if self.result_title.endswith(" 结果"):
            return self.result_title[: -len(" 结果")]
        return self.result_title or self.name

    @property
    def runs_engine(self) -> bool:
        """是否走引擎循环（``direct`` 为 False）。"""
        return self.factory is not None


class EngineRegistry:
    """进程内的推理范式注册表。

    内置范式采用**首次读取时惰性加载**：``xenon.engine.builtin_engines`` 只在
    第一次查询注册表时导入。这样既保持 Xenon 的零启动开销（不查注册表就不导入
    引擎相关模块），又避免注册表内容依赖调用方的 import 顺序 —— 后者曾让
    ``ENGINE_REGISTRY`` 在未导入 ``model_registry`` 时表现为空表。

    ``builtin_engines`` 只 import 本模块，引擎类的 import 都在 factory 内部，
    因此不构成循环导入。
    """

    def __init__(self, *, autoload_builtins: bool = False) -> None:
        self._specs: dict[str, EngineSpec] = {}
        self._autoload = autoload_builtins
        self._loaded = not autoload_builtins

    def _ensure_builtins(self) -> None:
        """幂等地加载内置范式声明。"""
        if self._loaded:
            return
        # 先置位再导入：builtin_engines 会回调 register()，若不先置位会无限递归。
        self._loaded = True
        import xenon.engine.builtin_engines  # noqa: F401

    def register(self, spec: EngineSpec, *, replace: bool = False) -> EngineSpec:
        name = str(spec.name).strip()
        if not name:
            raise ValueError("引擎名不能为空")
        if name in self._specs and not replace:
            raise ValueError(f"引擎已注册: {name}（如需覆盖请传 replace=True）")
        self._specs[name] = spec
        return spec

    def get(self, name: str) -> EngineSpec | None:
        self._ensure_builtins()
        return self._specs.get(str(name))

    def require(self, name: str) -> EngineSpec:
        """取出 spec，未注册时显式报错而不是静默回落。

        静默回落是此前的实际行为：``/mode`` 设置成功但 REPL 的 if/elif 链没有
        对应分支时，会落到 ``else`` 走 direct，用户以为在跑新范式其实在跑
        direct，且没有任何提示。
        """
        spec = self.get(name)
        if spec is None:
            available = ", ".join(sorted(self._specs)) or "(空)"
            raise KeyError(f"未注册的引擎: {name}。可用: {available}")
        return spec

    def contains(self, name: str) -> bool:
        self._ensure_builtins()
        return str(name) in self._specs

    def names(self) -> tuple[str, ...]:
        self._ensure_builtins()
        return tuple(self._specs)

    def items(self) -> tuple[tuple[str, EngineSpec], ...]:
        self._ensure_builtins()
        return tuple(self._specs.items())

    def unregister(self, name: str) -> EngineSpec | None:
        self._ensure_builtins()
        return self._specs.pop(str(name), None)


# 进程级注册表。内置范式在首次查询时加载，见 EngineRegistry._ensure_builtins。
ENGINE_REGISTRY = EngineRegistry(autoload_builtins=True)


def register_engine(
    name: str,
    *,
    factory: EngineFactory | None = None,
    description: str = "",
    mode_line: str = "",
    result_title: str = "",
    error_label: str = "",
    preserve_thinking_panel: bool = False,
    log_result_diagnostics: bool = False,
    replace: bool = False,
    **metadata: Any,
) -> EngineSpec:
    """注册一种推理范式。

    这是 ``docs/ARCHITECTURE.md`` 承诺的稳定扩展契约之一。
    """
    return ENGINE_REGISTRY.register(
        EngineSpec(
            name=name,
            description=description,
            factory=factory,
            mode_line=mode_line,
            result_title=result_title,
            error_label=error_label,
            preserve_thinking_panel=preserve_thinking_panel,
            log_result_diagnostics=log_result_diagnostics,
            metadata=metadata,
        ),
        replace=replace,
    )
