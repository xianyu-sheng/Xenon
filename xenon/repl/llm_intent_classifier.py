"""
LLM Intent Classifier — 基于 LLM 的意图分类器。

当正则分类器无法识别意图时，回退到 LLM 分类器进行二次判断。
使用轻量快速的模型（如 GPT-4o-mini / Claude Haiku）进行分类，
避免每次都调用大模型造成延迟和成本问题。

设计原则：
1. 快速：使用小模型，控制在 200ms 内
2. 准确：提供清晰的分类标准和示例
3. 可回退：LLM 不可用时优雅降级到正则结果
4. 可配置：用户可选择启用/禁用 LLM 分类器
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from xenon.utils.llm_client import chat_completion
from xenon.repl.system_config import get_config

logger = logging.getLogger(__name__)

# 意图类别及其描述（与 prompt_optimizer.py 的 TEMPLATES 保持一致）
INTENT_CATEGORIES = {
    "write_code": "编写、实现、创建新的代码、函数、类、模块、脚本、算法",
    "debug": "调试、修复 bug、解决报错、异常、崩溃问题",
    "explain": "解释、说明、讲解代码或技术概念的含义和工作原理",
    "refactor": "重构、优化、改进现有代码的质量、性能、可读性",
    "write_test": "编写、生成测试用例、单元测试",
    "design": "设计、规划系统架构、模块结构、接口、数据库",
    "convert": "转换、迁移代码或数据格式（从 A 到 B）",
    "novel": "小说创作、故事编写、续写、润色、角色设定、世界观构建",
    "query": "查询实时信息（天气、时间、票务、价格等需要工具的查询）",
    "research": "调研、研究、对比技术方案、平台、项目、社区维护状态",
    "write_doc": "编写、整理技术文档、README、API 文档、说明书",
    "chat": "闲聊、问候、致谢等日常对话",
}


@dataclass
class ClassificationResult:
    """LLM 分类结果。"""
    intent: str | None
    confidence: float  # 0-1 之间
    reasoning: str = ""  # 分类理由（调试用）
    latency_ms: float = 0.0  # 分类耗时
    fallback: bool = False  # 是否是降级结果


class LLMIntentClassifier:
    """基于 LLM 的意图分类器。"""

    def __init__(
        self,
        *,
        model: str | None = None,
        enabled: bool = True,
        confidence_threshold: float = 0.7,
        timeout: float = 5.0,
    ):
        """
        Args:
            model: 使用的模型 ID，None 时从配置读取
            enabled: 是否启用 LLM 分类器
            confidence_threshold: 置信度阈值，低于此值返回 None
            timeout: 单次调用超时时间（秒）
        """
        self.enabled = enabled
        self.confidence_threshold = confidence_threshold
        self.timeout = timeout

        # 从配置读取默认模型（用于分类的快速小模型）
        if model is None:
            config = get_config()
            # 优先使用配置的分类器模型，否则使用默认的快速模型
            self.model = getattr(config.intent_classifier, 'model', None)
            if not self.model:
                # 默认使用快速小模型
                self.model = self._get_default_classifier_model()
        else:
            self.model = model

        logger.info(f"LLM 意图分类器初始化: model={self.model}, enabled={self.enabled}")

    @staticmethod
    def _get_default_classifier_model() -> str:
        """获取默认的分类器模型（优先选择快速、便宜的小模型）。"""
        # 按优先级尝试：Claude Haiku > GPT-4o-mini > DeepSeek Flash
        # 这些都是快速且成本低的模型
        candidates = [
            "anthropic/claude-3-5-haiku-20241022",
            "openai/gpt-4o-mini",
            "deepseek/deepseek-v4-flash",
        ]

        # TODO: 可以检查哪个 API Key 可用，选择对应的模型
        # 目前简单返回第一个
        return candidates[0]

    def classify(
        self,
        user_input: str,
        *,
        context_messages: list[dict] | None = None,
    ) -> ClassificationResult:
        """
        使用 LLM 对用户输入进行意图分类。

        Args:
            user_input: 用户输入文本
            context_messages: 可选的上下文消息（用于理解多轮对话）

        Returns:
            ClassificationResult 包含意图、置信度和推理过程
        """
        if not self.enabled:
            return ClassificationResult(
                intent=None,
                confidence=0.0,
                reasoning="LLM 分类器未启用",
                fallback=True,
            )

        if not user_input or not user_input.strip():
            return ClassificationResult(
                intent=None,
                confidence=0.0,
                reasoning="输入为空",
            )

        start_time = time.time()

        try:
            result = self._call_llm_classifier(user_input, context_messages)
            result.latency_ms = (time.time() - start_time) * 1000

            # 置信度过低时返回 None
            if result.confidence < self.confidence_threshold:
                logger.debug(
                    f"LLM 分类置信度过低: {result.confidence:.2f} < {self.confidence_threshold}, "
                    f"intent={result.intent}"
                )
                return ClassificationResult(
                    intent=None,
                    confidence=result.confidence,
                    reasoning=f"置信度过低: {result.reasoning}",
                    latency_ms=result.latency_ms,
                )

            logger.debug(
                f"LLM 分类成功: intent={result.intent}, "
                f"confidence={result.confidence:.2f}, "
                f"latency={result.latency_ms:.0f}ms"
            )
            return result

        except Exception as e:
            logger.warning(f"LLM 意图分类失败: {e}", exc_info=True)
            return ClassificationResult(
                intent=None,
                confidence=0.0,
                reasoning=f"分类失败: {str(e)}",
                latency_ms=(time.time() - start_time) * 1000,
                fallback=True,
            )

    def _call_llm_classifier(
        self,
        user_input: str,
        context_messages: list[dict] | None,
    ) -> ClassificationResult:
        """调用 LLM 进行分类（内部方法）。"""

        # 构建分类 prompt
        system_prompt = self._build_system_prompt()
        user_prompt = self._build_user_prompt(user_input, context_messages)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # 调用 LLM（使用低 temperature 确保稳定输出）
        try:
            response_text = chat_completion(
                model_id=self.model,  # 第一个参数是 model_id
                messages=messages,
                temperature=0.1,
                max_tokens=1000,  # 推理模型需要更多 tokens（隐藏推理阶段消耗大量 tokens）
            )
        except Exception as e:
            raise RuntimeError(f"LLM 调用失败: {e}") from e

        # 解析响应（期望 JSON 格式）
        return self._parse_response(response_text)

    @staticmethod
    def _build_system_prompt() -> str:
        """构建系统提示词（定义分类任务和标准）。"""

        # 构建意图类别列表
        categories_desc = "\n".join(
            f"- {key}: {desc}"
            for key, desc in INTENT_CATEGORIES.items()
        )

        return f"""你是一个意图分类专家。你的任务是分析用户输入，判断用户的真实意图。

