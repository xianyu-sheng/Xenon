"""
Plan-Execute Engine — 规划-执行两阶段引擎。

Phase 1: Planning — LLM 生成步骤列表
Phase 2: Execution — 逐步执行，每步结果写入 context
"""

from __future__ import annotations

import logging
from typing import Any

from omniagent.engine.context import AgentContext
from omniagent.nodes.tool_node import ToolNode
from omniagent.utils.llm_client import chat_completion
from omniagent.utils.response_adapter import parse_plan

logger = logging.getLogger(__name__)

PLAN_SYSTEM_PROMPT = """你是一个任务规划专家。将用户任务分解为可执行步骤。

直接输出以下JSON，不要输出任何其他内容、解释或思考过程：
```json
{"analysis":"简要分析","steps":[{"id":1,"task":"步骤描述","tool":null,"params":{}}]}
```

可用工具: command, read_file, write_file, list_files, search_files, git, web_fetch
不需要工具的步骤 tool 设为 null。
"""

EXECUTE_PROMPT = """你正在执行一个任务计划。当前步骤信息如下:

步骤 {step_id}/{total_steps}: {step_task}
之前步骤的结果:
{previous_results}

请完成这个步骤。如果需要使用工具，请用简洁的文字说明你要做什么。如果不需要工具，直接给出结果。
"""


class PlanExecuteEngine:
    """规划-执行两阶段引擎。"""

    def __init__(
        self,
        model_priority: list[str],
        *,
        max_steps: int = 20,
        system_prompt: str | None = None,
    ) -> None:
        self.model_priority = model_priority
        self.max_steps = max_steps
        self.system_prompt = system_prompt or PLAN_SYSTEM_PROMPT

    def run(self, user_input: str, context: AgentContext | None = None) -> str:
        """
        执行 Plan-Execute 流程。

        Args:
            user_input: 用户输入
            context: 可选的共享上下文

        Returns:
            最终执行结果
        """
        ctx = context or AgentContext()

        # Phase 1: Planning
        logger.info("Plan-Execute Phase 1: 规划中...")
        plan = self._plan(user_input, ctx)
        steps = plan.get("steps", [])

        if not steps:
            return plan.get("analysis", "未能生成有效的执行计划。")

        logger.info(f"计划生成 {len(steps)} 个步骤")

        # Phase 2: Execution
        logger.info("Plan-Execute Phase 2: 执行中...")
        results = []

        for i, step in enumerate(steps[:self.max_steps]):
            step_id = step.get("id", i + 1)
            step_task = step.get("task", "")
            tool = step.get("tool")
            params = step.get("params", {})

            logger.info(f"执行步骤 {step_id}: {step_task}")

            # 构建上下文提示
            prev_results = "\n".join(
                f"步骤 {r['step_id']}: {r['result'][:200]}"
                for r in results[-3:]  # 只保留最近 3 步
            ) if results else "(无)"

            if tool and tool != "null":
                # 使用工具执行
                result = self._execute_step_with_tool(tool, params, ctx)
            else:
                # 使用 LLM 执行
                result = self._execute_step_with_llm(
                    step_id, len(steps), step_task, prev_results, user_input
                )

            results.append({
                "step_id": step_id,
                "task": step_task,
                "result": result,
            })

            ctx.set(f"step_{step_id}_result", result)
            logger.info(f"步骤 {step_id} 完成: {result[:100]}")

        # 汇总结果
        summary = self._summarize(user_input, plan.get("analysis", ""), results)
        return summary

    def _plan(self, user_input: str, context: AgentContext | None = None) -> dict[str, Any]:
        """Phase 1: 生成执行计划。"""
        messages = [{"role": "system", "content": self.system_prompt}]
        # 注入对话历史（最近 6 条，排除 system 消息）
        if context:
            history = context.get_conversation_messages()
            if history:
                recent = [m for m in history if m.get("role") != "system"][-6:]
                messages.extend(recent)
                logger.info(f"Plan 注入 {len(recent)} 条对话历史")
            else:
                logger.warning("Plan: 无对话历史可注入！")
        else:
            logger.warning("Plan: context 为 None！")

        response = self._call_llm(messages)
        if not response or not response.strip():
            logger.warning("LLM 返回了空响应！请检查 API 配置和模型是否支持。")
        else:
            logger.info(f"LLM 原始响应 (前500字): {response[:500]}")
        result = self._parse_json(response)
        logger.info(f"解析后: steps={len(result.get('steps', []))}, analysis={result.get('analysis', '')[:100]}")
        return result

    def _execute_step_with_tool(self, tool: str, params: dict, context: AgentContext) -> str:
        """使用工具执行步骤。"""
        try:
            node = ToolNode(f"plan_{tool}", action_type=tool, **params)
            result = node.execute(context)

            if result.get("success"):
                for key in ("content", "stdout", "output", "files"):
                    if key in result and result[key]:
                        val = result[key]
                        if isinstance(val, list):
                            return "\n".join(str(v) for v in val[:30])
                        return str(val)[:2000]
                return "执行成功"
            else:
                return f"执行失败: {result.get('error', result)}"

        except Exception as e:
            return f"执行异常: {e}"

    def _execute_step_with_llm(
        self, step_id: int, total: int, task: str, prev_results: str, original: str,
    ) -> str:
        """使用 LLM 执行不需要工具的步骤。"""
        prompt = EXECUTE_PROMPT.format(
            step_id=step_id, total_steps=total,
            step_task=task, previous_results=prev_results,
        )
        messages = [
            {"role": "system", "content": f"原始任务: {original}"},
            {"role": "user", "content": prompt},
        ]
        return self._call_llm(messages)

    def _summarize(self, original: str, analysis: str, results: list[dict]) -> str:
        """汇总所有步骤的结果。"""
        results_text = "\n".join(
            f"步骤 {r['step_id']} ({r['task']}): {r['result'][:300]}"
            for r in results
        )

        messages = [
            {"role": "system", "content": "请根据以下执行结果，给出简洁的最终总结。"},
            {"role": "user", "content": f"原始任务: {original}\n\n分析: {analysis}\n\n执行结果:\n{results_text}"},
        ]
        return self._call_llm(messages)

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

    def _parse_json(self, text: str) -> dict[str, Any]:
        """从 LLM 输出中提取 JSON（委托给 response_adapter 中间件）。"""
        return parse_plan(text)
