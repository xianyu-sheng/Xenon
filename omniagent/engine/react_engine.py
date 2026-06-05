"""
ReAct Engine — 思考-行动-观察循环引擎。

ReAct 模式: Think → Act → Observe → 循环直到完成
- Think: LLM 分析当前状态，决定下一步行动
- Act: 执行工具（ToolNode）
- Observe: 将工具结果反馈给 LLM
"""

from __future__ import annotations

import logging
from typing import Any

from omniagent.engine.callbacks import EngineCallback
from omniagent.engine.context import AgentContext
from omniagent.engine.tool_tracker import ToolExecutionTracker
from omniagent.nodes.tool_node import ToolNode
from omniagent.utils.llm_client import chat_completion
from omniagent.utils.response_adapter import parse_react

logger = logging.getLogger(__name__)

# ReAct 系统提示
REACT_SYSTEM_PROMPT = """你是一个 ReAct 模式的 AI 编程助手。你通过 **思考-行动-观察** 的循环来解决问题。
你必须实际执行操作，不能仅在文字中描述。

## 输出格式

每次回复 **只输出一个 JSON 对象**（不要输出其他任何内容）：

调用工具时：
```json
{{"thought": "分析当前状态，决定下一步", "action": "工具名", "action_input": {{"参数名": "值"}}}}
```

任务完成时：
```json
{{"thought": "总结执行结果", "final_answer": "给用户的最终回答"}}
```

## 示例

用户: 创建一个 hello.py 文件，打印 Hello World
助手: {{"thought": "用户需要创建一个 Python 文件", "action": "write_file", "action_input": {{"file_path": "hello.py", "content": "print('Hello World')"}}}}

用户: 查看当前目录有哪些文件
助手: {{"thought": "需要列出当前目录的文件", "action": "list_files", "action_input": {{"file_path": "."}}}}

用户: 帮我写一个 hello.py（假设上一步已成功创建）
助手: {{"thought": "文件已在上一步创建成功，任务完成", "final_answer": "已创建 hello.py，内容为 print('Hello World')"}}

## 工具调用规则

1. **参数名必须使用标准名称**（见下方工具列表），不要用别名
2. **一个 JSON 只调用一个工具**，不要同时调用多个
3. **工具失败时**：分析错误原因，调整参数后重试，或换一种方法
4. **不要编造结果**：如果不确定文件是否创建成功，用 read_file 验证
5. **何时使用 final_answer**：只有当所有操作都通过工具实际执行完毕后，才能使用 final_answer
6. **严禁发明工具**：只能使用下方列出的工具，不存在 get_content_from_url、get_github_repo_content 等工具

## 可用工具（完整且唯一，不存在其他工具）

{tools_desc}

## 分析 GitHub 项目的标准流程

当用户要求分析 GitHub 仓库时，必须按以下顺序执行：
1. 用 github_fetch(repo="owner/repo", github_action="list_files") 列出所有文件
2. 用 github_fetch(repo="owner/repo", github_action="fetch_readme") 获取 README
3. 用 github_fetch(repo="owner/repo", github_action="fetch_file", github_path="xxx.py") 逐个获取关键源码
4. 基于实际获取的代码进行分析（不要凭空猜测）
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
    "create_directory": {
        "name": "create_directory",
        "description": "创建目录（含父目录）",
        "params": {"file_path": "目录路径"},
    },
    "batch_write": {
        "name": "batch_write",
        "description": "批量写入多个文件（原子性，适合创建多文件项目）",
        "params": {"files": "[{\"path\": \"a.py\", \"content\": \"...\"}, ...]"},
    },
    "batch_edit": {
        "name": "batch_edit",
        "description": "批量编辑多个文件（每个编辑独立验证）",
        "params": {"edits": "[{\"file_path\": \"a.py\", \"old_text\": \"...\", \"new_text\": \"...\"}, ...]"},
    },
    "code_index": {
        "name": "code_index",
        "description": "搜索项目中的代码符号（函数、类、变量），基于 AST 索引",
        "params": {"search_pattern": "要搜索的符号名或关键词", "file_path": "索引目录（可选，默认当前目录）"},
    },
    "ast_analyze": {
        "name": "ast_analyze",
        "description": "分析 Python 文件的代码结构（函数签名、类、复杂度、未使用导入）",
        "params": {"file_path": "Python 文件路径"},
    },
    "refactor": {
        "name": "refactor",
        "description": "代码重构：跨文件重命名符号、清理未使用导入、分析重构建议",
        "params": {"refactor_action": "rename|clean_imports|analyze", "old_name": "旧名（rename用）", "new_name": "新名（rename用）", "file_path": "文件路径（clean_imports/analyze用）"},
    },
    "diff_preview": {
        "name": "diff_preview",
        "description": "预览文件修改的 diff（不实际修改），用于确认后再操作",
        "params": {"file_path": "文件路径", "old_text": "原文本（编辑模式）", "new_text": "新文本"},
    },
    "mcp_call": {
        "name": "mcp_call",
        "description": "调用外部 MCP 服务器工具（需先通过 /mcp add 添加服务器）",
        "params": {"tool_name": "MCP工具名（格式: server:tool 或 tool）", "tool_args": "{参数字典}"},
    },
    "github_fetch": {
        "name": "github_fetch",
        "description": "GitHub 仓库操作：列出文件(list_files)、获取文件内容(fetch_file)、获取README(fetch_readme)",
        "params": {"repo": "owner/repo 格式", "github_action": "list_files|fetch_file|fetch_readme", "github_path": "文件路径(fetch_file用)", "branch": "分支名(默认main)"},
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
        callback: EngineCallback | None = None,
    ) -> None:
        self.model_priority = model_priority
        self.max_iterations = max_iterations
        self.tools = tools or BUILTIN_TOOLS
        self.system_prompt = system_prompt or self._build_system_prompt()
        self.callback = callback or EngineCallback()

    def _build_system_prompt(self) -> str:
        import sys
        tools_desc = "\n".join(
            f"- {t['name']}: {t['description']} (参数: {t['params']})"
            for t in self.tools.values()
        )

        # 检测操作系统
        if sys.platform == "win32":
            os_info = "Windows（使用 PowerShell 命令，不要使用 bash/Linux 命令如 ls, cat, mkdir -p, uname, which 等）"
            shell_info = "PowerShell（命令用 ; 分隔，不要用 &&）"
        elif sys.platform == "darwin":
            os_info = "macOS（使用 bash 命令）"
            shell_info = "bash/zsh"
        else:
            os_info = "Linux（使用 bash 命令）"
            shell_info = "bash"

        env_info = f"""

