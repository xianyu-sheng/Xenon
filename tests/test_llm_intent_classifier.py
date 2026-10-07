"""
LLM Intent Classifier 测试。

测试分类器的核心功能：
1. 正则无法识别时的 LLM 回退
2. 各种意图类型的准确分类
3. 配置启用/禁用
4. 置信度阈值过滤
5. 错误处理和降级
"""

from __future__ import annotations

import pytest

from xenon.repl.llm_intent_classifier import (
    LLMIntentClassifier,
    ClassificationResult,
    classify_intent_with_llm,
    INTENT_CATEGORIES,
)


class TestLLMIntentClassifier:
    """LLM 意图分类器基础测试。"""

    def test_classifier_disabled_returns_fallback(self):
        """分类器禁用时应返回 fallback 结果。"""
        classifier = LLMIntentClassifier(enabled=False)
        result = classifier.classify("写一个排序函数")

        assert result.intent is None
        assert result.fallback is True
        assert "未启用" in result.reasoning

    def test_classifier_empty_input_returns_none(self):
        """空输入应返回 None（不依赖环境里是否有可用 provider）。"""
        classifier = LLMIntentClassifier(enabled=True, model="test/model")
        result = classifier.classify("")

        assert result.intent is None
        assert "输入为空" in result.reasoning

    def test_parse_response_valid_json(self):
        """测试解析有效的 JSON 响应。"""
        response = '{"intent": "write_code", "confidence": 0.95, "reasoning": "test"}'
        result = LLMIntentClassifier._parse_response(response)

        assert result.intent == "write_code"
        assert result.confidence == 0.95
        assert result.reasoning == "test"

    def test_parse_response_with_markdown_blocks(self):
        """测试解析带 markdown 代码块的响应。"""
        response = (
            '```json\n{"intent": "debug", "confidence": 0.9, "reasoning": "test"}\n```'
        )
        result = LLMIntentClassifier._parse_response(response)

        assert result.intent == "debug"
        assert result.confidence == 0.9

    def test_parse_response_invalid_intent(self):
        """测试解析无效意图类别的响应。"""
        response = (
            '{"intent": "invalid_intent", "confidence": 0.9, "reasoning": "test"}'
        )
        result = LLMIntentClassifier._parse_response(response)

        assert result.intent is None
        assert result.confidence == 0.0

    def test_parse_response_null_intent(self):
        """测试解析 null 意图的响应。"""
        response = '{"intent": null, "confidence": 0.0, "reasoning": "无法判断"}'
        result = LLMIntentClassifier._parse_response(response)

        assert result.intent is None
        assert result.confidence == 0.0

    def test_parse_response_invalid_json(self):
        """测试解析无效 JSON 时抛出异常。"""
        response = "not a json"
        with pytest.raises(ValueError, match="无法解析.*JSON"):
            LLMIntentClassifier._parse_response(response)

    def test_confidence_threshold_filtering(self):
        """测试置信度阈值过滤。

        显式指定 model：否则在无凭据的 CI 环境下，构造器会因为找不到可用
        provider 而硬禁用分类器，mock 永远不会被执行。
        """
        classifier = LLMIntentClassifier(
            enabled=True,
            model="test/model",
            confidence_threshold=0.8,
        )

        # Mock _call_llm_classifier to return low confidence
        def mock_call(self, text, ctx):
            return ClassificationResult(
                intent="write_code",
                confidence=0.6,  # 低于阈值 0.8
                reasoning="低置信度",
            )

        original_call = classifier._call_llm_classifier
        classifier._call_llm_classifier = lambda t, c, **kw: mock_call(
            classifier, t, c
        )

        result = classifier.classify("some text")

        # 低于阈值应返回 None
        assert result.intent is None
        assert result.confidence == 0.6

        classifier._call_llm_classifier = original_call

    def test_system_prompt_contains_all_intents(self):
        """测试系统提示词包含所有意图类别。"""
        system_prompt = LLMIntentClassifier._build_system_prompt()

        for intent_key in INTENT_CATEGORIES.keys():
            assert intent_key in system_prompt, f"意图 {intent_key} 未在系统提示词中"

    def test_user_prompt_with_context(self):
        """测试用户提示词包含上下文。"""
        context = [
            {"role": "user", "content": "之前的问题"},
            {"role": "assistant", "content": "之前的回答"},
        ]
        prompt = LLMIntentClassifier._build_user_prompt("新问题", context)

        assert "新问题" in prompt
        assert "对话上下文" in prompt
        assert "之前的问题" in prompt

    def test_user_prompt_without_context(self):
        """测试用户提示词不包含上下文。"""
        prompt = LLMIntentClassifier._build_user_prompt("问题", None)

        assert "问题" in prompt
        assert "对话上下文" not in prompt

    def test_default_model_selection_uses_configured_provider(self, monkeypatch):
        """默认分类模型必须来自已配置 provider（不再盲选 Claude）。"""
        from xenon.repl import provider_registry

        class _Provider:
            def __init__(self, key, models):
                self.key = key
                self.models = models

        monkeypatch.setattr(
            provider_registry,
            "get_configured_providers",
            lambda **kwargs: [
                _Provider("deepseek", ["deepseek-v4-pro", "deepseek-v4-flash"])
            ],
        )

        assert (
            LLMIntentClassifier._select_configured_model()
            == "deepseek/deepseek-v4-flash"
        )

    def test_default_model_selection_without_credentials(self, monkeypatch):
        """没有已配置 provider 时返回空串（分类器禁用，回退正则）。"""
        from xenon.repl import provider_registry

        monkeypatch.setattr(
            provider_registry,
            "get_configured_providers",
            lambda **kwargs: [],
        )

        assert LLMIntentClassifier._select_configured_model() == ""


