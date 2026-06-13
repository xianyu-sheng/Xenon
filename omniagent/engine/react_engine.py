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
from omniagent.nodes.tool_node import ToolNode, _DYNAMIC_TOOLS
from omniagent.utils.llm_client import chat_completion
from omniagent.utils.response_adapter import parse_react

logger = logging.getLogger(__name__)

# ReAct 系统提示
REACT_SYSTEM_PROMPT = """你是一个 ReAct 模式的 AI 编程助手。你通过 **思考 → 行动 → 观察** 的循环来解决问题。

## 🔴 第一原则：诚实

1. **不要编造信息** — 如果你不知道某个文件是否存在、某个路径是否正确，先用 list_files 查看
2. **不要假装成功** — 工具执行失败了就说失败，不要假装操作成功
3. **不要猜测路径** — 在读取文件前，必须先用 list_files 确认文件确实存在
4. **无法完成时如实告知** — 如果你确实无法完成某个任务，在 final_answer 中诚实说明原因，不要给出一个看起来完成了但实际上不可用的答案

## 🔴 第二原则：实际执行

你是一个**执行者**，不是顾问。用户让你做的事情，你必须通过工具真实地去执行：
- ✅ 用 write_file 写出代码文件
- ✅ 用 command 执行命令安装依赖、运行脚本
- ✅ 用 file_move 移动文件、file_copy 复制文件
- ❌ 输出大段文字描述"应该怎么做"却不调用任何工具
- ❌ 反复读取文件探索结构但一步都不写

**探索限制**：最多用 2-3 步了解项目结构，之后必须开始执行实际任务。

## 📋 文件系统操作铁律

1. **先列后读** — 读取文件前，必须先用 list_files 确认文件存在。禁止猜测路径直接 read_file
2. **先列后写** — 创建项目前，先用 list_files 了解现有结构
3. **路径来自真实数据** — read_file 的 file_path 参数必须来自 list_files 的实际输出，不能是自己编造的
4. **操作后验证** — 文件写入/移动后，用 list_files 或 read_file 验证操作是否成功

## 输出格式

每次回复 **只输出一个 JSON 对象**（不要输出其他任何非 JSON 内容）：

调用工具时：
```json
{{"thought": "分析当前状态，决定下一步做什么", "action": "工具名", "action_input": {{"参数名": "值"}}}}
```

任务完成时：
```json
{{"thought": "总结完成了什么、结果如何", "final_answer": "给用户的最终回答"}}
```

## final_answer 质量标准

你的 final_answer 必须是一个**有用的、完整的回答**，而不是简单的文件列表。要求：
- ✅ "已为你创建了一个 Flask 项目，包含 app.py、requirements.txt 和 templates/index.html。运行方式：pip install -r requirements.txt && python app.py"
- ✅ "分析完成。该仓库是一个 Raft KV 存储系统，核心模块包括：1) raft_impl.py — Raft 共识算法实现... 主要发现：选举超时设置过短导致频繁 leader 切换"
- ❌ "app.py requirements.txt templates/index.html"（只是文件路径列表，没有解释）
- ❌ "我已经完成了任务"（没有说明具体做了什么）
- ❌ 输出的内容看起来完成了但实际上不可用

## 可用工具（完整列表，除此之外不存在其他工具）

{tools_desc}

## 分析代码仓库的标准流程

无论分析本地项目还是 GitHub 仓库，都必须遵循：

### 本地项目
1. 先用 list_files 列出项目目录结构
2. 用 read_file 读取关键文件（基于第 1 步的真实输出）
3. 用 search_files 搜索特定模式

### GitHub 仓库
1. 用 github_fetch(repo="owner/repo", github_action="list_files") 列出所有文件
2. 用 github_fetch(repo="owner/repo", github_action="fetch_readme") 获取 README
3. 用 github_fetch(repo="owner/repo", github_action="fetch_file", github_path="真实路径") 逐个获取关键源码
4. **基于实际代码分析，不要凭空推断**

### Git 克隆后的本地分析
1. 先用 command 执行 `git clone <url> <目录名>` 将仓库克隆到本地
2. 用 list_files 列出克隆后的目录结构
3. 用 read_file 读取关键文件（路径必须来自第 2 步的真实输出）
4. 基于实际代码进行分析
"""

