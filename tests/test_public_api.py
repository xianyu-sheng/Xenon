"""公共 API 回归测试 — 锁定库使用契约。

Xenon 既是 CLI 也是库；``xenon/__init__.py`` 导出的是面向外部调用者的
稳定接口。本文件保证：

1. ``__all__`` 声明的成员全部可解析（懒加载不死链）
2. ``run_task`` 走 kernel loop，工作区围栏由 gateway 绑定
3. 错误路径（空 task、不存在的 workspace、无模型）给出清晰报错
4. ``register_tool`` 注册成功后在全局可见
5. 引擎工厂类 API（create_engine / register_engine / bind_workspace）已随引擎层退役

设计原则：``run_task`` **必须**运行在受约束的工作区内，且**不产生交付提交**
（库调用不替用户决定提交）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import xenon
from xenon.modes import MODE_CATALOG


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


class TestModes:
    """There is one loop: ``list_engines`` lists the thinking modes."""

    def test_list_engines_returns_mode_names(self):
        modes = xenon.list_engines()
        assert isinstance(modes, tuple)
        assert {"react", "plan-execute", "reflection", "plan-react"} <= set(modes)
        assert set(modes) == set(MODE_CATALOG)

    def test_engine_factories_are_retired(self):
        """create_engine / register_engine / bind_workspace went with the engines."""
        for name in ("create_engine", "register_engine", "bind_workspace"):
            assert name not in xenon.__all__
            with pytest.raises(AttributeError):
                getattr(xenon, name)


class TestRunTask:
    """SDK entry: kernel loop, workspace fence, no delivery commit."""

    def test_run_task_builds_a_loop_strategy(self, tmp_path):
        captured: dict = {}

        class FakeRun:
            result = SimpleNamespace(answer="OK")

        def fake_run_task(objective, **kwargs):
            captured.update(kwargs)
            captured["objective"] = objective
            return FakeRun()

        with patch("xenon.harness.runner.run_task", fake_run_task):
            answer = xenon.run_task(
                "task",
                workspace=str(tmp_path),
                engine="plan-react",
                model_priority=["x/y"],
            )

        assert answer == "OK"
        assert captured["objective"] == "task"
        assert captured["workspace"] == tmp_path
        assert captured["finalize"] is False, "库调用不能产生提交"
        strategy = captured["strategy"]
        assert strategy.profile.plan is True
        assert strategy.profile.step_scope is True

    def test_run_task_empty_task_raises(self, tmp_path):
        with pytest.raises(ValueError, match="必须是非空字符串"):
            xenon.run_task("", workspace=str(tmp_path), model_priority=["x/y"])

    def test_run_task_none_task_raises(self, tmp_path):
        with pytest.raises(ValueError, match="必须是非空字符串"):
            xenon.run_task(None, workspace=str(tmp_path), model_priority=["x/y"])

    def test_run_task_nonexistent_workspace_raises(self):
        with pytest.raises(ValueError, match="不是已存在的目录"):
            xenon.run_task("task", workspace="/nope/nope/nope", model_priority=["x/y"])

    def test_run_task_unknown_engine_falls_back_to_bare_react(self, tmp_path):
        """未知 mode 名不会静默变成别的模式：它只用裸 react 阶段。"""
        captured: dict = {}

        class FakeRun:
            result = SimpleNamespace(answer="ok")

        def fake_run_task(objective, **kwargs):
            captured.update(kwargs)
            return FakeRun()

        with patch("xenon.harness.runner.run_task", fake_run_task):
            xenon.run_task(
                "task",
                workspace=str(tmp_path),
                engine="no-such",
                model_priority=["x/y"],
            )

        assert captured["strategy"].profile.plan is False

    def test_run_task_no_models_and_no_config_raises(self, tmp_path, monkeypatch):
        """无显式 model_priority 且无本地配置时，给出可操作的报错。"""
        monkeypatch.setattr("xenon._default_model_priority", lambda: [])
        with pytest.raises(ValueError, match="未提供 model_priority"):
            xenon.run_task("task", workspace=str(tmp_path))


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
