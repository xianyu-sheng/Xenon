"""
Reflection Engine — 执行-审查-修正循环引擎。

循环: Execute(LLM 生成) → Review(LLM 审查) → 修正 → 直到审查通过
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from omniagent.engine.base import BaseEngine
from omniagent.engine.callbacks import ConsoleCallback, EngineCallback, SilentCallback
from omniagent.engine.context import AgentContext
from omniagent.utils.response_adapter import parse_review

if TYPE_CHECKING:
    from omniagent.repl.context_manager import ContextManager

logger = logging.getLogger(__name__)

EXECUTOR_PROMPT = """你是一个专业的代码和技术执行者。请根据用户的需求生成高质量的输出。

要求：
- 代码必须语法正确、可直接运行
- 如果用户要求创建文件，直接输出完整文件内容
- 如果这是修正轮次，根据审查反馈逐条改进，不要遗漏任何问题
- 用中文解释，代码用英文
"""

REVIEWER_PROMPT = """你是一个严格的质量审查员。请审查执行者的输出，判断是否满足用户需求。

## 输出格式

只输出一个 JSON，不要输出其他内容：
```json
{{"pass": true 或 false, "score": 1-10 的评分, "feedback": "具体评价", "issues": ["问题1", "问题2"]}}
```

## 评分标准

- **9-10 分（通过）**: 完美满足需求，代码可运行，无任何问题
- **7-8 分（通过）**: 基本满足需求，有小瑕疵但不影响使用
- **5-6 分（不通过）**: 部分满足需求，有明显问题需要修正
- **1-4 分（不通过）**: 严重偏离需求，需要大幅重做

## 审查要点

1. **完整性**: 是否完整回答了用户的所有需求？
2. **正确性**: 代码是否语法正确、逻辑正确、可运行？
3. **安全性**: 是否有安全隐患（如硬编码密码、SQL注入等）？
4. **规范性**: 命名是否清晰、格式是否规范、有无注释？
5. **遗漏**: 是否遗漏了用户提到的任何要求？

## 常见扣分项

- 语法错误 → score ≤ 5
- 遗漏用户明确要求的功能 → score ≤ 6
- 代码可运行但逻辑有误 → score ≤ 6
- 格式混乱、命名不清 → score ≤ 7
- 缺少必要的注释或文档 → score ≤ 8

pass=true 当且仅当 score >= 7。
"""


class ReflectionEngine(BaseEngine):
    """执行-审查-修正循环引擎。"""

    def __init__(
        self,
        model_priority: list[str],
        *,
        max_rounds: int = 3,
        pass_threshold: int = 7,
        executor_prompt: str | None = None,
        reviewer_prompt: str | None = None,
        callback: EngineCallback | None = None,
        model_configs: dict[str, Any] | None = None,
    ) -> None:
        # R2: 公共属性与 _call_llm 由 BaseEngine 提供。
        super().__init__(
            model_priority, callback=callback,
            model_configs=model_configs, temperature=0.3,
        )
        self.max_rounds = max_rounds
        self.pass_threshold = pass_threshold
        self.executor_prompt = executor_prompt or EXECUTOR_PROMPT
        self.reviewer_prompt = reviewer_prompt or REVIEWER_PROMPT

    def run(
        self,
        user_input: str,
        context: AgentContext | None = None,
        ctx_mgr: ContextManager | None = None,
    ) -> str:
        """
        执行 Reflection 流程。

        Args:
            user_input: 用户输入
            context: 可选的共享上下文（含对话历史）
            ctx_mgr: F4 注入的 ContextManager——提供时 _execute 消费其（已压缩）消息
                而非自行 ``[-6:]`` 截断。

        Returns:
            修正后的最终输出
        """
        feedback = ""
        self._reset_interrupt()
        self._ctx_mgr = ctx_mgr  # F4
        self._begin_run()  # P3-Q2: 链路追踪

        for round_num in range(1, self.max_rounds + 1):
            if self._interrupted:
                self.callback.on_warning("引擎被用户中断，停止修正")
                logger.info("Reflection 被中断，退出修正循环")
                break
            logger.debug(f"Reflection 第 {round_num} 轮")

            # Execute
            output = self._execute(user_input, feedback, context)

            # Review
            review = self._review(user_input, output)
            score = review.get("score", 0)
            passed = review.get("pass") and score >= self.pass_threshold

            self.callback.on_review(score, passed, review.get("feedback", "")[:200])

            if passed:
                logger.info(f"审查通过 (分数: {score})")
                self.callback.on_finish(output)
                return output

            feedback = review.get("feedback", "请改进输出质量")
            issues = review.get("issues", [])
            logger.debug(f"审查未通过: {feedback}")

            if issues:
                feedback += "\n具体问题:\n" + "\n".join(f"- {i}" for i in issues)

        logger.info(f"达到最大修正轮次 ({self.max_rounds})，返回最后一轮输出")
        self.callback.on_warning(f"达到最大修正轮次 ({self.max_rounds})")
        self.callback.on_finish(output)
        return output

    def _execute(self, user_input: str, feedback: str = "", context: AgentContext | None = None) -> str:
        """执行阶段: LLM 生成输出。"""
        messages = [{"role": "system", "content": self.executor_prompt}]
        # F4: 优先消费 ctx_mgr（已压缩）消息；否则回退 AgentContext 历史 [-6:]
        history = self._history_messages(context)
        if history:
            if self._ctx_mgr is not None:
                messages.extend(history)
            else:
                messages.extend(history[-6:])

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

    def _parse_review(self, response: str) -> dict[str, Any]:
        """解析审查结果 JSON（委托给 response_adapter 中间件）。"""
        return parse_review(response)
