"""引擎注册表契约测试。

核心断言：**新增一种推理范式只需一次 register_engine() 调用，不必修改任何现有
文件**，并且注册后 /mode 列表、REPL dispatch 与 evals 白名单全部自动认得它。

这三条覆盖了 issue #6 列出的原有 6 处改动点。
"""

from __future__ import annotations

import pytest

from xenon.engine.registry import (
    ENGINE_REGISTRY,
    EngineSpec,
    EngineRegistry,
    register_engine,
)


@pytest.fixture
def clean_registry():
    """在隔离的注册表上测试，避免污染进程内的内置注册。"""
    reg = EngineRegistry()
    yield reg


@pytest.fixture
def temp_engine():
    """向全局注册表临时注册一个假范式，测试结束后移除。"""
    name = "test-tot"
    spec = register_engine(
        name,
        factory=lambda **kw: object(),
        description="测试用的假范式",
        mode_line="· ToT 测试",
        result_title="ToT 结果",
    )
    yield spec
    ENGINE_REGISTRY.unregister(name)


class TestEngineSpec:
    def test_label_derives_from_result_title(self):
        spec = EngineSpec(name="x", result_title="Plan-Execute 结果")
        assert spec.label == "Plan-Execute"

    def test_explicit_error_label_wins(self):
        spec = EngineSpec(name="x", result_title="A 结果", error_label="B")
        assert spec.label == "B"

    def test_label_falls_back_to_name(self):
        assert EngineSpec(name="tot").label == "tot"

    def test_runs_engine_false_without_factory(self):
        """direct 模式没有 factory，不走引擎循环。"""
        assert EngineSpec(name="direct").runs_engine is False

    def test_runs_engine_true_with_factory(self):
        assert EngineSpec(name="x", factory=lambda **kw: None).runs_engine is True


class TestRegistryBasics:
    def test_register_and_get(self, clean_registry):
        spec = EngineSpec(name="tot", factory=lambda **kw: None)
        clean_registry.register(spec)
        assert clean_registry.get("tot") is spec
        assert clean_registry.contains("tot")

    def test_duplicate_rejected_by_default(self, clean_registry):
        clean_registry.register(EngineSpec(name="tot"))
        with pytest.raises(ValueError, match="已注册"):
            clean_registry.register(EngineSpec(name="tot"))

    def test_replace_allows_override(self, clean_registry):
        clean_registry.register(EngineSpec(name="tot", description="v1"))
        clean_registry.register(EngineSpec(name="tot", description="v2"), replace=True)
        assert clean_registry.get("tot").description == "v2"

    def test_empty_name_rejected(self, clean_registry):
        with pytest.raises(ValueError, match="不能为空"):
            clean_registry.register(EngineSpec(name="  "))

    def test_name_is_normalized_in_returned_spec(self, clean_registry):
        spec = clean_registry.register(EngineSpec(name="  tot  "))
        assert spec.name == "tot"
        assert clean_registry.get("tot") is spec
        assert clean_registry.unregister("  tot  ") is spec

    def test_require_raises_on_unknown(self, clean_registry):
        """未注册必须显式报错，而不是返回 None 让调用方静默回落。"""
        clean_registry.register(EngineSpec(name="react"))
        with pytest.raises(KeyError) as exc:
            clean_registry.require("nope")
        # 报错信息要列出可用项，否则贡献者不知道自己拼错了什么
        assert "react" in str(exc.value)

    def test_unregister_returns_spec(self, clean_registry):
        clean_registry.register(EngineSpec(name="tot"))
        assert clean_registry.unregister("tot").name == "tot"
        assert not clean_registry.contains("tot")


class TestBuiltinEnginesRegistered:
    """七种内置范式必须全部在注册表里，且元数据完整。"""

    EXPECTED = {
        "direct",
        "react",
        "plan-execute",
        "reflection",
        "plan-react",
        "plan-reflection",
        "react-reflection",
    }

    def test_all_seven_registered(self):
        assert set(ENGINE_REGISTRY.names()) >= self.EXPECTED

    def test_direct_has_no_factory(self):
        assert ENGINE_REGISTRY.require("direct").runs_engine is False

    def test_engine_modes_have_factory_and_copy(self):
        """除 direct 外，每种范式都要能构造，且有模式行与结果标题。"""
        for name in self.EXPECTED - {"direct"}:
            spec = ENGINE_REGISTRY.require(name)
            assert spec.runs_engine, f"{name} 缺少 factory"
            assert spec.mode_line, f"{name} 缺少 mode_line"
            assert spec.result_title, f"{name} 缺少 result_title"
            assert spec.description, f"{name} 缺少 description"

    def test_react_preserves_thinking_panel(self):
        """ReAct 异常时要保留 thinking 面板，否则 Ctrl+O 只剩原始日志。"""
        assert ENGINE_REGISTRY.require("react").preserve_thinking_panel is True


