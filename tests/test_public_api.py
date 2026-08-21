"""包级公共 API 测试（xenon/__init__.py 懒加载工厂）。

契约：
- import xenon 零子模块开销（不触发 engine/repl 导入）
- create_engine("react", ...) 构造真实 ReActEngine
- list_engines() 返回内置范式
- 非法名/无 factory 范式给出明确错误
"""

from __future__ import annotations

import sys

import pytest


class TestPublicAPI:
    def test_version_exported(self):
        import xenon

        assert isinstance(xenon.__version__, str)
        assert xenon.__version__.count(".") == 2

    def test_import_is_lazy(self):
        """import xenon 不得拉起 engine/repl 重型子模块。"""
        import subprocess

        code = (
            "import sys; import xenon; "
            "heavy = [m for m in sys.modules if m.startswith(('xenon.engine', 'xenon.repl'))]; "
            "print(','.join(heavy) or 'CLEAN')"
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        assert r.stdout.strip() == "CLEAN", f"懒加载失效，提前导入了: {r.stdout.strip()}"

    def test_list_engines_contains_builtins(self):
        import xenon

        names = xenon.list_engines()
        for expected in ("react", "plan-execute", "reflection"):
            assert expected in names, f"内置范式 {expected} 缺失: {names}"

    def test_create_engine_react(self):
        import xenon
        from xenon.engine.react_engine import ReActEngine

        eng = xenon.create_engine("react", model_priority=["prov/m1"])
        assert isinstance(eng, ReActEngine)

    def test_create_engine_unknown_name(self):
        import xenon

        with pytest.raises((KeyError, ValueError)):
            xenon.create_engine("no-such-engine", model_priority=["prov/m1"])

    def test_create_engine_direct_has_no_factory(self):
        """direct 不走引擎循环（registry.py EngineSpec.factory=None），应明确报错。"""
        import xenon

        with pytest.raises(KeyError, match="不走引擎循环"):
            xenon.create_engine("direct", model_priority=["prov/m1"])

    def test_dir_lists_public_api(self):
        import xenon

        d = dir(xenon)
        for name in ("create_engine", "list_engines", "__version__"):
            assert name in d

    def test_unknown_attribute_raises(self):
        import xenon

        with pytest.raises(AttributeError, match="has no attribute"):
            xenon.not_a_real_attribute  # noqa: B018
