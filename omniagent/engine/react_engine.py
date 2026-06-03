"""
ReAct Engine — 思考-行动-观察循环引擎。

ReAct 模式: Think → Act → Observe → 循环直到完成
- Think: LLM 分析当前状态，决定下一步行动
- Act: 执行工具（ToolNode）
- Observe: 将工具结果反馈给 LLM
"""

from __future__ import annotations

import json
import logging
from typing import Any

from omniagent.engine.context import AgentContext
from omniagent.nodes.tool_node import ToolNode
from omniagent.utils.llm_client import chat_completion

logger = logging.getLogger(__name__)

# ReAct 系统提示
REACT_SYSTEM_PROMPT = """你是一个 ReAct 模式的 AI 助手。你通过思考-行动-观察的循环来解决问题。

每次回复，请严格按照以下 JSON 格式输出（不要输出其他内容）：

如果需要使用工具：
```json
{"thought": "你的思考过程", "action": "工具名称", "action_input": {"参数名": "参数值"}}
```

如果任务已完成：
```json
{"thought": "你的思考过程", "final_answer": "最终答案"}
```

可用工具:
{tools_desc}
"""

# 内置工具描述
BUILTIN_TOOLS = {
    "command": {
        "name": "command",
        "description": "执行终端命令",
        "params": {"action": "要执行的命令"},
    },
    "read_file": {
        "name": "read_file",
        "description": "读取文件内容",
        "params": {"file_path": "文件路径"},
    },
    "write_file": {
        "name": "write_file",
        "description": "将内容写入文件",
        "params": {"file_path": "文件路径", "content": "要写入的内容"},
    },
    "list_files": {
        "name": "list_files",
        "description": "列出目录中的文件",
        "params": {"file_path": "目录路径", "pattern": "glob 模式，如 *.py"},
    },
    "search_files": {
        "name": "search_files",
        "description": "在文件中搜索内容",
        "params": {"file_path": "搜索目录", "search_pattern": "搜索关键词", "file_filter": "文件过滤，如 *.py"},
    },
    "git": {
        "name": "git",
        "description": "执行 Git 操作",
        "params": {"git_command": "status|diff|log|add|commit|branch"},
    },
    "web_fetch": {
        "name": "web_fetch",
        "description": "抓取网页内容",
        "params": {"url": "网址"},
    },
    "edit_file": {
        "name": "edit_file",
        "description": "精确编辑文件（查找替换）",
        "params": {
            "file_path": "文件路径",
            "old_text": "要替换的原始文本（必须精确匹配）",
            "new_text": "替换后的文本",
        },
    },
}


class ReActEngine:
    """ReAct 思考-行动-观察循环引擎。"""

    def __init__(
        self,
        model_priority: list[str],
        *,
        max_iterations: int = 10,
        system_prompt: str | None = None,
        tools: dict[str, dict] | None = None,
    ) -> None:
        self.model_priority = model_priority
        self.max_iterations = max_iterations
        self.tools = tools or BUILTIN_TOOLS
        self.system_prompt = system_prompt or self._build_system_prompt()

    def _build_system_prompt(self) -> str:
        tools_desc = "\n".join(
            f"- {t['name']}: {t['description']} (参数: {t['params']})"
            for t in self.tools.values()
        )
        return REACT_SYSTEM_PROMPT.format(tools_desc=tools_desc)

    def run(self, user_input: str, context: AgentContext | None = None) -> str:
        """
        执行 ReAct 循环。

        Args:
            user_input: 用户输入
            context: 可选的共享上下文

        Returns:
            最终答案文本
        """
        ctx = context or AgentContext()
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_input},
        ]

        for i in range(self.max_iterations):
            logger.info(f"ReAct 迭代 {i + 1}/{self.max_iterations}")

            # 调用 LLM
            response = self._call_llm(messages)
            messages.append({"role": "assistant", "content": response})

            # 解析 LLM 输出
            parsed = self._parse_response(response)

            if "final_answer" in parsed:
                logger.info(f"ReAct 完成，共 {i + 1} 次迭代")
                return parsed["final_answer"]

            if "action" in parsed:
                # 执行工具
                action = parsed["action"]
                action_input = parsed.get("action_input", {})
                thought = parsed.get("thought", "")

                logger.info(f"ReAct 思考: {thought}")
                logger.info(f"ReAct 行动: {action}({action_input})")

                observation = self._execute_tool(action, action_input, ctx)

                # 将观察结果加入对话
                obs_msg = f"Observation: {observation}"
                messages.append({"role": "user", "content": obs_msg})
                logger.info(f"ReAct 观察: {observation[:200]}")
            else:
                # LLM 没有给出有效输出，直接返回
                return parsed.get("thought", response)

        return f"达到最大迭代次数 ({self.max_iterations})，未能得出最终答案。"

    def _call_llm(self, messages: list[dict[str, str]]) -> str:
        """调用 LLM，支持多模型 fallback。"""
        last_error = None
        for model_id in self.model_priority:
            try:
                return chat_completion(model_id, messages, max_tokens=2048, temperature=0.3)
            except Exception as e:
                last_error = e
                logger.warning(f"模型 {model_id} 失败: {e}，尝试下一个...")
        raise RuntimeError(f"所有模型均调用失败: {last_error}")

    def _parse_response(self, response: str) -> dict[str, Any]:
        """解析 LLM 的 JSON 输出。"""
        # 尝试提取 JSON 块
        text = response.strip()

        # 处理 markdown 代码块
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
            # 尝试找到第一个 { 和最后一个 }
            brace_start = text.find("{")
            brace_end = text.rfind("}")
            if brace_start != -1 and brace_end != -1:
                try:
                    return json.loads(text[brace_start:brace_end + 1])
                except json.JSONDecodeError:
                    pass

            # 解析失败，当作最终答案
            return {"thought": response, "final_answer": response}

    def _execute_tool(self, action: str, action_input: dict, context: AgentContext) -> str:
        """执行工具并返回结果。"""
        tool_info = self.tools.get(action)
        if not tool_info:
            return f"错误: 未知工具 '{action}'，可用工具: {list(self.tools.keys())}"

        try:
            node = ToolNode(
                f"react_{action}",
                action_type=action,
                **action_input,
            )
            result = node.execute(context)

            if result.get("success"):
                # 提取主要内容
                for key in ("content", "stdout", "output", "files"):
                    if key in result and result[key]:
                        val = result[key]
                        if isinstance(val, list):
                            return "\n".join(str(v) for v in val[:50])
                        return str(val)[:3000]
                return str(result)[:3000]
            else:
                return f"工具执行失败: {result.get('error', result)}"

        except Exception as e:
            return f"工具执行异常: {e}"