# 内置工具描述
BUILTIN_TOOLS = {
    "command": {
        "name": "command",
        "description": "在本机终端执行 shell 命令（Windows 用 PowerShell，Linux/macOS 用 bash）。可用于运行脚本、安装依赖、查看系统信息等。不能用于读写文件（请用 read_file/write_file）。",
        "params": {"action": "要执行的终端命令，如 'pip install requests' 或 'dir'"},
    },
    "read_file": {
        "name": "read_file",
        "description": "读取本机文件内容并返回文本。支持分段读取（start_line 从 1 开始，max_lines 为行数）。仅限本地文件，不能读取 URL（请用 web_fetch）或 GitHub 仓库文件（请用 github_fetch）。",
        "params": {"file_path": "本地文件的绝对或相对路径", "start_line": "起始行号（可选，从 1 开始）", "max_lines": "读取行数（可选）"},
    },
    "write_file": {
        "name": "write_file",
        "description": "将文本内容完整写入本机文件（覆盖已有内容）。文件不存在时自动创建，父目录不存在时自动创建。",
        "params": {"file_path": "本地文件路径", "content": "要写入的完整文本内容"},
    },
    "list_files": {
        "name": "list_files",
        "description": "列出本机指定目录下的文件和子目录。仅限本地目录，不能列出 GitHub 仓库文件（请用 github_fetch）。",
        "params": {"file_path": "本地目录路径", "pattern": "glob 过滤模式，如 *.py 或 src/**/*.ts（可选，默认 *）"},
    },
    "search_files": {
        "name": "search_files",
        "description": "在本机指定目录中搜索包含关键词的文件，返回匹配的文件路径和行内容。类似 grep 功能。",
        "params": {"file_path": "搜索的根目录", "search_pattern": "要搜索的文本关键词或正则表达式", "file_filter": "文件名过滤，如 *.py（可选）"},
    },
    "git": {
        "name": "git",
        "description": "在本机执行 Git 版本控制操作。只支持查看类命令（status/diff/log/branch）和基本操作（add/commit）。",
        "params": {"git_command": "Git 子命令，如 'status'、'diff'、'log --oneline -10'、'add -A'、'commit -m msg'"},
    },
    "web_fetch": {
        "name": "web_fetch",
        "description": "通过 HTTP GET 请求抓取任意 URL 的内容并返回文本。HTML 页面会自动转为纯文本。可用于抓取文档、API 响应、raw 文件等。不能列出 GitHub 仓库文件结构（请用 github_fetch 的 list_files）。",
        "params": {"url": "要抓取的完整 URL，如 https://example.com/api/data"},
    },
    "edit_file": {
        "name": "edit_file",
        "description": "对本机文件进行精确的查找-替换编辑。old_text 必须与文件中的原文完全匹配（包括空格和缩进），替换 new_text。适合修改单处内容。",
        "params": {
            "file_path": "要编辑的本地文件路径",
            "old_text": "文件中要被替换的原始文本（必须精确匹配，包含缩进和空格）",
            "new_text": "替换后的新文本",
        },
    },
    "create_directory": {
        "name": "create_directory",
        "description": "在本机创建目录，如果父目录不存在会自动递归创建（类似 mkdir -p）。",
        "params": {"file_path": "要创建的目录路径"},
    },
    "file_move": {
        "name": "file_move",
        "description": "将文件或文件夹从一个位置移动到另一个位置。可用于重命名、整理文件。注意：移动操作不可撤销，请确认目标正确。",
        "params": {
            "source": "源文件/文件夹的路径",
            "destination": "目标路径（如果目标是目录且已存在，文件会被移入该目录；否则文件会被移动并重命名）",
        },
    },
    "file_copy": {
        "name": "file_copy",
        "description": "将文件复制到新位置。源文件保持不变。如果要备份文件或复制模板，请使用此工具。",
        "params": {
            "source": "源文件的路径",
            "destination": "目标路径（如果目标是目录且已存在，文件会被复制到该目录下；否则文件会被复制并重命名）",
        },
    },
    "batch_write": {
        "name": "batch_write",
        "description": "一次性写入多个文件（原子操作，全部成功或全部回滚）。适合创建多文件项目结构。",
        "params": {"files": "文件列表，格式: [{path: a.py, content: 文件内容}, ...]"},
    },
    "batch_edit": {
        "name": "batch_edit",
        "description": "一次性编辑多个文件，每个编辑操作独立执行和验证。适合跨文件重构。",
        "params": {"edits": "编辑列表，格式: [{file_path: a.py, old_text: 原文, new_text: 新文}, ...]"},
    },
    "code_index": {
        "name": "code_index",
        "description": "基于 AST 解析搜索项目中的代码符号（函数定义、类定义、变量名）。返回符号名称、所在文件和行号。仅支持 Python 文件。",
        "params": {"search_pattern": "要搜索的符号名或部分关键词", "file_path": "索引的根目录（可选，默认当前目录）"},
    },
    "ast_analyze": {
        "name": "ast_analyze",
        "description": "对 Python 文件进行 AST 深度分析：提取所有函数签名、类结构、继承关系、圈复杂度、未使用的 import。仅支持 .py 文件。",
        "params": {"file_path": "要分析的 Python 文件路径"},
    },
    "refactor": {
        "name": "refactor",
        "description": "代码重构工具。rename: 跨文件精确重命名符号（函数/类/变量）；clean_imports: 删除未使用的 import；analyze: 分析文件的重构建议。",
        "params": {"refactor_action": "rename（重命名）| clean_imports（清理导入）| analyze（分析建议）", "old_name": "旧符号名（rename 时必填）", "new_name": "新符号名（rename 时必填）", "file_path": "目标文件路径（clean_imports/analyze 时必填）"},
    },
    "diff_preview": {
        "name": "diff_preview",
        "description": "预览对文件的修改效果（生成 diff），但不实际修改文件。用于在执行 edit_file 前确认修改是否正确。",
        "params": {"file_path": "要预览修改的文件路径", "old_text": "要被替换的原文（编辑模式）", "new_text": "替换后的新文"},
    },
    "mcp_call": {
        "name": "mcp_call",
        "description": "调用通过 MCP 协议连接的外部工具服务器。需要先用 /mcp add 命令添加服务器并发现可用工具。",
        "params": {"tool_name": "MCP 工具名，格式为 server:tool 或 tool", "tool_args": "工具参数字典"},
    },
    "github_fetch": {
        "name": "github_fetch",
        "description": "GitHub 仓库专用操作工具。list_files: 列出仓库中所有文件路径（通过 GitHub API）；fetch_file: 获取指定文件的源码内容；fetch_readme: 自动查找并获取 README 文件。仅支持公开仓库。",
        "params": {"repo": "仓库标识，格式为 owner/repo（如 facebook/react）", "github_action": "list_files（列出文件）| fetch_file（获取文件）| fetch_readme（获取README）", "github_path": "文件路径（仅 fetch_file 时需要，如 src/index.js）", "branch": "分支名（可选，默认 main，失败时自动尝试 master）"},
    },
    "weather": {
        "name": "weather",
        "description": "查询指定城市的实时天气信息，包括温度、湿度、风速、穿衣建议等。支持中文城市名（如 '北京'、'重庆'）和英文城市名（如 'Beijing'、'Chongqing'）。",
        "params": {"city": "城市名称，如 '北京'、'重庆'、'Shanghai'", "lang": "语言，zh 中文（默认）或 en 英文"},
    },
    "datetime": {
        "name": "datetime",
        "description": "获取当前日期和时间信息，包括年月日、星期几、时分秒。当用户询问时间相关问题时使用此工具。",
        "params": {},
    },
    "register_tool": {
        "name": "register_tool",
        "description": "注册一个新的自定义工具，注册后可在后续对话中使用。支持两种模式：1) python_function: 传入 module.function 格式的 Python 函数路径，系统自动导入；2) command_template: 传入 shell 命令模板（用 {param} 表示参数占位符）。注册成功后工具立即可用。",
        "params": {
            "tool_name": "新工具的名称（英文，如 query_gold_price）",
            "description": "工具功能描述，LLM 会根据此描述决定何时调用",
            "python_function": "Python 函数路径，格式为 module.submodule.function（如 omniagent.utils.weather.get_weather）",
            "command_template": "Shell 命令模板，用 {param} 表示参数（如 curl -s https://api.example.com/{symbol}）",
            "params": "参数定义字典，格式: {type: object, properties: {param1: {type: string, description: ...}}}",
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

        from datetime import datetime
        now = datetime.now()
        weekdays_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        current_datetime = f"{now.year}年{now.month}月{now.day}日 {weekdays_cn[now.weekday()]} {now.strftime('%H:%M:%S')}"

        env_info = f"""

## 运行环境

- 操作系统: {os_info}
- Shell: {shell_info}
- Python: {sys.version.split()[0]}
- 工作目录: 通过命令 `pwd`（Linux/macOS）或 `Get-Location`（Windows）获取
- 当前日期时间: {current_datetime}

重要：
- 根据操作系统使用正确的命令。Windows 下不要使用 ls, cat, mkdir -p, uname, which, grep 等 Linux 命令。
- 当用户询问日期、时间、星期几时，直接回答上面提供的当前日期时间，不要编造或猜测。
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
            logger.debug(f"ReAct 注入 {len(recent)} 条对话历史")
        else:
            logger.warning("ReAct: 无对话历史可注入！")
        messages.append({"role": "user", "content": user_input})

        # 判断输入是否需要工具操作
        requires_tools = self._input_requires_tools(user_input)
        no_tool_streak = 0  # 连续未执行工具的轮次

        for i in range(self.max_iterations):
            logger.debug(f"ReAct 迭代 {i + 1}/{self.max_iterations}")

            # 调用 LLM
            response = self._call_llm(messages)
            messages.append({"role": "assistant", "content": response})

            # 解析 LLM 输出
            parsed = self._parse_response(response)

            thought = parsed.get("thought", "")
            if thought:
                self.callback.on_think(thought)

            final_answer = parsed.get("final_answer", "")
            if final_answer and final_answer.strip():
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
                        answer = final_answer
                        warning = (
                            "\n\n⚠️ **警告**: 本次回答未经工具执行验证。"
                            "LLM 声称完成了任务但未实际调用任何工具，"
                            "文件操作可能未真正执行。"
                        )
                        self.callback.on_warning("LLM 连续拒绝工具调用，附带警告返回")
                        logger.warning("ReAct: LLM 连续拒绝工具调用，附带警告返回")
                        self.callback.on_finish(answer + warning)
                        return answer + warning

                logger.debug(f"ReAct 完成，共 {i + 1} 次迭代，工具调用 {len(tracker.calls)} 次")
                answer = final_answer
                if tracker.has_executions():
                    summary = tracker.execution_summary()
                    logger.debug(f"ReAct 工具执行摘要: {summary}")
                self.callback.on_finish(answer)
                return answer

            if "action" in parsed:
                # 执行工具
                action = parsed["action"]
                action_input = parsed.get("action_input", {})

                logger.debug(f"ReAct 思考: {thought}")
                logger.debug(f"ReAct 行动: {action}({action_input})")
                self.callback.on_act(action, action_input)

                observation = self._execute_tool(action, action_input, ctx, tracker)
                self.callback.on_observe(observation)

                # 将观察结果加入对话
                obs_msg = f"Observation: {observation}"
                messages.append({"role": "user", "content": obs_msg})
                logger.debug(f"ReAct 观察: {observation[:200]}")
                no_tool_streak = 0
            else:
                # LLM 没有给出有效输出，尝试从最后一条观察中提取
                last_obs = ""
                for m in reversed(messages):
                    if m.get("role") == "user" and m.get("content", "").startswith("Observation:"):
                        last_obs = m["content"][len("Observation:"):].strip()
                        break
                if last_obs:
                    result = last_obs[:1000]
                else:
                    result = parsed.get("thought", "").strip() or response.strip()
                if not result:
                    result = "任务已执行，但未生成明确的回复内容。请尝试重新提问或使用更具体的指令。"
                self.callback.on_finish(result)
                return result

        # 达到最大迭代次数，尝试从最后的观察结果中提取有用信息
        last_obs = ""
        for m in reversed(messages):
            if m.get("role") == "user" and m.get("content", "").startswith("Observation:"):
                last_obs = m["content"][len("Observation:"):].strip()
                break
        if last_obs and len(last_obs) > 50:
            # 最后一条观察有实质内容，返回它
            msg = f"达到最大迭代次数 ({self.max_iterations})，以下是最后的执行结果：\n\n{last_obs[:2000]}"
        else:
            msg = f"达到最大迭代次数 ({self.max_iterations})，未能得出最终答案。请尝试简化问题或使用更具体的指令。"
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
        # 如果内置工具中没有，检查动态注册的工具
        if not tool_info and action in _DYNAMIC_TOOLS:
            tool_info = _DYNAMIC_TOOLS[action]
        if not tool_info:
            error_msg = f"错误: 未知工具 '{action}'，可用工具: {list(self.tools.keys()) + list(_DYNAMIC_TOOLS.keys())}"
            if tracker:
                tracker.record(action, action_input, False, error_msg, error=error_msg)
            return error_msg

        try:
            action_input = ToolNode.normalize_params(action_input)
            logger.debug(f"执行工具: {action}, 参数: {action_input}")
            node = ToolNode(
                f"react_{action}",
                action_type=action,
                **action_input,
            )
            result = node.execute(context)
            logger.debug(f"工具结果: {str(result)[:200]}")

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