## 运行环境

- 操作系统: {os_info}
- Shell: {shell_info}
- Python: {sys.version.split()[0]}
- 工作目录: 通过命令 `pwd`（Linux/macOS）或 `Get-Location`（Windows）获取

重要：根据操作系统使用正确的命令。Windows 下不要使用 ls, cat, mkdir -p, uname, which, grep 等 Linux 命令。
"""
        return REACT_SYSTEM_PROMPT.format(tools_desc=tools_desc) + env_info

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
        tracker = ToolExecutionTracker()
        messages = [{"role": "system", "content": self.system_prompt}]
        # 注入对话历史（最近 10 条，排除 system 消息）
        history = ctx.get_conversation_messages()
        if history:
            recent = [m for m in history if m.get("role") != "system"][-10:]
            messages.extend(recent)
            logger.info(f"ReAct 注入 {len(recent)} 条对话历史")
        else:
            logger.warning("ReAct: 无对话历史可注入！")
        messages.append({"role": "user", "content": user_input})

        # 判断输入是否需要工具操作
        requires_tools = self._input_requires_tools(user_input)
        no_tool_streak = 0  # 连续未执行工具的轮次

        for i in range(self.max_iterations):
            logger.info(f"ReAct 迭代 {i + 1}/{self.max_iterations}")

            # 调用 LLM
            response = self._call_llm(messages)
            messages.append({"role": "assistant", "content": response})

            # 解析 LLM 输出
            parsed = self._parse_response(response)

            thought = parsed.get("thought", "")
            if thought:
                self.callback.on_think(thought)

            if parsed.get("final_answer"):
                # ── 关键验证：如果需要工具但未执行，拒绝接受 final_answer ──
                if requires_tools and not tracker.has_executions():
                    no_tool_streak += 1
                    if no_tool_streak <= 2:
                        force_msg = (
                            "⚠️ 你还没有使用任何工具就声称完成了任务。"
                            "请使用工具（如 write_file、command、create_directory 等）"
                            "实际执行操作，而不是仅在文字中描述。"
                            "如果你确实不需要工具，请在 final_answer 中明确说明原因。"
                        )
                        messages.append({"role": "user", "content": force_msg})
                        self.callback.on_warning("LLM 未执行工具就声称完成，要求重试")
                        logger.warning(f"ReAct: LLM 未执行工具就声称完成，强制要求工具调用 (第 {no_tool_streak} 次)")
                        continue
                    else:
                        # 连续 3 次拒绝工具，附带警告返回
                        answer = parsed["final_answer"]
                        warning = (
                            "\n\n⚠️ **警告**: 本次回答未经工具执行验证。"
                            "LLM 声称完成了任务但未实际调用任何工具，"
                            "文件操作可能未真正执行。"
                        )
                        self.callback.on_warning("LLM 连续拒绝工具调用，附带警告返回")
                        logger.warning("ReAct: LLM 连续拒绝工具调用，附带警告返回")
                        self.callback.on_finish(answer + warning)
                        return answer + warning

                logger.info(f"ReAct 完成，共 {i + 1} 次迭代，工具调用 {len(tracker.calls)} 次")
                answer = parsed["final_answer"]
                if tracker.has_executions():
                    summary = tracker.execution_summary()
                    logger.info(f"ReAct 工具执行摘要: {summary}")
                self.callback.on_finish(answer)
                return answer

            if "action" in parsed:
                # 执行工具
                action = parsed["action"]
                action_input = parsed.get("action_input", {})

                logger.info(f"ReAct 思考: {thought}")
                logger.info(f"ReAct 行动: {action}({action_input})")
                self.callback.on_act(action, action_input)

                observation = self._execute_tool(action, action_input, ctx, tracker)
                self.callback.on_observe(observation)

                # 将观察结果加入对话
                obs_msg = f"Observation: {observation}"
                messages.append({"role": "user", "content": obs_msg})
                logger.info(f"ReAct 观察: {observation[:200]}")
                no_tool_streak = 0
            else:
                # LLM 没有给出有效输出，直接返回
                result = parsed.get("thought", response)
                self.callback.on_finish(result)
                return result

        msg = f"达到最大迭代次数 ({self.max_iterations})，未能得出最终答案。"
        self.callback.on_warning(msg)
        self.callback.on_finish(msg)
        return msg

    def _call_llm(self, messages: list[dict[str, str]], max_tokens: int = 131072) -> str:
        """调用 LLM，支持多模型 fallback。"""
        last_error = None
        for model_id in self.model_priority:
            try:
                return chat_completion(model_id, messages, max_tokens=max_tokens, temperature=0.3)
            except Exception as e:
                last_error = e
                logger.warning(f"模型 {model_id} 失败: {e}，尝试下一个...")
        raise RuntimeError(f"所有模型均调用失败: {last_error}")

    def _parse_response(self, response: str) -> dict[str, Any]:
        """解析 LLM 的 JSON 输出（委托给 response_adapter 中间件）。"""
        return parse_react(response)

    def _execute_tool(
        self,
        action: str,
        action_input: dict,
        context: AgentContext,
        tracker: ToolExecutionTracker | None = None,
    ) -> str:
        """执行工具并返回结果。"""
        tool_info = self.tools.get(action)
        if not tool_info:
            error_msg = f"错误: 未知工具 '{action}'，可用工具: {list(self.tools.keys())}"
            if tracker:
                tracker.record(action, action_input, False, error_msg, error=error_msg)
            return error_msg

        try:
            action_input = ToolNode.normalize_params(action_input)
            logger.info(f"执行工具: {action}, 参数: {action_input}")
            node = ToolNode(
                f"react_{action}",
                action_type=action,
                **action_input,
            )
            result = node.execute(context)
            logger.info(f"工具结果: {str(result)[:200]}")

            success = result.get("success", False)
            error = result.get("error")

            if success:
                # 提取主要内容
                summary = ""
                for key in ("content", "stdout", "output", "files"):
                    if key in result and result[key]:
                        val = result[key]
                        if isinstance(val, list):
                            summary = "\n".join(str(v) for v in val[:50])
                        else:
                            summary = str(val)[:3000]
                        break
                if not summary:
                    summary = str(result)[:3000]

                if tracker:
                    tracker.record(action, action_input, True, summary[:200])
                return summary
            else:
                error_detail = f"工具执行失败: {error or result}"
                if tracker:
                    tracker.record(action, action_input, False, error_detail, error=str(error))
                return error_detail

        except Exception as e:
            error_msg = f"工具执行异常: {e}"
            logger.error(f"工具执行异常: {action}({action_input}) -> {e}")
            if tracker:
                tracker.record(action, action_input, False, error_msg, error=str(e))
            return error_msg

    @staticmethod
    def _input_requires_tools(text: str) -> bool:
        """判断用户输入是否大概率需要工具执行。

        与 repl.py 的 _detect_tool_need 类似，但更宽松 —
        宁可误判需要工具（多一次确认），也不漏判。
        """
        tool_keywords = [
            # 文件操作
            "文件", "文件夹", "目录", "创建", "写入", "保存", "新建", "生成",
            "读取", "查看", "修改", "编辑", "删除", "替换",
            "写", "建", "做", "搭",
            "file", "folder", "directory", "create", "write", "save",
            "read", "edit", "delete", "modify", "replace", "make", "build",
            # 命令执行
            "执行", "运行", "命令", "脚本", "程序",
            "run", "execute", "command", "script",
            # Git
            "git", "commit", "push", "pull", "clone",
            # 搜索
            "搜索", "查找", "grep", "find", "search",
            # 路径模式
            ".py", ".js", ".ts", ".html", ".css", ".json", ".yaml",
            ".md", ".txt", ".sh", ".bat",
            "src/", "test", "lib/", "app/",
        ]
        text_lower = text.lower()
        return any(kw in text_lower for kw in tool_keywords)
