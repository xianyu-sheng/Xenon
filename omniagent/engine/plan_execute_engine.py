"""
Plan-Execute Engine — 规划-执行两阶段引擎。

Phase 1: Planning — LLM 生成步骤列表
Phase 2: Execution — 逐步执行，每步结果写入 context
"""

from __future__ import annotations

import logging
import re
from typing import Any

from omniagent.engine.callbacks import EngineCallback
from omniagent.engine.context import AgentContext
from omniagent.engine.react_engine import _check_hollow_answer
from omniagent.engine.tool_tracker import ToolExecutionTracker
from omniagent.nodes.tool_node import ToolNode
from omniagent.utils.llm_client import chat_completion
from omniagent.utils.response_adapter import parse_plan, parse_react

logger = logging.getLogger(__name__)

PLAN_SYSTEM_PROMPT = """你是一个任务规划专家。将用户任务分解为可执行的原子步骤。

## 🔴 核心原则

1. **先探查再规划** — 如果任务涉及已有文件或项目，第一步必须是 list_files 了解现有结构
2. **路径必须是实际的本地路径** — file_path 必须是具体的文件系统路径，如 `D:/project/app.py` 或 `src/main.py`。**绝对禁止**将自然语言描述作为路径值（如"基于步骤1的输出"、"来自上一步的文件列表"等）
3. **路径来自 list_files** — 任何 read_file/write_file/edit_file 的 file_path 必须直接来自 list_files 返回的真实文件名
4. **读取文件用 read_file，不要用 command** — read_file 是读取文件内容的专用工具，command 用于执行脚本/安装依赖，不要用 `Get-Content` 或 `cat` 等命令替代 read_file
5. **参数名必须使用标准名称** — file_path（不是 path）、action（不是 command）、content（不是 text）
6. **tool 字段必须是下方列表中的精确工具名** — 严禁发明或猜测工具名
7. **不需要工具的步骤** — tool 设为 null，如"汇总分析结果"、"输出最终结论"
8. **每步只做一个原子操作** — 不要把"创建5个文件"写成一个步骤，拆成5个步骤；不要把"读取所有文件"写成一个步骤，每个关键文件单独一个 read_file 步骤

## 输出格式

只输出一个 JSON，不要输出其他任何内容：
```json
{{"analysis":"简要分析任务目标和策略","steps":[{{"id":1,"task":"步骤描述","tool":"工具名或null","params":{{"参数名":"值"}},"depends_on":[]}}]}}
```

### 步骤依赖标注 (depends_on)

每个步骤必须标注 `depends_on` 字段（步骤 ID 的整数数组）：
- **depends_on: []** — 第一步（list_files），或不需要其他步骤输出的独立操作
- **depends_on: [1]** — 需要步骤 1 的输出（如 list_files 获取的文件列表）才能执行
- **depends_on: [2, 3]** — 需要步骤 2 和步骤 3 **都完成**后才能执行（汇总步骤）

标注原则：
1. **读取文件之前必须先 list_files** → 所有 read_file 步骤的 depends_on 必须包含 list_files 的步骤 ID
2. **汇总/分析步骤依赖所有数据收集步骤** → 汇总步骤的 depends_on 包含所有 read_file/search_files 的步骤 ID
3. **读取多个独立文件时，它们都只依赖 list_files** → 这些 read_file 步骤可以并行执行（系统自动处理）
4. **有依赖链条时**（如：先读 A → 基于 A 的输出编辑 B）→ 编辑 B 的 depends_on 包含读 A 的步骤 ID

## ⚠️ 可用工具列表（完整且唯一）

以下是所有可用工具，不存在其他工具。tool 字段必须是下列之一或 null：

- command: {{"action": "终端命令"}} — 在本机终端执行 shell 命令（Windows 用 PowerShell）。用于 git clone、安装依赖、运行脚本等。
- read_file: {{"file_path": "路径", "start_line": "起始行号(可选,从1开始)", "max_lines": "读取行数(可选)"}} — 读取本机文件内容。⚠️ 路径必须来自 list_files 的实际输出。
- write_file: {{"file_path": "路径", "content": "内容"}} — 将内容写入本机文件（覆盖）。自动创建父目录。
- list_files: {{"file_path": "目录", "pattern": "*.py"}} — 列出本机目录文件。⚠️ 读取任何文件前必须先执行此步骤。
- search_files: {{"file_path": "目录", "search_pattern": "关键词"}} — 在本机文件中搜索关键词（类似 grep）。
- git: {{"git_command": "status|diff|log|add|commit"}} — 本机 Git 操作。
- web_fetch: {{"url": "完整URL"}} — HTTP/HTTPS GET 抓取任意 URL 内容（HTTP 自动升级为 HTTPS）。用于获取网页、API 数据等。
- github_fetch: {{"repo": "owner/repo", "github_action": "list_files|fetch_file|fetch_readme", "github_path": "文件路径(fetch_file用)", "branch": "main"}} — GitHub 仓库专用操作。仅支持公开仓库。
- datetime: {{}} — 获取当前本地日期、时间和星期几（直接读取系统时钟，无需网络）。
- weather: {{"city": "城市名（中文如\"重庆\"或英文如\"Chongqing\"）", "lang": "zh或en（可选，默认zh）"}} — 查询指定城市的实时天气，包含温度、湿度、穿衣建议等。
- edit_file: {{"file_path": "路径", "old_text": "原文（必须精确匹配）", "new_text": "新文"}} — 精确查找替换编辑本机文件。
- create_directory: {{"file_path": "目录路径"}} — 创建目录（自动递归创建父目录）。
- file_move: {{"source": "源文件路径", "destination": "目标路径"}} — 移动文件或文件夹到新位置。
- file_copy: {{"source": "源文件路径", "destination": "目标路径"}} — 复制文件到新位置。
- batch_write: {{"files": [{{"path": "a.py", "content": "..."}}, ...]}} — 原子性批量写入多个文件。
- batch_edit: {{"edits": [{{"file_path": "a.py", "old_text": "...", "new_text": "..."}}, ...]}} — 批量编辑多个文件。
- code_index: {{"search_pattern": "符号名", "file_path": "目录"}} — 基于 AST 搜索 Python 代码符号。
- ast_analyze: {{"file_path": "Python文件"}} — AST 深度分析 Python 文件。
- refactor: {{"refactor_action": "rename|clean_imports|analyze", "old_name": "旧名", "new_name": "新名", "file_path": "路径"}} — 代码重构。
- diff_preview: {{"file_path": "路径", "old_text": "原文", "new_text": "新文"}} — 预览修改 diff（不实际改文件）。
- mcp_call: {{"tool_name": "server:tool", "tool_args": {{}}}} — 调用 MCP 外部工具服务器。

## 🔴 文件路径铁律

1. **如果用户输入包含"项目的真实文件列表"**：所有 read_file 的 file_path 必须来自该列表，不得编造
2. **如果用户输入包含"未获取到文件列表"的警告**：第一步必须规划 list_files，不要猜测任何文件名
3. **如果当前消息没有目录路径且未提供文件列表**（如对话跟进"更详细一些"）：你的步骤可能不需要工具（tool 设为 null），或者第一步 list_files

## 分析代码仓库的标准规划

### ✅ 正确示例（基于真实文件列表）

假设用户输入包含：
```
根目录文件: D:/myproject/app.py, D:/myproject/requirements.txt, D:/myproject/src/main.py
```

则规划如下（使用真实路径，标注依赖关系）：
```json
{"analysis":"分析项目结构和代码质量","steps":[
  {"id":1,"task":"列出项目根目录文件","tool":"list_files","params":{"file_path":"D:/myproject"},"depends_on":[]},
  {"id":2,"task":"读取 README.md 了解项目","tool":"read_file","params":{"file_path":"D:/myproject/README.md"},"depends_on":[1]},
  {"id":3,"task":"读取依赖配置 requirements.txt","tool":"read_file","params":{"file_path":"D:/myproject/requirements.txt"},"depends_on":[1]},
  {"id":4,"task":"读取入口文件 app.py","tool":"read_file","params":{"file_path":"D:/myproject/app.py"},"depends_on":[1]},
  {"id":5,"task":"读取核心模块 src/main.py","tool":"read_file","params":{"file_path":"D:/myproject/src/main.py"},"depends_on":[1]},
  {"id":6,"task":"基于实际代码汇总分析结果","tool":null,"params":{},"depends_on":[2,3,4,5]}
]}
```
注：步骤 2/3/4/5 都只依赖步骤 1（list_files），因此**可以并行执行**。步骤 6 依赖步骤 2-5 全部完成。

### ❌ 绝对禁止
- 编造不在文件列表中的文件名
- 使用自然语言描述代替实际路径
- 规划少于 5 个步骤（分析任务必须充分探索）

## 运行环境

- Windows 使用 PowerShell 命令（如 Get-ChildItem、Move-Item、Copy-Item），不要用 Linux 命令（ls、cat、mkdir -p）
- 执行命令前注意当前工作目录
"""

