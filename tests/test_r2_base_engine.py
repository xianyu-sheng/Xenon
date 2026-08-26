"""R2 验收：BaseEngine 抽象基类。

- 三个基础执行引擎继承 BaseEngine；
- _call_llm 单一来源（子类 __dict__ 不再各自携带副本）；
- 参数漂移消除：temperature 集中（react/plan/reflection=0.3）；
- observation 截断阈值统一为可配属性。
"""

from types import SimpleNamespace

from xenon.engine.base import BaseEngine
from xenon.engine.plan_execute_engine import PlanExecuteEngine
from xenon.engine.react_engine import ReActEngine
from xenon.engine.reflection_engine import ReflectionEngine
from xenon.repl.repl import REPL


class TestBaseEngineInheritance:
    def test_all_base_engines_inherit_base(self):
        for cls in (ReActEngine, PlanExecuteEngine, ReflectionEngine):
            assert issubclass(cls, BaseEngine), f"{cls.__name__} 未继承 BaseEngine"

    def test_call_llm_is_shared_single_source(self):
        """基础引擎不再各自携带 _call_llm 副本，统一继承自 BaseEngine。"""
        base_method = BaseEngine._call_llm
        for cls in (ReActEngine, PlanExecuteEngine, ReflectionEngine):
            assert "_call_llm" not in cls.__dict__, (
                f"{cls.__name__} 仍自带 _call_llm 副本"
            )
            assert cls._call_llm is base_method


class TestTemperatureDriftEliminated:
    def test_react_plan_reflection_use_0_3(self):
        for cls in (ReActEngine, PlanExecuteEngine, ReflectionEngine):
            assert cls(["openai/gpt-4o"]).temperature == 0.3


class TestObservationTruncateConfigurable:
    def test_default_is_2000(self):
        assert ReActEngine(["openai/gpt-4o"]).observation_truncate == 2000

    def test_subclass_can_override(self):
        class ShortEngine(ReActEngine):
            observation_truncate = 500

        assert ShortEngine(["openai/gpt-4o"]).observation_truncate == 500


class TestBaseEngineIsAbstract:
    def test_cannot_instantiate_base_directly(self):
        import pytest

        with pytest.raises(TypeError):
            BaseEngine(["openai/gpt-4o"])  # run 是 abstractmethod


class TestActualModelReporting:
    def test_repl_prefers_engine_actual_model(self):
        engine = SimpleNamespace(last_model_used="deepseek/deepseek-v4-flash")
        assert (
            REPL._engine_model_used(
                engine,
                ["deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-flash"],
            )
            == "deepseek/deepseek-v4-flash"
        )

    def test_repl_falls_back_when_engine_has_not_completed_a_call(self):
        engine = SimpleNamespace(last_model_used=None)
        assert REPL._engine_model_used(engine, ["openai/gpt-4o"]) == "openai/gpt-4o"
