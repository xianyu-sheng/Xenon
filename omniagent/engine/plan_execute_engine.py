"""
Plan-Execute Engine — 规划-执行两阶段引擎。

Phase 1: Planning — LLM 生成步骤列表
Phase 2: Execution — 逐步执行，每步结果写入 context
"""

from __future__ import annotations

import logging
from typing import Any

from omniagent.engine.callbacks import EngineCallback
from omniagent.engine.context import AgentContext
from omniagent.engine.tool_tracker import ToolExecutionTracker
from omniagent.nodes.tool_node import ToolNode
from omniagent.utils.llm_client import chat_completion
from omniagent.utils.response_adapter import parse_plan

logger = logging.getLogger(__name__)

PLAN_SYSTEM_PROMPT = """你是一个任务规划专家。将用户任务分解为可执行的原子步骤。

## 输出格式

只输出一个 JSON，不要输出其他任何内容：
```json
{{"analysis":"简要分析任务目标","steps":[{{"id":1,"task":"步骤描述","tool":"工具名或null","params":{{"参数名":"值"}}}}]}}
```

## 规划原则

1. **每步只做一个原子操作**：不要在一个步骤中"创建5个文件"，而是分成5个步骤
2. **参数名必须使用标准名称**：file_path（不是 path）、action（不是 command）、content（不是 text）等
3. **tool 字段必须是下方列表中的精确工具名**，严禁发明或猜测工具名
4. **不需要工具的步骤**：tool 设为 null，如"分析需求"、"设计方案"
5. **先读后写**：修改文件前先 read_file 查看内容
6. **步骤顺序合理**：依赖关系靠前的步骤排在前面

## ⚠️ 重要：可用工具列表（完整且唯一）

以下是所有可用工具，不存在其他工具。tool 字段必须是下列之一或 null：

- command: {{"action": "终端命令"}} — 在本机终端执行 shell 命令（Windows 用 PowerShell）。不能用于读写文件。
- read_file: {{"file_path": "路径", "start_line": "起始行号(可选,从1开始)", "max_lines": "读取行数(可选)"}} — 读取本机文件内容，支持分段读取。仅限本地文件，不能读 URL 或 GitHub 文件。
- write_file: {{"file_path": "路径", "content": "内容"}} — 将内容写入本机文件（覆盖）。自动创建父目录。
- list_files: {{"file_path": "目录", "pattern": "*.py"}} — 列出本机目录文件。仅限本地，不能列 GitHub 仓库。
- search_files: {{"file_path": "目录", "search_pattern": "关键词"}} — 在本机文件中搜索关键词（类似 grep）。
- git: {{"git_command": "status|diff|log|add|commit"}} — 本机 Git 操作（查看类+基本操作）。
- web_fetch: {{"url": "完整URL"}} — HTTP GET 抓取任意 URL 内容（HTML 自动转文本）。不能列 GitHub 仓库文件。
- github_fetch: {{"repo": "owner/repo", "github_action": "list_files|fetch_file|fetch_readme", "github_path": "文件路径(fetch_file用)", "branch": "main"}} — GitHub 仓库专用：list_files 列出所有文件，fetch_file 获取文件源码，fetch_readme 获取 README。仅支持公开仓库。
- edit_file: {{"file_path": "路径", "old_text": "原文（必须精确匹配）", "new_text": "新文"}} — 精确查找替换编辑本机文件。
- create_directory: {{"file_path": "目录路径"}} — 创建目录（自动递归创建父目录）。
- batch_write: {{"files": [{{"path": "a.py", "content": "..."}}, ...]}} — 原子性批量写入多个文件。
- batch_edit: {{"edits": [{{"file_path": "a.py", "old_text": "...", "new_text": "..."}}, ...]}} — 批量编辑多个文件。
- code_index: {{"search_pattern": "符号名", "file_path": "目录"}} — 基于 AST 搜索 Python 代码符号（函数/类/变量）。
- ast_analyze: {{"file_path": "Python文件"}} — AST 深度分析 Python 文件（签名、复杂度、未用 import）。
- refactor: {{"refactor_action": "rename|clean_imports|analyze", "old_name": "旧名", "new_name": "新名", "file_path": "路径"}} — 代码重构（重命名/清理导入/分析建议）。
- diff_preview: {{"file_path": "路径", "old_text": "原文", "new_text": "新文"}} — 预览修改 diff（不实际改文件）。
- mcp_call: {{"tool_name": "server:tool", "tool_args": {{}}}} — 调用 MCP 外部工具服务器。

## 分析 GitHub 项目的标准流程

当用户要求分析 GitHub 仓库时，必须按以下顺序执行：
1. github_fetch(repo="owner/repo", github_action="list_files") — 先列出所有文件
2. github_fetch(repo="owner/repo", github_action="fetch_readme") — 获取 README
3. github_fetch(repo="owner/repo", github_action="fetch_file", github_path="app.py") — 逐个获取关键源码文件
4. 基于实际获取的代码进行分析（不要凭空猜测）

## 示例

用户: 创建一个 Flask hello world 项目
```json
{{"analysis":"创建一个最小的 Flask 应用","steps":[{{"id":1,"task":"创建 app.py 文件","tool":"write_file","params":{{"file_path":"app.py","content":"from flask import Flask\\napp = Flask(__name__)\\n\\n@app.route('/')\\ndef hello():\\n    return 'Hello World!'"}}}},{{"id":2,"task":"创建 requirements.txt","tool":"write_file","params":{{"file_path":"requirements.txt","content":"flask>=3.0"}}}},{{"id":3,"task":"验证文件是否创建成功","tool":"list_files","params":{{"file_path":"."}}}}]}}
```

用户: 分析 https://github.com/owner/repo 项目
```json
{{"analysis":"分析 GitHub 项目的代码质量和结构","steps":[{{"id":1,"task":"列出仓库所有文件","tool":"github_fetch","params":{{"repo":"owner/repo","github_action":"list_files"}}}},{{"id":2,"task":"获取 README 了解项目概述","tool":"github_fetch","params":{{"repo":"owner/repo","github_action":"fetch_readme"}}}},{{"id":3,"task":"获取主入口文件代码","tool":"github_fetch","params":{{"repo":"owner/repo","github_action":"fetch_file","github_path":"app.py"}}}},{{"id":4,"task":"获取工具模块代码","tool":"github_fetch","params":{{"repo":"owner/repo","github_action":"fetch_file","github_path":"utils.py"}}}},{{"id":5,"task":"基于实际代码进行分析总结","tool":null,"params":{{}}}}]}}
```

## 运行环境

规划时请注意：如果需要执行命令，必须根据操作系统选择正确的命令格式。
Windows 使用 PowerShell（如 mkdir, copy, Get-ChildItem），不要使用 Linux 命令（ls, cat, mkdir -p 等）。
"""