EXECUTE_PROMPT = """你正在执行一个任务计划的第 {step_id} 步（共 {total_steps} 步）。

当前步骤: {step_task}

之前步骤的结果（含 list_files 输出的真实文件列表）:
{previous_results}

请完成这个步骤。仔细查看上面"之前步骤的结果"中的文件列表，从中选出实际存在的文件路径。

规则：
- 如果需要读取文件 → 输出 action + action_input，file_path 必须从上方的文件列表中复制
- 如果文件列表中找不到对应的文件 → 输出 result 说明"该文件不存在，跳过"
- 如果不需要工具 → 直接输出 result
- 绝对禁止编造不在文件列表中的路径

输出格式（只输出一个 JSON）：
- 需要工具: {{"thought": "...", "action": "工具名", "action_input": {{"参数": "值"}}}}
- 不需要工具: {{"thought": "...", "result": "你的分析/总结内容"}}
"""


class PlanExecuteEngine:
    """规划-执行两阶段引擎。"""

    def __init__(
        self,
        model_priority: list[str],
        *,
        executor_model_priority: list[str] | None = None,
        max_steps: int = 20,
        system_prompt: str | None = None,
        callback: EngineCallback | None = None,
    ) -> None:
        self.model_priority = model_priority  # planner 角色
        self.executor_model_priority = executor_model_priority or model_priority  # executor 角色，默认回退到 planner
        self.max_steps = max_steps
        self.system_prompt = system_prompt or PLAN_SYSTEM_PROMPT
        self.callback = callback or EngineCallback()

    def run(self, user_input: str, context: AgentContext | None = None) -> str:
        """
        执行 Plan-Execute 流程。

        对于探索类任务，先静默侦察目录结构，再生成计划。
        """
        ctx = context or AgentContext()
        tracker = ToolExecutionTracker()

        # Phase 0: Scout — 如果任务涉及本地目录，先 list_files 获取真实文件列表
        scout_info = self._scout(user_input, ctx, tracker)
        # Scout fallback: 如果当前消息没包含目录，从对话历史中提取
        if not scout_info:
            scout_info = self._scout_from_history(user_input, ctx, tracker)

        plan_input = user_input
        if scout_info:
            plan_input = f"{user_input}\n\n## 🔴 项目的真实文件列表（来自 list_files，请基于此规划）\n```\n{scout_info}\n```\n请使用上述真实文件路径来规划 read_file 步骤。"
        else:
            # 没有 scout 数据时，在 prompt 中强制要求先 list_files
            plan_input = (
                f"{user_input}\n\n"
                "## 🔴 重要：当前消息中没有指定目录路径，且未获取到文件列表。\n"
                "如果你的任务需要访问本地文件，规划的**第一步必须是 list_files**。\n"
                "**绝对禁止**猜测不存在的文件名或目录名——只能使用 list_files 实际返回的文件路径。\n"
                "如果你不需要访问文件（如纯对话/解释/展开已有分析），所有步骤的 tool 设为 null。"
            )

        # Phase 1: Planning（现在有真实文件列表）
        logger.debug("Plan-Execute Phase 1: 规划中...")
        plan = self._plan(plan_input, ctx)
        steps = plan.get("steps", [])

        if not steps:
            self.callback.on_warning("未能生成有效的执行计划")
            return plan.get("analysis", "未能生成有效的执行计划。")

        logger.debug(f"计划生成 {len(steps)} 个步骤")
        total = min(len(steps), self.max_steps)

        # Phase 2: Execution（支持 DAG 并行）
        logger.debug("Plan-Execute Phase 2: 执行中...")

        has_deps = any(
            isinstance(s, dict) and s.get("depends_on") and len(s.get("depends_on", [])) > 0
            for s in steps
        )

        if has_deps:
            # ── DAG 并行路径 ──
            try:
                from omniagent.engine.plan_dag import PlanDAG
                from omniagent.repl.cards import PlanProgressCard
                import asyncio

                dag = PlanDAG.from_plan(plan)
                errors = dag.validate()
                if errors:
                    logger.warning("Plan DAG 验证失败: %s", errors)

                analysis_text = plan.get("analysis", "")
                logger.info(
                    "PlanExecute DAG: %d steps, %d waves, has_parallelism=%s",
                    dag.total_steps, dag.wave_count, dag.has_parallelism,
                )

                progress_card = PlanProgressCard(
                    [s for s in dag.steps.values()], title="执行计划"
                )

                from rich.live import Live
                from rich.console import Console
                dag_console = Console()
                with Live(progress_card, console=dag_console, refresh_per_second=8, transient=False):
                    summary = asyncio.run(
                        self._execute_dag_waves(
                            dag, user_input, analysis_text, ctx, tracker, total,
                        )
                    )
                return summary
            except Exception as e:
                logger.warning("PlanExecute DAG 失败: %s，回退串行", e)
                # 回退

        # ── 串行路径（回退或默认）──
        results = []

        for i, step in enumerate(steps[:self.max_steps]):
            step_id = step.get("id", i + 1)
            step_task = step.get("task", "")
            tool = step.get("tool")
            params = step.get("params", {})

            logger.debug(f"执行步骤 {step_id}: {step_task}")
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
                # 使用 LLM 执行 — 支持 mini ReAct 循环
                result = self._execute_step_with_llm(
                    step_id, len(steps), step_task, prev_results, user_input, tracker, ctx
                )

            results.append({
                "step_id": step_id,
                "task": step_task,
                "result": result,
            })

            ctx.set(f"step_{step_id}_result", result)
            success = not result.startswith(("执行失败", "执行异常"))
            self.callback.on_step_done(step_id, success, result[:200])
            logger.debug(f"步骤 {step_id} 完成: {result[:100]}")

        # 汇总结果 — 附加工具执行摘要
        return self._summarize(user_input, plan.get("analysis", ""), results, tracker)

    def _scout(
        self, user_input: str, context: AgentContext,
        tracker: ToolExecutionTracker,
    ) -> str | None:
        """Phase 0: 静默侦察 — 委托给统一的 DirectoryScout 服务。"""
        from omniagent.engine.directory_scout import DirectoryScout

        scout = DirectoryScout()
        result = scout.scout(user_input, context, tracker)
        if result.has_data:
            return result.to_plan_context()
        return None

    def _scout_from_history(
        self, user_input: str, context: AgentContext,
        tracker: ToolExecutionTracker,
    ) -> str | None:
        """Scout 回退：委托给统一的 DirectoryScout 服务。"""
        from omniagent.engine.directory_scout import DirectoryScout

        scout = DirectoryScout()
        result = scout.scout_from_history(user_input, context, tracker)
        if result and result.has_data:
            return result.to_plan_context()
        return None

    def _plan(self, user_input: str, context: AgentContext | None = None) -> dict[str, Any]:
        """Phase 1: 生成执行计划。"""
        messages = [{"role": "system", "content": self.system_prompt}]
        # 注入对话历史（最近 10 条，包括 system 消息以保留 prompt_optimizer 的 system_hint）
        if context:
            history = context.get_conversation_messages()
            if history:
                # 取最近的非 system 消息 + 最近的 system 消息（含 system_hint）
                non_system = [m for m in history if m.get("role") != "system"][-6:]
                system_msgs = [m for m in history if m.get("role") == "system"][-2:]
                recent = system_msgs + non_system
                messages.extend(recent)
                logger.debug(f"Plan 注入 {len(recent)} 条对话历史 (含 {len(system_msgs)} 条 system)")
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
            logger.debug(f"LLM 原始响应 (前500字): {response[:500]}")
        result = self._parse_json(response)
        logger.debug(f"解析后: steps={len(result.get('steps', []))}, analysis={result.get('analysis', '')[:100]}")
        return result

    def _execute_step_with_tool(
        self, tool: str, params: dict, context: AgentContext,
        tracker: ToolExecutionTracker | None = None,
    ) -> str:
        """使用工具执行步骤。委托给统一的 ToolExecutor。"""
        from omniagent.engine.tool_executor import ToolExecutor

        executor = ToolExecutor()
        result = executor.execute(tool, params, context, tracker)

        if result.success:
            notify_text = result.format_notification()
            self.callback.on_observe(notify_text, card_data=result.to_card_data())
            return result.summary

        self.callback.on_observe(result.error or result.summary, card_data=result.to_card_data())
        return result.error or result.summary

    def _execute_step_with_llm(
        self, step_id: int, total: int, task: str, prev_results: str, original: str,
        tracker: ToolExecutionTracker | None = None,
        context: AgentContext | None = None,
    ) -> str:
        """使用 LLM 执行不需要工具的步骤。支持 mini ReAct 循环（最多 3 次工具调用）。"""
        prompt = EXECUTE_PROMPT.format(
            step_id=step_id, total_steps=total,
            step_task=task, previous_results=prev_results,
        )
        messages: list[dict[str, str]] = [
            {"role": "system", "content": f"原始任务: {original}"},
            {"role": "user", "content": prompt},
        ]

        # ── mini ReAct 循环（最多 3 次工具调用），使用 executor 模型 ──
        for _ in range(3):
            response = self._call_llm(messages, model_priority=self.executor_model_priority)
            parsed = parse_react(response)  # 用 ReAct 解析器提取 action/result

            # 检查是否有 action（工具调用）
            action = parsed.get("action", "")
            if action and action.strip():
                action_input = parsed.get("action_input", {}) or {}
                # 验证参数（使用统一 ToolExecutor 的验证逻辑）
                from omniagent.engine.tool_executor import _validate_tool_params
                validated = _validate_tool_params(action, action_input)
                if not validated["valid"]:
                    messages.append({"role": "assistant", "content": response})
                    messages.append({"role": "user", "content": f"参数错误: {validated['reason']}。请修正。"})
                    continue

                # 执行工具
                ctx = context or AgentContext()
                tool_result = self._execute_step_with_tool(action, action_input, ctx, tracker)
                messages.append({"role": "assistant", "content": response[:500]})
                messages.append({"role": "user", "content": f"工具 '{action}' 执行结果:\n{tool_result[:2000]}\n\n请继续完成当前步骤，或输出 {{\"result\": \"...\"}}。"})
                continue

            # 检查是否有 result/final_answer
            result_text = parsed.get("result", "") or parsed.get("final_answer", "")
            if result_text and result_text.strip():
                return self._verify_llm_file_claims(result_text, tracker)

            # 纯文本响应 → 直接返回
            return self._verify_llm_file_claims(response, tracker)

        # 耗尽 mini ReAct 循环 → 返回最后的响应
        last_content = messages[-1].get("content", "") if messages else ""
        return self._verify_llm_file_claims(str(last_content), tracker)

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

    async def _execute_dag_waves(
        self,
        dag: Any,  # PlanDAG
        user_input: str,
        analysis: str,
        context: AgentContext,
        tracker: ToolExecutionTracker | None,
        total: int,
    ) -> str:
        """按 DAG 波次异步执行步骤（PlanExecute 风格）。

        每波内步骤通过 asyncio.to_thread 并行执行，
        波间串行（保持依赖顺序）。
        """
        import asyncio
        results: list[dict[str, Any]] = []

        for wave in dag.waves():
            # 构建前序结果上下文
            prev_results = "\n".join(
                f"步骤 {r['step_id']}: {r['result'][:200]}"
                for r in results[-3:]
            ) if results else "(无)"

            # 并行执行波内步骤
            async def _run_step(step):
                sid = step.id
                task = step.task
                self.callback.on_step(sid, total, task)
                logger.debug(f"执行步骤 {sid}: {task}")

                if step.is_tool_step:
                    result = await asyncio.to_thread(
                        self._execute_step_with_tool,
                        step.tool, step.params, context, tracker,
                    )
                else:
                    result = await asyncio.to_thread(
                        self._execute_step_with_llm,
                        sid, total, task, prev_results, user_input, tracker, context,
                    )

                success = not result.startswith(("执行失败", "执行异常"))
                self.callback.on_step_done(sid, success, result[:200])
                return {"step_id": sid, "task": task, "result": result}

            wave_results = await asyncio.gather(*[_run_step(s) for s in wave])
            results.extend(wave_results)

            for r in wave_results:
                context.set(f"step_{r['step_id']}_result", r["result"])

        return self._summarize(user_input, analysis, results, tracker)

    def _summarize(
        self, original: str, analysis: str, results: list[dict],
        tracker: ToolExecutionTracker | None = None,
    ) -> str:
        """汇总所有步骤的结果。包含空洞检测。"""
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
                "你是一个任务汇总专家。你的唯一职责是基于**下方提供的执行结果**撰写总结。\n\n"
                "## 🔴 铁律\n"
                "1. **只能使用执行结果中的数据** — 执行结果说文件不存在就是不存在，说文件存在就是存在\n"
                "2. **禁止使用你对项目结构的先验假设** — 不要因为没看到 `models/` 目录就说目录不存在；"
                "可能目录叫 `backend/app/models/` 或者项目用了不同的结构\n"
                "3. **如果执行结果显示读到了 flask/sqlalchemy/react 相关代码**，"
                "不要声称「无对应代码」——你看到了代码\n"
                "4. **不要推测** — 如果某个功能你没读到相关代码，说「未在已读取的文件中发现」，而不是「不存在」\n"
                "5. **用中文总结，简洁清晰**"
            )},
            {"role": "user", "content": (
                f"原始任务: {original}\n\n分析: {analysis}\n\n"
                f"执行结果:\n{results_text}{tool_summary}\n\n"
                "请基于以上执行结果，给出简洁的最终总结。"
                "记住：只基于执行结果，不要用你的先验知识推翻执行结果。"
            )},
        ]
        summary = self._call_llm(messages)

        # ── 空洞检测 ──
        hollow_check = _check_hollow_answer(summary, original, tracker)
        if hollow_check["is_hollow"]:
            logger.warning(f"PlanExecute: 汇总结果空洞 — {hollow_check['reason']}，追加警告")
            warning = (
                f"\n\n⚠️ **注意**: 以上总结可能不够完整（{hollow_check['reason']}）。"
                f"请查看各步骤的详细执行结果获取更完整的信息。"
            )
            return summary + warning

        return summary

    def _call_llm(
        self,
        messages: list[dict[str, str]],
        max_tokens: int = 131072,
        *,
        model_priority: list[str] | None = None,
    ) -> str:
        """调用 LLM，支持多模型 fallback 和按阶段切换模型。

        Args:
            messages: LLM 消息列表
            max_tokens: 最大输出 token
            model_priority: 覆盖默认模型列表（用于按阶段分派不同模型角色）
        """
        models = model_priority or self.model_priority
        last_error = None
        for model_id in models:
            try:
                return chat_completion(model_id, messages, max_tokens=max_tokens, temperature=0.3)
            except Exception as e:
                last_error = e
                logger.warning(f"模型 {model_id} 失败: {e}")
        msg = f"所有模型均调用失败: {last_error}"
        raise RuntimeError(msg)

    def _parse_json(self, text: str) -> dict[str, Any]:
        """从 LLM 输出中提取 JSON（委托给 response_adapter 中间件）。"""
        return parse_plan(text)
