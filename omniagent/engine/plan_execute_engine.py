"""
Plan-Execute Engine — 规划-执行两阶段引擎。

Phase 1: Planning — LLM 生成步骤列表
Phase 2: Execution — 逐步执行，每步结果写入 context
"""

from __future__ import annotations

import json
import logging
from typing import Any

from omniagent.engine.context import AgentContext
from omniagent.nodes.tool_node import ToolNode
from omniagent.utils.llm_client import chat_completion

logger = logging.getLogger(__name__)

PLAN_SYSTEM_PROMPT = """你是一个任务规划专家。用户会给你一个任务，你需要将其分解为可执行的步骤列表。

请严格按照以下 JSON 格式输出（不要输出其他内容）：
```json
{
  "analysis": "对任务的简要分析",
  "steps": [
    {"id": 1, "task": "步骤描述", "tool": "工具名称或 null", "params": {}},
    {"id": 2, "task": "步骤描述", "tool": "工具名称或 null", "params": {}}
  ]
}
```

可用工具:
- command: 执行终端命令 (action: 命令)
- read_file: 读取文件 (file_path: 路径)
- write_file: 写入文件 (file_path: 路径, content: 内容)
- list_files: 列出文件 (file_path: 目录, pattern: glob)
- search_files: 搜索内容 (file_path: 目录, search_pattern: 关键词)
- git: Git 操作 (git_command: status|diff|log|add|commit)
- web_fetch: 抓取网页 (url: 网址)

如果某步骤不需要工具（如纯思考），tool 设为 null。
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
        plan = self._plan(user_input)
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

    def _plan(self, user_input: str) -> dict[str, Any]:
        """Phase 1: 生成执行计划。"""
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_input},
        ]

        response = self._call_llm(messages)
        return self._parse_json(response)

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

    def _call_llm(self, messages: list[dict[str, str]]) -> str:
        """调用 LLM，支持多模型 fallback。"""
        last_error = None
        for model_id in self.model_priority:
            try:
                return chat_completion(model_id, messages, max_tokens=2048, temperature=0.3)
            except Exception as e:
                last_error = e
                logger.warning(f"模型 {model_id} 失败: {e}")
        raise RuntimeError(f"所有模型均调用失败: {last_error}")

    def _parse_json(self, text: str) -> dict[str, Any]:
        """从 LLM 输出中提取 JSON。"""
        text = text.strip()
        if "```json" in text:
            start = text.find("```json") + 7
            end = text.find("```", start)
            if end != -1:
                text = text[start:end].strip()
        elif "```" in text:
            start = text.find("```") + 3
            end = text.find("```", start)
            if end != -1:
                text = text[start:end].strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            brace_start = text.find("{")
            brace_end = text.rfind("}")
            if brace_start != -1 and brace_end != -1:
                try:
                    return json.loads(text[brace_start:brace_end + 1])
                except json.JSONDecodeError:
                    pass
            return {"analysis": text, "steps": []}