## 意图类别

{categories_desc}

## 分类规则

1. **优先级顺序**：
   - debug（调试修复）> write_test（测试）> write_doc（文档）> write_code（写代码）
   - convert（转换）> refactor（重构）> write_code
   - query（实时查询）> research（资料调研）
   - 特定意图优先于通用意图

2. **关键判据**：
   - 看**动词**：写/实现/创建 vs 修复/调试 vs 解释/说明
   - 看**对象**：代码/函数/测试/文档/架构
   - 看**目标**：新建 vs 修改 vs 理解 vs 查询
   - 看**上下文**：是否有报错信息、是否需要外部数据

3. **边界情况**：
   - "写一个函数" → write_code（新建代码）
   - "修复这个函数" → debug（修复问题）
   - "优化这段代码" → refactor（改进现有代码）
   - "解释这段代码" → explain（理解代码）
   - "今天天气" → query（实时查询）
   - "调研最好的库" → research（资料调研）

4. **无法判断时**：
   - 返回 null 而不是猜测
   - 置信度诚实反映不确定性

## 输出格式

必须输出 JSON，包含三个字段：
```json
{{
  "intent": "意图类别key或null",
  "confidence": 0.95,
  "reasoning": "简短的分类理由（一句话）"
}}
```

例如：
- 输入："帮我写一个排序函数" → {{"intent": "write_code", "confidence": 0.95, "reasoning": "明确要求编写新函数"}}
- 输入："这段代码报错了" → {{"intent": "debug", "confidence": 0.9, "reasoning": "存在报错需要修复"}}
- 输入："今天北京天气" → {{"intent": "query", "confidence": 0.95, "reasoning": "查询实时天气信息"}}
- 输入："嗯" → {{"intent": null, "confidence": 0.0, "reasoning": "输入过于简短无法判断"}}

