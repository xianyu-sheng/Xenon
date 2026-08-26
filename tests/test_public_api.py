"""公共 API 回归测试 — 锁定库使用契约。

Xenon 既是 CLI 也是库；``xenon/__init__.py`` 导出的是面向外部调用者的
稳定接口。本文件保证：

1. ``__all__`` 声明的成员全部可解析（懒加载不死链）
2. ``run_task`` 默认绑定工作区围栏，且绑定先于 ``run()``
3. ``bind_workspace`` 真的把 ``cwd`` 覆盖为可信根
4. 错误路径（空 task、不存在的 workspace、未知引擎）给出清晰报错
5. ``register_tool`` / ``register_engine`` 注册成功后在全局可见

设计原则：``run_task`` **必须**默认绑定围栏——否则库用户落在 v0.8.5
修复前的脆弱路径（模型自选 cwd 可移动围栏本身）。``create_engine`` 是
底层构件，不自动绑定，调用方需自行调用 ``bind_workspace``。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import xenon


class TestLazyLoading:
    """懒加载：import xenon 不触发子模块导入。"""

    def test_import_does_not_load_submodules(self):
        """``import xenon`` 零子模块开销，保持 CLI 启动路径快。"""
        # 实际测试需要在独立进程里跑（本测试所在进程已导过），
        # 这里只能检查 __getattr__ 机制是否正确连线。
        assert hasattr(xenon, "__getattr__")
        assert callable(xenon.__dir__)

    def test_all_exports_resolve(self):
        """``__all__`` 声明的成员全部可解析，懒加载不死链。"""
        for name in xenon.__all__:
            if name == "__version__":
                assert isinstance(xenon.__version__, str)
            else:
                obj = getattr(xenon, name)  # 解析失败这里就抛了
                assert obj is not None

    def test_unknown_attribute_raises(self):
        with pytest.raises(AttributeError, match="no attribute 'nonexistent'"):
            _ = xenon.nonexistent


class TestEngineFactories:
    """引擎工厂：create_engine / list_engines。"""

    def test_list_engines_returns_builtin_paradigms(self):
        engines = xenon.list_engines()
        assert isinstance(engines, tuple)
        assert "react" in engines
        assert "plan-execute" in engines
        assert len(engines) >= 7  # 内置 7 种

    def test_create_engine_requires_factory(self):
        """'direct' 模式无 factory，不能 create_engine。"""
        with pytest.raises(KeyError, match="不走引擎循环"):
            xenon.create_engine("direct", ["x/y"])

    def test_create_engine_unknown_name_raises(self):
        with pytest.raises(KeyError, match="未注册的引擎"):
            xenon.create_engine("no-such-paradigm", ["x/y"])

    def test_create_engine_returns_engine_with_no_runtime(self):
        """底层构件 create_engine 不自动绑定围栏 — 调用方自行 bind_workspace。"""
        from xenon.engine.callbacks import SilentCallback

        engine = xenon.create_engine(
            "react", ["deepseek/deepseek-chat"], callback=SilentCallback()
        )
        assert engine is not None
        executor = getattr(engine, "_tool_executor", None)
        assert executor is not None
        assert executor.runtime is None  # 未绑定


class TestWorkspaceFence:
    """工作区围栏：bind_workspace 必须覆盖模型提供的 cwd。"""

    def test_bind_workspace_sets_runtime(self, tmp_path):
        from xenon.engine.callbacks import SilentCallback

        engine = xenon.create_engine(
            "react", ["deepseek/deepseek-chat"], callback=SilentCallback()
        )
        xenon.bind_workspace(engine, tmp_path)
        executor = engine._tool_executor
        assert executor.runtime is not None
        assert executor.runtime.workspace_root == tmp_path.resolve()

    def test_bind_workspace_overwrites_model_cwd(self, tmp_path):
        """绑定后模型给的 cwd 被覆盖为可信根，无法移动围栏本身。"""
        from xenon.engine.callbacks import SilentCallback

        engine = xenon.create_engine(
            "react", ["deepseek/deepseek-chat"], callback=SilentCallback()
        )
        xenon.bind_workspace(engine, tmp_path)
        params = engine._tool_executor._runtime_params(
            {"file_path": "x.txt", "cwd": "/tmp/attacker-chosen"}
        )
        assert params["cwd"] == str(tmp_path.resolve())

    def test_bind_workspace_nonexistent_raises(self):
        from xenon.engine.callbacks import SilentCallback

        engine = xenon.create_engine(
            "react", ["deepseek/deepseek-chat"], callback=SilentCallback()
        )
        with pytest.raises(ValueError, match="not a directory"):
            xenon.bind_workspace(engine, "/nonexistent/definitely-not-here")


class TestRunTask:
    """高层入口 run_task：默认绑定围栏，错误路径清晰报错。"""

    def test_run_task_binds_before_run(self, tmp_path):
        """run_task 必须先 bind 后 run — 晚于 run 则首个工具调用不受约束。"""
        order = []
        fake_engine = MagicMock()
        fake_engine.run.side_effect = lambda *a, **k: (order.append("run"), "OK")[1]

        with (
            patch("xenon._create_engine", return_value=fake_engine),
            patch(
                "xenon._bind_workspace", side_effect=lambda e, w: order.append("bind")
            ),
        ):
            result = xenon.run_task(
                "task", workspace=str(tmp_path), model_priority=["x/y"]
            )

        assert order == ["bind", "run"], f"绑定未先于 run：{order}"
        assert result == "OK"

    def test_run_task_empty_task_raises(self, tmp_path):
        with pytest.raises(ValueError, match="必须是非空字符串"):
            xenon.run_task("", workspace=str(tmp_path), model_priority=["x/y"])

    def test_run_task_none_task_raises(self, tmp_path):
        with pytest.raises(ValueError, match="必须是非空字符串"):
            xenon.run_task(None, workspace=str(tmp_path), model_priority=["x/y"])

    def test_run_task_nonexistent_workspace_raises(self):
        with pytest.raises(ValueError, match="not a directory"):
            xenon.run_task("task", workspace="/nope/nope/nope", model_priority=["x/y"])

    def test_run_task_unknown_engine_raises(self, tmp_path):
        with pytest.raises(KeyError, match="未注册的引擎"):
            xenon.run_task(
                "task",
                workspace=str(tmp_path),
                engine="no-such",
                model_priority=["x/y"],
            )

    def test_run_task_no_models_and_no_config_raises(self, tmp_path, monkeypatch):
        """无显式 model_priority 且无本地配置时，给出可操作的报错。"""
        # 让 _default_model_priority 返回空（模拟无配置）
        monkeypatch.setattr("xenon._default_model_priority", lambda: [])
        with pytest.raises(ValueError, match="未提供 model_priority"):
            xenon.run_task("task", workspace=str(tmp_path))

    def test_run_task_uses_silent_callback_by_default(self, tmp_path):
        """不传 callback 时默认用 SilentCallback，不输出到终端。"""
        fake_engine = MagicMock()
        fake_engine.run.return_value = "done"

        with (
            patch("xenon._create_engine") as mock_create,
            patch("xenon._bind_workspace"),
        ):
            xenon.run_task("task", workspace=str(tmp_path), model_priority=["x/y"])
            # 检查 create_engine 是否被调用时带了 SilentCallback
            assert mock_create.called
            call_kwargs = mock_create.call_args.kwargs
            from xenon.engine.callbacks import SilentCallback

            assert isinstance(call_kwargs.get("callback"), SilentCallback)


class TestToolRegistration:
    """工具注册：register_tool 应纳入全局 BUILTIN_TOOL_REGISTRY。"""

    def test_register_tool_makes_tool_visible(self):
        """注册后工具出现在 plugin_schemas() 里，LLM 能看见它。"""
        from xenon.nodes.tool_registry import BUILTIN_TOOL_REGISTRY

        @xenon.register_tool(
            "test_custom_tool", description="测试工具", params={"x": "输入"}
        )
        def handler(node, context):
            return {"status": "ok"}

        schemas = BUILTIN_TOOL_REGISTRY.plugin_schemas()
        assert "test_custom_tool" in schemas
        assert schemas["test_custom_tool"]["description"] == "测试工具"

    def test_register_tool_default_risk_is_sensitive(self):
        """不指定 risk 时默认 SENSITIVE，不会静默绕过权限确认。"""
        from xenon.nodes.tool_registry import BUILTIN_TOOL_REGISTRY

        @xenon.register_tool("test_risky_tool", description="有风险", replace=True)
        def handler(node, context):
            return {}

        defn = BUILTIN_TOOL_REGISTRY.get("test_risky_tool")
        assert defn is not None
        assert defn.risk == "SENSITIVE"


class TestEngineRegistration:
    """引擎注册：register_engine 应出现在 list_engines() 里。"""

    def test_register_engine_makes_paradigm_visible(self):
        """注册后范式名出现在 list_engines()，create_engine 能用它。"""
        from xenon.engine.base import BaseEngine

        class DummyEngine(BaseEngine):
            def run(self, user_input, context=None, ctx_mgr=None):
                return "dummy"

        xenon.register_engine(
            name="test-dummy",
            factory=lambda **kw: DummyEngine(model_priority=["x/y"], **kw),
            mode_line="测试",
            result_title="测试结果",
        )
        assert "test-dummy" in xenon.list_engines()
