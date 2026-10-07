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
from typing import Any

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

# 操作原语：与“意图”分离的封闭词表，描述完成请求真正需要的副作用。
# 两轮审计的核心结论：intent 描述“想要什么”，operations 才决定“授权与能力”。
OPERATION_VOCABULARY = (
    "read",  # 读取/查看/搜索/分析已有内容
    "write",  # 修改/更新已有文件
    "create",  # 新建文件/目录/保存新产物
    "delete",  # 删除/移除
    "move",  # 移动/复制/重命名
    "execute",  # 运行命令/脚本/测试
    "network",  # 联网抓取/下载/查询
)


@dataclass
class ClassificationResult:
    """LLM 分类结果。"""

    intent: str | None
    confidence: float  # 0-1 之间
    reasoning: str = ""  # 分类理由（调试用）
    latency_ms: float = 0.0  # 分类耗时
    fallback: bool = False  # 是否是降级结果
    operations: tuple[str, ...] = ()  # 完成请求需要的操作原语
    chat_only: bool = False  # 用户明确要求只在对话中回答


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
        # 同一轮内 REPL路由/难度估计/契约构建会对同一输入多次要分类结果，
        # 小缓存直接消除重复的小模型调用（64 条上限，超出即清空）。
        self._cache: dict[tuple, ClassificationResult] = {}

        # 从配置读取默认模型（用于分类的快速小模型）
        if model is None:
            config = get_config()
            self.model = getattr(config.intent_classifier, "model", "") or ""
            if not self.model:
                # 从已配置 API Key 的 provider 中挑选，而不是盲选 Claude。
                self.model = self._select_configured_model()
        else:
            self.model = model

        if not self.model:
            # 没有可用模型时硬禁用，调用方自动回退正则层。
            self.enabled = False
            logger.info(
                "LLM 意图分类器无可用模型（已配置 provider 为空），"
                "已禁用并回退正则"
            )
        else:
            logger.info(
                f"LLM 意图分类器初始化: model={self.model}, enabled={self.enabled}"
            )

    # 便宜/低延迟模型的名称特征，按 provider 已配置的模型列表匹配。
    _FAST_MODEL_PATTERNS = (
        "flash",
        "mini",
        "haiku",
        "nano",
        "lite",
        "small",
        "turbo",
        "instant",
    )
    _PROVIDER_PRIORITY = (
        "deepseek",
        "openai",
        "anthropic",
        "ark",
        "moonshot",
        "zhipu",
        "gemini",
        "ollama",
    )

    @classmethod
    def _select_configured_model(cls) -> str:
        """从已配置 API Key 的 provider 中挑选最快的分类器模型。

        旧实现盲选 ``anthropic/claude-3-5-haiku``，未配置 Anthropic key 时每轮
        调用都失败再静默降级。现在只读凭据层（refresh_models=False，不触发
        网络探测）：优先匹配便宜模型的名称特征，否则退回优先级最高的
        provider 的第一个模型；没有任何已配置 provider 时返回空串（禁用）。
        """

        try:
            from xenon.repl.provider_registry import get_configured_providers

            providers = get_configured_providers(
                refresh_models=False, use_cache=True
            )
        except Exception as exc:  # noqa: BLE001 — 选型失败不能让流程崩溃
            logger.debug("分类器模型选择失败: %s", exc)
            return ""

        def rank(provider: Any) -> int:
            try:
                return cls._PROVIDER_PRIORITY.index(provider.key)
            except (ValueError, AttributeError):
                return len(cls._PROVIDER_PRIORITY)

        ordered = sorted(providers, key=rank)
        for provider in ordered:
            for name in provider.models or []:
                lowered = str(name).lower()
                if any(pat in lowered for pat in cls._FAST_MODEL_PATTERNS):
                    return f"{provider.key}/{name}"
        for provider in ordered:
            if provider.models:
                return f"{provider.key}/{provider.models[0]}"
        return ""

    def classify(
        self,
        user_input: str,
        *,
        context_messages: list[dict] | None = None,
        hints: dict[str, Any] | None = None,
    ) -> ClassificationResult:
        """
        使用 LLM 对用户输入进行意图分类。

        Args:
            user_input: 用户输入文本
            context_messages: 可选的上下文消息（用于理解多轮对话）
            hints: 正则层提取的确定性信号（写入/执行结构、禁令、路径），
                作为提示喂给分类器，让它有据可依且两边冲突显式化。

        Returns:
            ClassificationResult 包含意图、操作原语、置信度和推理过程
        """
        if not user_input or not user_input.strip():
            return ClassificationResult(
                intent=None,
                confidence=0.0,
                reasoning="输入为空",
            )

        if not self.enabled:
            return ClassificationResult(
                intent=None,
                confidence=0.0,
                reasoning="LLM 分类器未启用",
                fallback=True,
            )

        cache_key = (
            user_input,
            tuple(
                (str(m.get("role", "")), str(m.get("content", ""))[:80])
                for m in (context_messages or [])[-2:]
            ),
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            logger.debug("LLM 分类命中缓存: intent=%s", cached.intent)
            return cached

        def _remember(classified: ClassificationResult) -> ClassificationResult:
            if len(self._cache) >= 64:
                self._cache.clear()
            self._cache[cache_key] = classified
            return classified

        start_time = time.time()

        try:
            result = self._call_llm_classifier(
                user_input,
                context_messages,
                hints=hints,
            )
            result.latency_ms = (time.time() - start_time) * 1000

            # 置信度过低时返回 None
            if result.confidence < self.confidence_threshold:
                logger.debug(
                    f"LLM 分类置信度过低: {result.confidence:.2f} < {self.confidence_threshold}, "
                    f"intent={result.intent}"
                )
                return _remember(
                    ClassificationResult(
                        intent=None,
                        confidence=result.confidence,
                        reasoning=f"置信度过低: {result.reasoning}",
                        latency_ms=result.latency_ms,
                    )
                )

            logger.debug(
                f"LLM 分类成功: intent={result.intent}, "
                f"confidence={result.confidence:.2f}, "
                f"latency={result.latency_ms:.0f}ms"
            )
            return _remember(result)

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
        *,
        hints: dict[str, Any] | None = None,
    ) -> ClassificationResult:
        """调用 LLM 进行分类（内部方法）。"""

        # 构建分类 prompt
        system_prompt = self._build_system_prompt()
        user_prompt = self._build_user_prompt(user_input, context_messages, hints)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # 调用 LLM（使用低 temperature 确保稳定输出）
        # 推理模型需要更多 tokens（隐藏推理阶段会消耗预算）；1000 会导致
        # JSON 被截断后在客户端反复修复，2048 让完整分类结果的占比更高。
        try:
            response_text = chat_completion(
                model_id=self.model,  # 第一个参数是 model_id
                messages=messages,
                temperature=0.1,
                max_tokens=2048,
            )
        except Exception as e:
            raise RuntimeError(f"LLM 调用失败: {e}") from e

        # 解析（期望 JSON 格式）
        try:
            return self._parse_response(response_text)
        except ValueError:
            # 一次修复重试：把非法输出回传给模型，要求只输出 JSON。
            logger.warning("意图分类响应非合法 JSON，发起一次修复重试")
            retry_messages = [
                *messages,
                {"role": "assistant", "content": response_text[:600]},
                {
                    "role": "user",
                    "content": (
                        "上面的输出不是合法 JSON。请只输出一个 JSON 对象，字段为 "
                        "intent / operations / chat_only / confidence / reasoning，"
                        "不要任何解释、前后缀或多余字符。"
                    ),
                },
            ]
            try:
                repaired_text = chat_completion(
                    model_id=self.model,
                    messages=retry_messages,
                    temperature=0.0,
                    max_tokens=512,
                )
            except Exception as e:
                raise RuntimeError(f"LLM 修复重试失败: {e}") from e
            return self._parse_response(repaired_text)

    @staticmethod
    def _build_system_prompt() -> str:
        """构建系统提示词（定义分类任务和标准）。"""

        # 构建意图类别列表
        categories_desc = "\n".join(
            f"- {key}: {desc}" for key, desc in INTENT_CATEGORIES.items()
        )

        ops_desc = ", ".join(OPERATION_VOCABULARY)
        return f"""你是意图分类专家。分析用户输入，只输出一个 JSON 对象。

## 意图类别
{categories_desc}

## 规则
1. operations 与 intent 分离，取值只能是：{ops_desc}。
   - 聊天/解释/设计/把代码或文本贴在对话里 → operations 为空。
   - 「写一个函数给我看看」「给我一段代码」不是文件操作；只有用户指定文件/路径/保存目标（写到 X、输出到 X、在 X 里加）才给 write/create。
   - 删除已有内容 → delete；移动/复制/重命名 → move；修改已有文件 → write。
   - 问原因/思路/建议（告诉我原因、思路是什么、有什么建议、怎么解决）→ operations 为空，即使句中出现“处理/修复/优化”。
   - 用户禁止某项操作（不要写文件/不要运行）→ 对应 operation 不出现，chat_only=true。
2. query/research 通常用 read 或 network；debug/refactor 默认 read；用户要求修复/重构/改进某个具体对象（模块、函数、文件、脚本）时加 write。
3. 无法判断时 intent=null，confidence 如实。

## 输出（只输出 JSON，无解释）
{{"intent":"类别或null","operations":[],"chat_only":false,"confidence":0.0,"reasoning":"一句话"}}

示例：
"帮我写一个排序函数" → {{"intent":"write_code","operations":[],"chat_only":false,"confidence":0.95,"reasoning":"代码贴对话"}}
"把结果写到 output.txt" → {{"intent":"write_code","operations":["create"],"chat_only":false,"confidence":0.95,"reasoning":"写入文件"}}
"帮我重构这个模块" → {{"intent":"refactor","operations":["read","write"],"chat_only":false,"confidence":0.9,"reasoning":"重构模块需读写"}}
"改完代码后运行测试" → {{"intent":"debug","operations":["execute"],"chat_only":false,"confidence":0.9,"reasoning":"要求运行测试"}}
"你觉得这两个方案哪个好" → {{"intent":"chat","operations":[],"chat_only":false,"confidence":0.9,"reasoning":"征询观点"}}
"删除 /tmp/foo.txt" → {{"intent":"refactor","operations":["delete"],"chat_only":false,"confidence":0.9,"reasoning":"删除文件"}}
"修复这个 bug 的思路是什么" → {{"intent":"debug","operations":[],"chat_only":false,"confidence":0.9,"reasoning":"只要思路"}}
"不要修改任何文件，只解释" → {{"intent":"explain","operations":[],"chat_only":true,"confidence":0.95,"reasoning":"禁止文件操作"}}"""

    @staticmethod
    def _build_user_prompt(
        user_input: str,
        context_messages: list[dict] | None,
        hints: dict[str, Any] | None = None,
    ) -> str:
        """构建用户提示词。"""

        prompt_parts = []

        # 添加上下文（如果有）
        if context_messages and len(context_messages) > 0:
            # 只取最近 2 轮对话作为上下文
            recent_context = (
                context_messages[-4:] if len(context_messages) > 4 else context_messages
            )
            context_str = "\n".join(
                f"{msg.get('role', 'user')}: {msg.get('content', '')[:100]}"
                for msg in recent_context
            )
            prompt_parts.append(f"## 对话上下文\n\n{context_str}\n")

        # 正则层的确定性信号：让 LLM 有据可依，冲突在合并层显式裁决。
        if hints:
            lines: list[str] = []
            if hints.get("write_snippets"):
                lines.append(
                    "- 检测到显式写入结构: " + "；".join(hints["write_snippets"])
                )
            if hints.get("execute_snippets"):
                lines.append(
                    "- 检测到显式执行结构: " + "；".join(hints["execute_snippets"])
                )
            if hints.get("read_snippets"):
                lines.append(
                    "- 检测到读取/路径信号: " + "；".join(hints["read_snippets"])
                )
            if hints.get("negations"):
                lines.append("\u2022 检测到显式禁令: " + "；".join(hints["negations"]))
            if hints.get("chat_only"):
                lines.append("- 检测到 chat_only 约束")
            if hints.get("advisory"):
                lines.append(
                    "- 检测到征询解释语义（问原因/思路/建议）：operations 应为空，"
                    "不要因为句中出现'处理/修复/优化'就授权写入"
                )
            if hints.get("no_tools"):
                lines.append("- 检测到 no_tools 约束")
            if lines:
                prompt_parts.append(
                    "## 确定性信号（正则提取，必须纳入判断）\n\n"
                    + "\n".join(lines)
                    + "\n"
                )

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

        # 操作原语：封闭词表校验，未知值丢弃并记录，防止模型发明能力。
        raw_ops = data.get("operations") or []
        operations: list[str] = []
        if isinstance(raw_ops, list):
            for op in raw_ops:
                name = str(op).strip().lower()
                if name in OPERATION_VOCABULARY and name not in operations:
                    operations.append(name)
                elif name:
                    logger.debug("忽略无效操作原语: %s", name)
        chat_only = bool(data.get("chat_only", False))

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
            operations=tuple(operations),
            chat_only=chat_only,
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
                enabled = getattr(config.intent_classifier, "enabled", False)

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