EXECUTE_PROMPT = """你正在执行一个任务计划的第 {step_id} 步（共 {total_steps} 步）。

当前步骤: {step_task}

之前步骤的结果:
{previous_results}

请完成这个步骤。
- 如果需要使用工具，说明你要做什么以及使用什么工具和参数
- 如果不需要工具，直接给出结果
- 如果之前步骤失败了，分析原因并尝试修复
"""


class PlanExecuteEngine:
    """规划-执行两阶段引擎。"""

    def __init__(
        self,
        model_priority: list[str],
        *,
        max_steps: int = 20,
        system_prompt: str | None = None,
        callback: EngineCallback | None = None,
    ) -> None:
        self.model_priority = model_priority
        self.max_steps = max_steps
        self.system_prompt = system_prompt or PLAN_SYSTEM_PROMPT
        self.callback = callback or EngineCallback()

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
        tracker = ToolExecutionTracker()

        # Phase 1: Planning
        logger.info("Plan-Execute Phase 1: 规划中...")
        plan = self._plan(user_input, ctx)
        steps = plan.get("steps", [])

        if not steps:
            self.callback.on_warning("未能生成有效的执行计划")
            return plan.get("analysis", "未能生成有效的执行计划。")

        logger.info(f"计划生成 {len(steps)} 个步骤")
        total = min(len(steps), self.max_steps)

        # Phase 2: Execution
        logger.info("Plan-Execute Phase 2: 执行中...")
        results = []

        for i, step in enumerate(steps[:self.max_steps]):
            step_id = step.get("id", i + 1)
            step_task = step.get("task", "")
            tool = step.get("tool")
            params = step.get("params", {})

            logger.info(f"执行步骤 {step_id}: {step_task}")
            self.callback.on_step(step_id, total, step_task)

            # 构建上下文提示
            prev_results = "\n".join(
                f"步骤 {r['step_id']}: {r['result'][:200]}"
                for r in results[-3:]  # 只保留最近 3 步
            ) if results else "(无)"

            if tool and tool != "null":
                # 使用工具执行
                result = self._execute_step_with_tool(tool, params, ctx, tracker)
            else:
                # 使用 LLM 执行 — 会验证文件操作声明
                result = self._execute_step_with_llm(
                    step_id, len(steps), step_task, prev_results, user_input, tracker
                )

            results.append({
                "step_id": step_id,
                "task": step_task,
                "result": result,
            })

            ctx.set(f"step_{step_id}_result", result)
            success = not result.startswith(("执行失败", "执行异常"))
            self.callback.on_step_done(step_id, success, result[:200])
            logger.info(f"步骤 {step_id} 完成: {result[:100]}")

        # 汇总结果 — 附加工具执行摘要
        summary = self._summarize(user_input, plan.get("analysis", ""), results, tracker)
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

        # 关键：将当前用户输入加入消息列表
        messages.append({"role": "user", "content": user_input})

        response = self._call_llm(messages)
        if not response or not response.strip():
            logger.warning("LLM 返回了空响应！请检查 API 配置和模型是否支持。")
        else:
            logger.info(f"LLM 原始响应 (前500字): {response[:500]}")
        result = self._parse_json(response)
        logger.info(f"解析后: steps={len(result.get('steps', []))}, analysis={result.get('analysis', '')[:100]}")
        return result

    def _execute_step_with_tool(
        self, tool: str, params: dict, context: AgentContext,
        tracker: ToolExecutionTracker | None = None,
    ) -> str:
        """使用工具执行步骤。"""
        try:
            params = ToolNode.normalize_params(params)
            self.callback.on_act(tool, params)
            node = ToolNode(f"plan_{tool}", action_type=tool, **params)
            result = node.execute(context)

            success = result.get("success", False)
            error = result.get("error")

            if success:
                summary = ""
                for key in ("content", "stdout", "output", "files"):
                    if key in result and result[key]:
                        val = result[key]
                        if isinstance(val, list):
                            summary = "\n".join(str(v) for v in val[:30])
                        else:
                            summary = str(val)[:2000]
                        break
                if not summary:
                    summary = "执行成功"

                if tracker:
                    tracker.record(tool, params, True, summary[:200])
                self.callback.on_observe(summary)
                return summary
            else:
                error_detail = f"执行失败: {error or result}"
                if tracker:
                    tracker.record(tool, params, False, error_detail, error=str(error))
                self.callback.on_observe(error_detail)
                return error_detail

        except Exception as e:
            error_msg = f"执行异常: {e}"
            if tracker:
                tracker.record(tool, params, False, error_msg, error=str(e))
            self.callback.on_observe(error_msg)
            return error_msg

    def _execute_step_with_llm(
        self, step_id: int, total: int, task: str, prev_results: str, original: str,
        tracker: ToolExecutionTracker | None = None,
    ) -> str:
        """使用 LLM 执行不需要工具的步骤。会验证文件操作声明。"""
        prompt = EXECUTE_PROMPT.format(
            step_id=step_id, total_steps=total,
            step_task=task, previous_results=prev_results,
        )
        messages = [
            {"role": "system", "content": f"原始任务: {original}"},
            {"role": "user", "content": prompt},
        ]
        result = self._call_llm(messages)

        # ── 验证 LLM 是否声明了文件操作但实际未执行 ──
        result = self._verify_llm_file_claims(result, tracker)
        return result

    @staticmethod
    def _verify_llm_file_claims(
        llm_output: str, tracker: ToolExecutionTracker | None = None,
    ) -> str:
        """检查 LLM 输出中是否声称创建/写入了文件，但实际未通过工具执行。

        如果检测到未验证的文件声明，追加警告信息。
        """
        import re

        # 检测文件操作声明的关键词
        claim_patterns = [
            r"(?:已|已经|成功)?(?:创建|新建|生成|写入|保存)(?:了)?",
            r"(?:created|written|saved|generated|initialized|made)",
            r"(?:文件|目录|文件夹)(?:已|已经)",
        ]

        has_claim = any(re.search(p, llm_output, re.IGNORECASE) for p in claim_patterns)
        if not has_claim:
            return llm_output

        # 提取提到的文件路径
        file_patterns = [
            r'[\w/\\.-]+\.(?:py|js|ts|html|css|json|yaml|yml|toml|md|txt|sh|bat|ps1|go|rs|java|c|cpp|h)',
            r'(?:src|lib|app|test|tests|dist|build|bin|config|docs)[/\\][\w/\\.-]+',
        ]
        mentioned_files = set()
        for pattern in file_patterns:
            mentioned_files.update(re.findall(pattern, llm_output))

        if not mentioned_files:
            return llm_output

        # 检查哪些文件真的通过工具创建了
        verified_files = set()
        if tracker:
            for call in tracker.calls:
                if call.success and call.tool_name in ("write_file", "create_directory"):
                    fp = call.params.get("file_path", "")
                    if fp:
                        verified_files.add(fp)

        # 对每个提到的文件，验证是否真的存在或被工具创建
        unverified = []
        for f in mentioned_files:
            if f in verified_files:
                continue
            from pathlib import Path
            if not Path(f).exists():
                unverified.append(f)

        if unverified:
            warning = (
                f"\n\n⚠️ **注意**: 以上内容中提到了创建文件 "
                f"`{'`, `'.join(unverified)}`，"
                f"但这些文件未经工具验证，可能并未实际创建。"
                f"如需真正创建文件，请使用 write_file 工具。"
            )
            return llm_output + warning

        return llm_output

    def _summarize(
        self, original: str, analysis: str, results: list[dict],
        tracker: ToolExecutionTracker | None = None,
    ) -> str:
        """汇总所有步骤的结果。"""
        results_text = "\n".join(
            f"步骤 {r['step_id']} ({r['task']}): {r['result'][:300]}"
            for r in results
        )

        # 构建工具执行摘要
        tool_summary = ""
        if tracker and tracker.has_executions():
            tool_summary = f"\n\n工具执行记录:\n{tracker.detail_log()}"

        messages = [
            {"role": "system", "content": (
                "请根据以下执行结果，给出简洁的最终总结。"
                "如果某些步骤声称创建了文件但没有对应的工具执行记录，"
                "请在总结中明确指出这些文件可能并未实际创建。"
            )},
            {"role": "user", "content": (
                f"原始任务: {original}\n\n分析: {analysis}\n\n"
                f"执行结果:\n{results_text}{tool_summary}"
            )},
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
