"""
Reflection Engine — 执行-审查-修正循环引擎。

循环: Execute(LLM 生成) → Review(LLM 审查) → 修正 → 直到审查通过
"""

from __future__ import annotations

import logging
from typing import Any

from omniagent.engine.context import AgentContext
from omniagent.utils.llm_client import chat_completion
from omniagent.utils.response_adapter import parse_review

logger = logging.getLogger(__name__)

EXECUTOR_PROMPT = """你是一个专业的代码和技术执行者。请根据用户的需求生成高质量的输出。
如果这是修正轮次，请根据审查反馈改进你的输出。
"""

REVIEWER_PROMPT = """你是一个严格的质量审查员。请审查执行者的输出，判断是否满足用户需求。

请严格按照以下 JSON 格式输出（不要输出其他内容）：
```json
{
  "pass": true 或 false,
  "score": 1-10 的评分,
  "feedback": "具体的改进建议（如果 pass=false）或简要评价（如果 pass=true）",
  "issues": ["问题1", "问题2"]
}
```

审查标准：
1. 是否完整回答了用户的问题
2. 代码是否正确、可运行
3. 是否有遗漏或错误
4. 格式是否规范
"""


class ReflectionEngine:
    """执行-审查-修正循环引擎。"""

    def __init__(
        self,
        model_priority: list[str],
        *,
        max_rounds: int = 3,
        pass_threshold: int = 7,
        executor_prompt: str | None = None,
        reviewer_prompt: str | None = None,
    ) -> None:
        self.model_priority = model_priority
        self.max_rounds = max_rounds
        self.pass_threshold = pass_threshold
        self.executor_prompt = executor_prompt or EXECUTOR_PROMPT
        self.reviewer_prompt = reviewer_prompt or REVIEWER_PROMPT

    def run(self, user_input: str, context: AgentContext | None = None) -> str:
        """
        执行 Reflection 流程。

        Args:
            user_input: 用户输入
            context: 可选的共享上下文（含对话历史）

        Returns:
            修正后的最终输出
        """
        feedback = ""

        for round_num in range(1, self.max_rounds + 1):
            logger.info(f"Reflection 第 {round_num} 轮")

            # Execute
            output = self._execute(user_input, feedback, context)

            # Review
            review = self._review(user_input, output)

            if review.get("pass") and review.get("score", 0) >= self.pass_threshold:
                logger.info(f"审查通过 (分数: {review.get('score')})")
                return output

            feedback = review.get("feedback", "请改进输出质量")
            issues = review.get("issues", [])
            logger.info(f"审查未通过: {feedback}")

            if issues:
                feedback += "\n具体问题:\n" + "\n".join(f"- {i}" for i in issues)

        logger.info(f"达到最大修正轮次 ({self.max_rounds})，返回最后一轮输出")
        return output

    def _execute(self, user_input: str, feedback: str = "", context: AgentContext | None = None) -> str:
        """执行阶段: LLM 生成输出。"""
        messages = [{"role": "system", "content": self.executor_prompt}]
        # 注入对话历史（最近 6 条，排除 system 消息）
        if context:
            history = context.get_conversation_messages()
            if history:
                recent = [m for m in history if m.get("role") != "system"][-6:]
                messages.extend(recent)

        if feedback:
            messages.append({
                "role": "user",
                "content": f"原始需求: {user_input}\n\n上一轮审查反馈:\n{feedback}\n\n请根据反馈改进你的输出。",
            })
        else:
            messages.append({"role": "user", "content": user_input})

        return self._call_llm(messages)

    def _review(self, user_input: str, output: str) -> dict[str, Any]:
        """审查阶段: LLM 审查输出。"""
        messages = [
            {"role": "system", "content": self.reviewer_prompt},
            {"role": "user", "content": f"用户需求:\n{user_input}\n\n执行者输出:\n{output}"},
        ]

        response = self._call_llm(messages)
        return self._parse_review(response)

    def _call_llm(self, messages: list[dict[str, str]], max_tokens: int = 131072) -> str:
        """调用 LLM，支持多模型 fallback。"""
        last_error = None
        for model_id in self.model_priority:
            try:
                return chat_completion(model_id, messages, max_tokens=max_tokens, temperature=0.3)
            except Exception as e:
                last_error = e
                logger.warning(f"模型 {model_id} 失败: {e}")
        raise RuntimeError(f"所有模型均调用失败: {last_error}")

    def _parse_review(self, response: str) -> dict[str, Any]:
        """解析审查结果 JSON（委托给 response_adapter 中间件）。"""
        return parse_review(response)
