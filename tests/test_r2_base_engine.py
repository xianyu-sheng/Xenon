"""R2 验收：BaseEngine 抽象基类。

- 四引擎（ReAct/PlanExecute/Reflection/Novel）继承 BaseEngine；
- _call_llm 单一来源（子类 __dict__ 不再各自携带副本）；
- 参数漂移消除：temperature 集中（react/plan/reflection=0.3，novel=0.8）；
- novel 的 B7 漂移修复：现读 ModelConfig（api_key/base_url/max_tokens）；
- observation 截断阈值统一为可配属性。
"""
from types import SimpleNamespace

import xenon.engine.base as base_mod
from xenon.engine.base import BaseEngine
from xenon.engine.novel_engine import NovelEngine
from xenon.engine.plan_execute_engine import PlanExecuteEngine
from xenon.engine.react_engine import ReActEngine
from xenon.engine.reflection_engine import ReflectionEngine


class TestBaseEngineInheritance:
    def test_all_four_engines_inherit_base(self):
        for cls in (ReActEngine, PlanExecuteEngine, ReflectionEngine, NovelEngine):
            assert issubclass(cls, BaseEngine), f"{cls.__name__} 未继承 BaseEngine"

    def test_call_llm_is_shared_single_source(self):
        """四引擎不再各自携带 _call_llm 副本，统一继承自 BaseEngine。"""
        base_method = BaseEngine._call_llm
        for cls in (ReActEngine, PlanExecuteEngine, ReflectionEngine, NovelEngine):
            assert "_call_llm" not in cls.__dict__, f"{cls.__name__} 仍自带 _call_llm 副本"
            assert cls._call_llm is base_method


class TestTemperatureDriftEliminated:
    def test_react_plan_reflection_use_0_3(self):
        for cls in (ReActEngine, PlanExecuteEngine, ReflectionEngine):
            assert cls(["openai/gpt-4o"]).temperature == 0.3

    def test_novel_uses_0_8(self):
        assert NovelEngine(["openai/gpt-4o"]).temperature == 0.8


class TestNovelB7WiringFixed:
    """R2 漂移修复：novel 此前 _call_llm 未读 ModelConfig，现统一由 BaseEngine 接入。"""

    def test_novel_reads_model_config(self, monkeypatch):
        mc = SimpleNamespace(max_tokens=2048, api_key="sk-novel",
                             base_url="https://novel.example.com/v1")
        eng = NovelEngine(["openai/gpt-4o"], model_configs={"openai/gpt-4o": mc})
        captured = {}

        def fake_chat(model_id, messages, **kw):
            captured.update(kw)
            return '{"final_answer":"ok"}'

        monkeypatch.setattr(base_mod, "chat_completion", fake_chat)
        eng._call_llm([{"role": "user", "content": "hi"}])
        assert captured["max_tokens"] == 2048
        assert captured["credentials"] == {"openai": "sk-novel"}
        assert captured["base_url"] == "https://novel.example.com/v1"
        assert captured["temperature"] == 0.8

    def test_novel_falls_back_to_8192_without_config(self, monkeypatch):
        eng = NovelEngine(["openai/gpt-4o"])  # 无 model_configs
        captured = {}

        def fake_chat(model_id, messages, **kw):
            captured["mt"] = kw.get("max_tokens")
            return '{"final_answer":"ok"}'

        monkeypatch.setattr(base_mod, "chat_completion", fake_chat)
        eng._call_llm([{"role": "user", "content": "hi"}])
        assert captured["mt"] == 8192


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