class TestIntentClassifierConfigDefaults:
    """LLM 意图分类器默认开启（用户要求）；测试进程由根 conftest 关闭。"""

    def test_dataclass_default_is_enabled(self):
        from xenon.repl.system_config import IntentClassifierConfig

        assert IntentClassifierConfig().enabled is True

    def test_config_default_is_enabled_without_env_or_file(
        self, monkeypatch, tmp_path
    ):
        from xenon.repl import system_config

        monkeypatch.delenv("XENON_INTENT_CLASSIFIER_ENABLED", raising=False)
        monkeypatch.setattr(
            system_config, "CONFIG_PATH", tmp_path / "missing.yaml"
        )
        monkeypatch.setattr(system_config, "_file_cache", None)
        monkeypatch.setattr(system_config, "_file_cache_key", None)

        assert system_config.get_config().intent_classifier.enabled is True

    def test_env_can_disable_the_classifier(self, monkeypatch, tmp_path):
        from xenon.repl import system_config

        monkeypatch.setenv("XENON_INTENT_CLASSIFIER_ENABLED", "0")
        monkeypatch.setattr(
            system_config, "CONFIG_PATH", tmp_path / "missing.yaml"
        )
        monkeypatch.setattr(system_config, "_file_cache", None)
        monkeypatch.setattr(system_config, "_file_cache_key", None)

        assert system_config.get_config().intent_classifier.enabled is False


class TestIntentClassifierIntegration:
    """意图分类器集成测试（需要真实 API 调用）。"""

    @pytest.mark.skip(reason="需要真实 LLM API，成本较高")
    def test_classify_write_code_intent(self):
        """测试识别写代码意图（集成测试）。"""
        result = classify_intent_with_llm("帮我实现一个快速排序算法")
        assert result == "write_code"

    @pytest.mark.skip(reason="需要真实 LLM API，成本较高")
    def test_classify_debug_intent(self):
        """测试识别调试意图（集成测试）。"""
        result = classify_intent_with_llm("这段代码报错了，帮我修复")
        assert result == "debug"

    @pytest.mark.skip(reason="需要真实 LLM API，成本较高")
    def test_classify_query_intent(self):
        """测试识别查询意图（集成测试）。"""
        result = classify_intent_with_llm("今天北京的天气怎么样")
        assert result == "query"

    @pytest.mark.skip(reason="需要真实 LLM API，成本较高")
    def test_classify_ambiguous_input(self):
        """测试模糊输入（集成测试）。"""
        result = classify_intent_with_llm("嗯")
        assert result is None  # 模糊输入应返回 None


class TestDifficultyEstimatorWithLLM:
    """测试 DifficultyEstimator 与 LLM 分类器的集成。"""

    def test_detect_intent_fallback_to_llm(self, monkeypatch):
        """单一语义通道后：路由启发不再回退调 LLM（分类器是唯一语义源）。"""
        from xenon.repl.difficulty_estimator import DifficultyEstimator

        calls: list[str] = []

        monkeypatch.setattr(
            "xenon.repl.prompt_optimizer.detect_intent", lambda text: None
        )
        monkeypatch.setattr(
            "xenon.repl.llm_intent_classifier.classify_intent_with_llm",
            lambda text: calls.append(text) or "write_code",
        )

        intent = DifficultyEstimator._detect_intent("某个输入")

        assert intent is None
        assert calls == []

        estimator = DifficultyEstimator()
        intent = estimator._detect_intent("一些正则无法识别的文本")

        assert intent is None
        assert calls == []

    def test_detect_intent_llm_failure_returns_none(self, monkeypatch):
        """测试 LLM 分类器失败时返回 None。"""
        from xenon.repl.difficulty_estimator import DifficultyEstimator

        # Mock 正则分类器返回 None
        def mock_regex_detect(text):
            return None

        # Mock LLM 分类器抛出异常
        def mock_llm_classify(text, context_messages=None):
            raise RuntimeError("LLM API 调用失败")

        monkeypatch.setattr(
            "xenon.repl.prompt_optimizer.detect_intent",
            mock_regex_detect,
        )
        monkeypatch.setattr(
            "xenon.repl.llm_intent_classifier.classify_intent_with_llm",
            mock_llm_classify,
        )

        estimator = DifficultyEstimator()
        intent = estimator._detect_intent("一些文本")

        # LLM 失败时应返回 None 而不是抛出异常
        assert intent is None


class TestClassificationResult:
    """测试 ClassificationResult 数据类。"""

    def test_classification_result_defaults(self):
        """测试 ClassificationResult 默认值。"""
        result = ClassificationResult(intent="write_code", confidence=0.9)

        assert result.intent == "write_code"
        assert result.confidence == 0.9
        assert result.reasoning == ""
        assert result.latency_ms == 0.0
        assert result.fallback is False

    def test_classification_result_with_all_fields(self):
        """测试 ClassificationResult 所有字段。"""
        result = ClassificationResult(
            intent="debug",
            confidence=0.85,
            reasoning="存在报错信息",
            latency_ms=150.5,
            fallback=False,
        )

        assert result.intent == "debug"
        assert result.confidence == 0.85
        assert result.reasoning == "存在报错信息"
        assert result.latency_ms == 150.5
        assert result.fallback is False