只输出 JSON，不要额外解释。"""

    @staticmethod
    def _build_user_prompt(
        user_input: str,
        context_messages: list[dict] | None,
    ) -> str:
        """构建用户提示词。"""

        prompt_parts = []

        # 添加上下文（如果有）
        if context_messages and len(context_messages) > 0:
            # 只取最近 2 轮对话作为上下文
            recent_context = context_messages[-4:] if len(context_messages) > 4 else context_messages
            context_str = "\n".join(
                f"{msg.get('role', 'user')}: {msg.get('content', '')[:100]}"
                for msg in recent_context
            )
            prompt_parts.append(f"## 对话上下文\n\n{context_str}\n")

        # 添加待分类的用户输入
        prompt_parts.append(f"## 待分类的用户输入\n\n{user_input}\n")
        prompt_parts.append("请输出 JSON 格式的分类结果：")

        return "\n".join(prompt_parts)

    @staticmethod
    def _parse_response(response_text: str) -> ClassificationResult:
        """解析 LLM 响应，提取分类结果。"""

        # 清理响应文本（移除可能的 markdown 代码块标记）
        cleaned = response_text.strip()
        if cleaned.startswith("```"):
            # 移除开头的 ```json 或 ```
            lines = cleaned.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()

        # 解析 JSON
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.warning(f"LLM 响应不是有效的 JSON: {cleaned[:200]}")
            raise ValueError(f"无法解析 LLM 响应为 JSON: {e}") from e

        # 提取字段
        intent = data.get("intent")
        confidence = float(data.get("confidence", 0.0))
        reasoning = data.get("reasoning", "")

        # 验证 intent 是否在有效类别中
        if intent is not None and intent not in INTENT_CATEGORIES:
            logger.warning(f"LLM 返回了无效的意图类别: {intent}")
            intent = None
            confidence = 0.0
            reasoning = f"无效类别: {intent}"

        return ClassificationResult(
            intent=intent,
            confidence=confidence,
            reasoning=reasoning,
        )


# ── 全局分类器实例（延迟初始化）──────────────────────────────

_classifier_instance: LLMIntentClassifier | None = None
_classifier_lock = __import__("threading").Lock()


def get_llm_classifier() -> LLMIntentClassifier:
    """获取全局 LLM 分类器实例（单例模式）。"""
    global _classifier_instance

    if _classifier_instance is None:
        with _classifier_lock:
            if _classifier_instance is None:
                # 从配置读取是否启用
                config = get_config()
                enabled = getattr(config.intent_classifier, 'enabled', False)

                _classifier_instance = LLMIntentClassifier(enabled=enabled)

    return _classifier_instance


def classify_intent_with_llm(
    user_input: str,
    *,
    context_messages: list[dict] | None = None,
) -> str | None:
    """
    使用 LLM 对用户输入进行意图分类（便捷函数）。

    Args:
        user_input: 用户输入文本
        context_messages: 可选的上下文消息

    Returns:
        意图类别字符串，或 None（无法识别）
    """
    classifier = get_llm_classifier()
    result = classifier.classify(user_input, context_messages=context_messages)
    return result.intent
