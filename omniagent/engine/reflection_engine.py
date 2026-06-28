"""
Reflection Engine — 执行-审查-修正循环引擎。

循环: Execute(LLM 生成) → Review(LLM 审查) → 修正 → 直到审查通过
"""

from __future__ import annotations

import logging
from typing import Any

from omniagent.engine.callbacks import ConsoleCallback, EngineCallback, SilentCallback
from omniagent.engine.context import AgentContext
from omniagent.utils.llm_client import chat_completion
from omniagent.utils.response_adapter import parse_review

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


class ReflectionEngine:
    """执行-审查-修正循环引擎。"""

    def __init__(
        self,
        model_priority: list[str],
        *,
        executor_model_priority: list[str] | None = None,
        reviewer_model_priority: list[str] | None = None,
        max_rounds: int = 3,
        pass_threshold: int = 7,
        executor_prompt: str | None = None,
        reviewer_prompt: str | None = None,
        callback: EngineCallback | None = None,
    ) -> None:
        self.model_priority = model_priority  # 默认模型（回退用）
        self.executor_model_priority = executor_model_priority or model_priority
        self.reviewer_model_priority = reviewer_model_priority or model_priority
        self.max_rounds = max_rounds
        self.pass_threshold = pass_threshold
        self.executor_prompt = executor_prompt or EXECUTOR_PROMPT
        self.reviewer_prompt = reviewer_prompt or REVIEWER_PROMPT
        self.callback = callback or EngineCallback()

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
            logger.debug(f"Reflection 第 {round_num} 轮")

            try:
                # Execute
                output = self._execute(user_input, feedback, context)

                # Review
                review = self._review(user_input, output)
                score = review.get("score", 0)
                passed = review.get("pass") and score >= self.pass_threshold

                self.callback.on_review(score, passed, review.get("feedback", "")[:200])

                if passed:
                    logger.debug(f"审查通过 (分数: {score})")
                    self.callback.on_finish(output)
                    return output

                feedback = review.get("feedback", "请改进输出质量")
                issues = review.get("issues", [])
                logger.debug(f"审查未通过: {feedback}")

                if issues:
                    feedback += "\n具体问题:\n" + "\n".join(f"- {i}" for i in issues)

            except Exception as round_error:
                logger.error(f"Reflection 第 {round_num} 轮异常: {round_error}", exc_info=True)
                if round_num < self.max_rounds:
                    feedback = f"执行过程中出现异常: {round_error}。请尝试不同的方式完成任务。"
                    continue
                self.callback.on_error(f"Reflection 引擎异常: {round_error}")
                # 返回已有输出（如果有的话），否则返回错误消息
                return locals().get("output", f"## 引擎异常\n\n{round_error}")

        logger.debug(f"达到最大修正轮次 ({self.max_rounds})，返回最后一轮输出")
        self.callback.on_warning(f"达到最大修正轮次 ({self.max_rounds})")
        # 验证最终输出质量
        try:
            from omniagent.engine.react_engine import _is_substantive_answer
            if not _is_substantive_answer(output):
                warning = "\n\n[注意] 输出可能为计划描述而非实际结果，建议重新运行。"
                output = output + warning
        except ImportError:
            pass
        self.callback.on_finish(output)
        return output

    def _execute(self, user_input: str, feedback: str = "", context: AgentContext | None = None) -> str:
        """执行阶段: 检测工具需求，必要时委托 ReAct 引擎执行。"""
        # 检测是否需要工具执行
        requires_tools = self._input_needs_tools(user_input)

        if requires_tools:
            # 委托给 ReActEngine 执行（有工具能力）
            logger.info("Reflection: 检测到工具需求，委托 ReAct 引擎执行")
            try:
                from omniagent.engine.react_engine import ReActEngine
                react_engine = ReActEngine(
                    model_priority=self.executor_model_priority,
                    max_iterations=12,
                    callback=SilentCallback(),
                )
                react_result = react_engine.run(user_input, context)
                if feedback:
                    # 如果有反馈，追加到结果中让下一轮考虑
                    react_result = (
                        f"{react_result}\n\n"
                        f"[审查反馈]\n{feedback}\n\n"
                        f"请根据反馈改进上述输出。"
                    )
                return react_result
            except Exception as e:
                logger.error(f"Reflection ReAct 委托失败: {e}，回退到纯文本模式")

        # 纯文本模式（不需要工具时使用）
        messages = [{"role": "system", "content": self.executor_prompt}]
        if context:
            history = context.get_conversation_messages()
            if history:
                non_system = [m for m in history if m.get("role") != "system"][-6:]
                system_msgs = [m for m in history if m.get("role") == "system"][-2:]
                recent = system_msgs + non_system
                messages.extend(recent)

        if feedback:
            messages.append({
                "role": "user",
                "content": f"原始需求: {user_input}\n\n上一轮审查反馈:\n{feedback}\n\n请根据反馈改进你的输出。",
            })
        else:
            messages.append({"role": "user", "content": user_input})

        return self._call_llm(messages, model_priority=self.executor_model_priority)

    @staticmethod
    def _input_needs_tools(text: str) -> bool:
        """检测用户输入是否需要工具执行。"""
        try:
            from omniagent.repl.prompt_optimizer import classify_input
            result = classify_input(text)
            return result.get("requires_tools", False)
        except ImportError:
            # 回退: 简单关键词检测
            tool_keywords = [
                "文件", "创建", "写入", "读取", "修改", "删除", "编辑",
                "执行", "运行", "命令", "git", "安装", "搜索",
                ".py", ".js", ".ts", ".md", ".txt", ".json",
                "章", "章节", "大纲", "代码", "脚本",
            ]
            return any(kw in text.lower() for kw in tool_keywords)

    def _review(self, user_input: str, output: str) -> dict[str, Any]:
        """审查阶段: LLM 审查输出（使用 reviewer 模型）。"""
        messages = [
            {"role": "system", "content": self.reviewer_prompt},
            {"role": "user", "content": f"用户需求:\n{user_input}\n\n执行者输出:\n{output}"},
        ]

        response = self._call_llm(messages, model_priority=self.reviewer_model_priority)
        return self._parse_review(response)

    def _call_llm(
        self,
        messages: list[dict[str, str]],
        max_tokens: int = 131072,
        *,
        model_priority: list[str] | None = None,
    ) -> str:
        """调用 LLM，支持多模型 fallback 和按阶段切换模型。"""
        models = model_priority or self.model_priority
        last_error = None
        for model_id in models:
            try:
                return chat_completion(model_id, messages, max_tokens=max_tokens, temperature=0.3)
            except Exception as e:
                last_error = e
                logger.warning(f"模型 {model_id} 失败: {e}")
        raise RuntimeError(f"所有模型均调用失败: {last_error}")

    def _parse_review(self, response: str) -> dict[str, Any]:
        """解析审查结果 JSON（委托给 response_adapter 中间件）。"""
        return parse_review(response)