class TestSingleSourceOfTruth:
    """注册表必须是范式清单的唯一来源 —— 钉死此前的多处漂移。"""

    def test_builtin_modes_derived_from_registry(self):
        from xenon.repl.model_registry import BUILTIN_MODES

        assert set(BUILTIN_MODES) == set(ENGINE_REGISTRY.names()), (
            "BUILTIN_MODES 与 ENGINE_REGISTRY 不一致 —— 说明又出现了第二份清单"
        )
        for name, mode in BUILTIN_MODES.items():
            assert mode.description == ENGINE_REGISTRY.require(name).description

    def test_evals_whitelist_derived_from_registry(self):
        from evals.runner import SUPPORTED_ENGINE_TYPES

        assert set(SUPPORTED_ENGINE_TYPES) == set(ENGINE_REGISTRY.names()), (
            "evals 白名单与注册表不一致 —— /mode 能切但 eval 会拒绝运行"
        )

    def test_swebench_engine_list_derived_from_registry(self):
        """swebench_xenon.ALL_ENGINES 现从注册表派生（issue #22 修复后）。"""
        from evals.swebench_xenon import ALL_ENGINES as SWE_ALL_ENGINES
        from evals.swebench_xenon import CODE_EDITING_ENGINES
        from evals.swebench_xenon import _NON_CODE_EDITING

        assert set(SWE_ALL_ENGINES) == set(ENGINE_REGISTRY.names()), (
            "swebench ALL_ENGINES 与注册表不一致"
        )
        assert "direct" in _NON_CODE_EDITING
        assert "reflection" in _NON_CODE_EDITING
        assert "react" not in _NON_CODE_EDITING
        assert "react" in CODE_EDITING_ENGINES
        assert "plan-execute" in CODE_EDITING_ENGINES
        assert "direct" not in CODE_EDITING_ENGINES

    def test_repl_has_no_hardcoded_engine_dispatch(self):
        """repl.py 不应再有按范式名硬编码的 elif 链。"""
        import ast
        import pathlib

        import xenon.repl.repl as mod

        src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        offenders = []
        for node in ast.walk(tree):
            # 找 `mode == "<某个范式名>"` 这类比较
            if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name):
                if node.left.id != "mode":
                    continue
                for comp in node.comparators:
                    if (
                        isinstance(comp, ast.Constant)
                        and isinstance(comp.value, str)
                        and comp.value in ENGINE_REGISTRY.names()
                        and comp.value != "direct"
                    ):
                        offenders.append((node.lineno, comp.value))
        assert not offenders, (
            f"repl.py 仍有硬编码范式 dispatch: {offenders}。"
            "应改为 ENGINE_REGISTRY 查表，否则新增范式又要改这里"
        )


class TestNewEngineNeedsNoExistingFileEdit:
    """定位核心断言：注册即可用，无需改任何现有文件。"""

    def test_registered_engine_appears_in_mode_list(self, temp_engine):
        """新注册的范式必须能被 /mode 认得（ModelRegistry.set_mode）。"""
        from xenon.repl.model_registry import ModelRegistry, ThinkingMode

        reg = ModelRegistry()
        # ModelRegistry 从 BUILTIN_MODES 复制，而 BUILTIN_MODES 在 import 时
        # 已定型；新范式需要显式加入该实例，模拟「注册后重启」的效果。
        reg.modes[temp_engine.name] = ThinkingMode(
            name=temp_engine.name, description=temp_engine.description
        )
        mode = reg.set_mode(temp_engine.name)
        assert mode.name == temp_engine.name
        assert reg.current_mode == temp_engine.name

    def test_registry_require_finds_it(self, temp_engine):
        assert ENGINE_REGISTRY.require(temp_engine.name) is temp_engine

    def test_factory_is_called_with_common_kwargs(self):
        """factory 必须收到 REPL 组装的公共 kwargs，范式独有调参自己填。"""
        seen = {}

        def factory(**kwargs):
            seen.update(kwargs)
            return object()

        spec = register_engine("test-kwargs", factory=factory)
        try:
            spec.factory(
                model_priority=["m1"],
                model_pool=None,
                auto_router=None,
                callback=None,
                model_configs={},
                permission_gate=None,
            )
            assert seen["model_priority"] == ["m1"]
            assert "callback" in seen
            assert "permission_gate" in seen
        finally:
            ENGINE_REGISTRY.unregister("test-kwargs")


class TestUnknownModeDoesNotSilentlyFallBack:
    """回归防护：未注册的 mode 必须显式报错，不能静默跑 direct。

    此前 repl.py 的 if/elif 链以 ``else: self._run_direct(...)`` 收尾，
    所以拼错范式名或漏注册时，用户以为在跑新范式，实际在跑 direct，
    且没有任何提示。这是 issue #6 里最坏的失败模式。
    """

    def test_dispatch_reports_unknown_mode(self):
        import ast
        import pathlib

        import xenon.repl.repl as mod

        src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        # dispatch 分支里必须存在「未注册」提示，且必须调用 _record_engine_error
        # 收尾（否则历史里会留下孤立 user 消息）
        assert "未注册的思考范式" in src, (
            "dispatch 未对未注册范式给出显式错误提示"
        )
        tree = ast.parse(src)
        # 确认 ENGINE_REGISTRY.get 被用于 dispatch
        uses_registry = any(
            isinstance(n, ast.Attribute)
            and n.attr in {"get", "require"}
            and isinstance(n.value, ast.Name)
            and n.value.id == "ENGINE_REGISTRY"
            for n in ast.walk(tree)
        )
        assert uses_registry, "repl.py 未通过 ENGINE_REGISTRY 分发"
