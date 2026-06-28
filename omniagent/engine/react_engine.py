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
import re
from pathlib import Path
from typing import Any

from omniagent.engine.callbacks import EngineCallback
from omniagent.engine.circuit_breaker import CircuitBreaker
from omniagent.engine.compactor import Compactor
from omniagent.engine.context import AgentContext
from omniagent.engine.tool_tracker import ToolExecutionTracker
from omniagent.engine.subagent import SubagentNotifier, get_background_registry
from omniagent.nodes.tool_node import _DYNAMIC_TOOLS, ToolNode
from omniagent.utils.llm_client import chat_completion
from omniagent.utils.response_adapter import parse_react, register_tools_from_dict

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

### ⏱️ 探索预算（分析类任务）
{exploration_budget}

### 📖 read_file 使用规范

- **默认读全文**：不传 start_line 和 max_lines，一次读完整文件
- **只在一种情况下分段**：文件明确超过 500 行（通过 list_files 无法判断大小时，先尝试全文读取）
- 绝对禁止对同一个文件多次分段读取（如先读前 200 行、再读中间 200 行）——这是最严重的效率浪费
- 如果文件太大被截断，换其他关键文件继续读，不要反复读同一个

## 📋 文件系统操作铁律

1. **先列后读** — 读取文件前，必须先用 list_files 确认文件存在。禁止猜测路径直接 read_file
2. **先列后写** — 创建项目前，先用 list_files 了解现有结构
3. **路径来自真实数据** — read_file 的 file_path 参数必须来自 list_files 的实际输出，不能是自己编造的
4. **操作后验证** — 文件写入/移动后，用 list_files 或 read_file 验证操作是否成功
5. **读全文不读片段** — read_file 一次读取完整文件，不要分段

## 输出格式 — 铁律

每次回复**必须且只能输出一个 JSON 对象**（不要输出任何非 JSON 内容）。JSON 对象必须包含 `thought` 字段，并且**必须**包含以下两者之一：

- **`action` + `action_input`** — 调用工具执行操作
- **`final_answer`** — 任务完成，交付最终结果

调用工具时：
```json
{{"thought": "分析当前状态，决定下一步做什么", "action": "工具名", "action_input": {{"参数名": "值"}}}}
```

任务完成时：
```json
{{"thought": "总结完成了什么、结果如何", "final_answer": "给用户的最终回答"}}
```

### ⚠️ 禁止的输出模式
- ❌ 只输出 `{{"thought": "..."}}` 而没有 `action` 或 `final_answer` — 这等于什么都没做
- ❌ 输出 `{{"thought": "我将继续分析...", "action": "..."}}` 但 action 为空或不存在

## 🔴 第三原则：final_answer 是交付物（禁止空洞化）

你的 `final_answer` 是你交付给用户的**最终产品**。它不是你的工作日志，不是你的待办清单，不是你对自己将要做什么的陈述。

### final_answer 铁律：
1. **禁止元语言/工作描述** — ❌ "我将分析这个项目" "继续完成分析" "基于收集到的信息..." "现在开始..." — 这些都是描述你自己在做什么的废话，不是用户要的分析
2. **必须包含实际内容** — ✅ 如果你被要求"分析项目"，final_answer 必须直接包含完整的分析报告文字
3. **禁止半截话** — ❌ "项目采用Flask后端+12个API蓝图..." 然后戛然而止，这不是一个完成的交付
4. **禁止空洞的完成声明** — ❌ "我已经完成了任务" "分析完成" "整体设计完善" — 没有具体内容的完成声明=没有完成
5. **分析报告必须结构化** — final_answer 必须直接呈现报告，至少包含：
   - 项目概述（1-2句）
   - 技术栈
   - 架构分析
   - 代码质量评估
   - 改进建议
6. **长度要求** — 分析类任务的 final_answer 至少 500 字。如果是创建/修改文件的任务，final_answer 需要说明创建了什么、为什么这样设计、如何使用

### final_answer 自检（输出前问自己）：
- 用户读完我的回答后，是否获得了有价值的信息？（如果一个字都没获得→退回重写）
- 我的回答是否像一个完整、可交付的分析报告？（如果像工作笔记→退回重写）
- 我有没有在回答中描述"我接下来要做什么"而不是直接给出结果？（如果有→去掉元语言，直接给结果）
- 回答中是否包含具体的技术细节（框架名、版本、文件路径、代码片段）而不仅仅是泛泛的形容词？

### 正确 vs 错误示例：

❌ **空洞（元语言）**：
"继续完成关键文件的读取和分析。项目采用Flask后端+12个API蓝图+React前端+pywebview桌面+ Vosk语音识别架构，整体设计完善。"

✅ **正确（实际内容）**：
"## 分析报告：语音日历工具

### 1. 项目概述
语音日历工具是一个以语音交互为核心的智能日程管理应用，支持通过自然语言语音指令创建日程和任务...

### 2. 技术栈
- 后端：Flask 3.0 + SQLAlchemy 3.1 + Vosk 0.3.45 离线语音识别
- 前端：React 19 + TypeScript 6 + Vite 8
- AI：DeepSeek API (deepseek-chat) 语义解析
- 天气：Open-Meteo 免费 API
- 数据库：SQLite

### 3. 架构分析
项目采用 Flask 蓝图模式，11 个 API 蓝图覆盖了 events/calendars/todos/reminders/voice/weather/sync/backup/desktop/assistant/usb_sync...

### 4. 代码质量
优点：LLM Prompt 工程扎实、降级策略完善...
问题：App.tsx 1885 行单体组件、零API认证、CORS全开...

### 5. 改进建议
1. 拆分 App.tsx 为多个自定义 Hook...
2. 引入 API 认证机制...
..."

❌ **空洞**："分析完成。该仓库是一个不错的项目。"
✅ **正确**："分析完成。该仓库是一个实现 Raft 共识算法的 KV 存储系统...（具体分析内容）"

## 可用工具（完整列表，除此之外不存在其他工具）

{tools_desc}

## 子 Agent 任务分化

你可以在执行过程中自然地将独立子任务分发给子 Agent 并行处理：

**可用工具**:
- `discover_agents` — 查看当前可用的子 Agent 类型及其能力
- `spawn_agent(goal, capability)` — 派生子 Agent 后台处理子任务
- `agent_result(task_id)` — 查询子 Agent 结果（不传 task_id 则列出全部）

**何时分化**:
- 需要搜索多个独立的目录/模式 → 并行 spawn code-explorer
- 需要同时创建/修改多个独立文件 → 并行 spawn file-writer
- 写完代码后需要运行测试验证 → spawn test-runner
- 对自己的输出不确定，需要独立审查 → spawn general-purpose

**分化原则**:
- 子任务之间无依赖时才并行化 — 有依赖的必须串行
- 每个子 Agent 只分配一个明确的小任务，不要太宽泛
- 先用 discover_agents 了解可用类型，再选择合适的 capability
- 子 Agent 结果通过 agent_result 查询，然后整合到你的 final_answer 中
- 子 Agent 有独立上下文 — 通过 context_seed 传递必要的文件路径和约束

## 持续学习与记忆

你可以通过 `remember` 工具将重要的经验持久化，供后续会话使用：

- **何时记录**: 用户表达明确偏好（"我习惯用 X 而不是 Y"）、纠正你的错误并确认正确做法、发现项目特定的编码约定
- **分类**:
  - `user-prefs`: 用户偏好（习惯、风格、工具偏好）
  - `project-rules`: 项目规则（命名规范、目录结构、特定约定）
  - `learned-patterns`: 经验教训（从错误中学到的模式、最佳实践）
- **原则**: 只记录经过确认的内容，不要猜测。一条记忆 1-3 句话即可。

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
1. **先检查是否已克隆**：用 list_files 检查目标目录是否已存在且包含项目文件
   - 如果已存在完整的项目目录 → 跳过 git clone，直接用 list_files + read_file 分析
   - 不要在已有完整克隆的情况下重复执行 git clone！
2. 如果确实需要克隆：用 command 执行 `git clone <url> <目录名>` 将仓库克隆到本地
   - git clone 可能需要较长时间（大型仓库可达 2-3 分钟），耐心等待
   - 如果 git clone 超时，检查目录是否部分克隆成功（有 .git 目录和关键文件）
3. 用 list_files 列出克隆后的目录结构
4. 用 read_file 读取关键文件（路径必须来自第 3 步的真实输出）
5. 基于实际代码进行分析

### 🔑 GitHub API 限流应对
GitHub API 对未认证请求限制 60次/小时。遇到 403 限流时：
1. 优先使用 `git clone` 克隆到本地分析
2. 使用 `web_fetch` 访问 `raw.githubusercontent.com` 获取单个文件（raw 文件无限流）
3. 不要重复调用 github_fetch 和 api.github.com 的 web_fetch
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
    "pytest": {
        "name": "pytest",
        "description": "运行 pytest 测试框架。写完代码后使用此工具验证代码是否正确。返回通过/失败/错误的统计。",
        "params": {
            "test_path": "测试文件或目录路径，默认 tests/",
            "filter_expr": "pytest -k 筛选表达式（可选），如 'test_move or test_copy'",
            "stop_on_fail": "第一个失败即停止（-x），默认 false",
        },
    },
    "run_test": {
        "name": "run_test",
        "description": "执行任意测试命令（适用于 pytest 以外的测试框架，如 unittest, go test, npm test）。命令在工作目录下执行。",
        "params": {
            "command": "测试命令，如 'python -m unittest discover -s tests' 或 'cargo test'",
            "timeout_seconds": "超时秒数，默认 120",
        },
    },
    # ── 子 Agent 工具（从 subagent.py 注册到生产路径）──
    "discover_agents": {
        "name": "discover_agents",
        "description": "发现可用的子 Agent 类型及其能力。返回每个子 Agent 的名称、可用工具、是否只读等信息。主 Agent 应先调用此工具了解可用类型，再选择合适的类型 spawn。",
        "params": {
            "name": "指定 AgentCard 名称查看详情（可选，不传则列出全部）",
        },
    },
    "spawn_agent": {
        "name": "spawn_agent",
        "description": "派生子 Agent 在后台独立处理子任务。子 Agent 有独立上下文，结果通过 agent_result 工具查询。通过 capability 参数指定子 Agent 类型。适合并行处理多个独立子任务。",
        "params": {
            "goal": "子任务目标描述",
            "capability": "子 Agent 能力类型（code-explorer/file-writer/test-runner/general-purpose），默认 general-purpose",
            "context_seed": "父 Agent 传递的上下文（可选）：{parent_goal, discovered_files, constraints}",
            "model": "指定模型（可选，默认继承父 Agent）",
        },
    },
    "agent_result": {
        "name": "agent_result",
        "description": "查询子 Agent 任务的执行结果。需要先通过 spawn_agent 创建子任务。不传 task_id 则列出所有子任务。",
        "params": {
            "task_id": "子任务 ID（可选，不传则列出所有子任务）",
        },
    },
    # ── 自主记忆持久化 ──
    "remember": {
        "name": "remember",
        "description": "持久化一条长期记忆/模式到系统提示词文件夹，供后续会话使用。当用户表达偏好（习惯、偏好、不喜欢）、发现项目特定约定、用户纠正你的错误并确认了正确做法时，应主动调用此工具。",
        "params": {
            "content": "要持久化的学习内容（1-3 句话即可）",
            "tags": "用于相关性匹配的标签列表，如 ['python', 'testing']",
            "category": "记忆分类: user-prefs（用户偏好）| project-rules（项目规则）| learned-patterns（经验教训）",
        },
    },
}


# ── final_answer 空洞检测辅助函数 ──────────────────────────────
_HOLLOW_PATTERNS: list[tuple[str, str]] = [
    # (正则 pattern, 解释)
    (r"^继续完成", "以'继续完成'开头 — 这是工作描述，不是分析报告"),
    (r"^我将", "以'我将'开头 — 描述意图而非交付内容"),
    (r"^我会", "以'我会'开头 — 描述意图而非交付内容"),
    (r"^接下来", "以'接下来'开头 — 工作日志风格"),
    (r"^首先[我，]", "以'首先'开头 — 工作日志风格"),
    (r"^然后", "以'然后'开头 — 工作日志风格"),
    (r"^现在开始", "以'现在开始'开头 — 工作日志风格"),
    (r"^让我[来们]", "以'让我'开头 — 自我对话风格"),
    (r"^下面我", "以'下面我'开头 — 工作日志风格"),
    (r"^基于(已)?(收集|获取|上述)", "以'基于收集/获取'开头 — 元语言而非内容"),
    (r"^已经收集", "以'已经收集'开头 — 元语言"),
    (r"^所有(外部)?工具", "以'所有工具'开头 — 工具状态描述而非分析"),
    (r"整体设计完善$", "以'整体设计完善'结尾 — 空泛评价无实质内容"),
    (r"^(分析)?完成[。！]?$", "纯完成声明无任何分析内容"),
    (r"^任务已[经办]", "处理状态描述而非分析内容"),
]

# ── 可配置默认值（实例可覆盖）──
_DEFAULT_MIN_FINAL_ANSWER_LENGTH = 200  # 分析类 final_answer 最低字符数
_DEFAULT_MIN_STRUCTURED_SECTIONS = 2    # 至少包含 2 个分析维度
_DEFAULT_MAX_NO_TOOL_STREAK = 2         # 连续未执行工具的最大容忍轮次
_DEFAULT_COMPACT_INTERVAL = 3           # 上下文压缩间隔（轮）
_DEFAULT_TOOL_RETRY_ATTEMPTS = 2        # 工具执行最大尝试次数
# ── 探索预算参数保持为静态默认值，但在 __init__ 中会根据 max_iterations 动态缩放 ──
_DEFAULT_EXPLORATION_START = 3          # 探索预算：前 N 步了解结构（静态默认，会被缩放）
_DEFAULT_EXPLORATION_SYNTHESIZE = 8     # 探索预算：第 N 步起必须合成（静态默认，会被缩放）
_DEFAULT_HURRY_WARNING = 4              # 剩余 N 轮时注入加速提示（静态默认，会被缩放）
_DEFAULT_FORCE_SYNTHESIS = 2            # 剩余 N 轮时强制合成（静态默认，会被缩放）
_DEFAULT_MIDPOINT_CALLS = 6             # 工具调用 N 次后进行中点提醒（静态默认，会被缩放）
# ── 缩放系数 ──
_EXPLORATION_START_RATIO = 0.25         # 探索阶段占 max_iterations 的比例（前 25%）
_EXPLORATION_SYNTHESIZE_RATIO = 0.70    # 合成阶段占 max_iterations 的比例（前 70%，即最后 30% 强制合成）
_HURRY_WARNING_RATIO = 0.35             # 剩余比例低于此值时发出加速警告
_FORCE_SYNTHESIS_RATIO = 0.15           # 剩余比例低于此值时强制合成
_MIDPOINT_CALLS_RATIO = 0.50            # 工具调用次数超过此比例时提醒
_REACT_LOOP_MAX_TOKENS = 32768          # ReAct 循环内每次 LLM 调用的最大输出 token（P1-7: 从 16384 提升，避免工具调用截断）


def _check_hollow_answer(
    final_answer: str,
    user_input: str = "",
    tracker: ToolExecutionTracker | None = None,
    min_length: int = _DEFAULT_MIN_FINAL_ANSWER_LENGTH,
    min_sections: int = _DEFAULT_MIN_STRUCTURED_SECTIONS,
) -> dict:
    """检测 final_answer 是否空洞/无实质内容。

    主信号: 元语言模式匹配（Hollow Pattern）
    辅助信号: 长度过短、结构缺失
    判定: 主信号匹配 + 至少一个辅助信号确认 → hollow

    Returns:
        {"is_hollow": bool, "reason": str}
    """
    text = final_answer.strip()
    reasons: list[str] = []

    # ── 主信号: 元语言/空洞模式检测 ──
    hollow_pattern_matched = False
    hollow_reason = ""
    for pattern, reason in _HOLLOW_PATTERNS:
        if re.search(pattern, text):
            # 技术词汇检查 — 只在回答足够长 (>400 chars) 且有结构时才绕过
            tech_markers = [
                "Flask", "React", "Python", "SQLite", "API", "TypeScript",
                "##", "###", "```",
            ]
            tech_count = sum(1 for m in tech_markers if m.lower() in text.lower())
            is_long_enough = len(text) >= 400
            if tech_count >= 3 and is_long_enough:
                continue  # 足够长且有技术内容，不过滤
            hollow_pattern_matched = True
            hollow_reason = reason
            break

    # ── 辅助信号 1: 长度过短 ──
    is_too_short = len(text) < min_length
    if is_too_short:
        action_verbs = ("创建", "写入", "删除", "移动", "复制", "安装", "执行", "运行")
        _user_is_action = any(v in user_input for v in action_verbs)
        if _user_is_action:
            is_too_short = False  # 操作类任务允许简短回答

    if is_too_short:
        reasons.append(f"长度仅 {len(text)} 字 (需要 ≥{min_length})")

    # ── 辅助信号 2: 分析类任务缺乏结构 ──
    is_poor_structure = False
    analysis_markers = ("分析", "诊断", "评估", "审查", "报告")
    is_analysis_task = any(m in user_input for m in analysis_markers)
    if is_analysis_task:
        section_indicators = [
            "##", "###", "1.", "2.", "3.", "一、", "二、", "三、",
            "项目", "架构", "技术栈", "代码", "建议", "改进",
        ]
        section_count = sum(1 for ind in section_indicators if ind in text)
        substantive_indicators = sum(
            1 for ind in ("Flask", "React", "Python", "TypeScript", "SQLite", "API", "数据库")
            if ind.lower() in text.lower()
        )
        if section_count < min_sections and substantive_indicators < 2:
            is_poor_structure = True
            reasons.append(f"分析任务缺少结构化内容 (结构标记: {section_count}, 技术指标: {substantive_indicators})")

    # ── 综合判定: 主信号 + 辅助信号 ──
    if hollow_pattern_matched and (is_too_short or is_poor_structure):
        return {"is_hollow": True, "reason": f"{hollow_reason}; {'; '.join(reasons)}"}

    # 极端情况: 没有匹配 hollow pattern 但长度极短 (< 50 chars) 且是分析任务且无实质内容
    if is_analysis_task and len(text) < 50 and not _is_action_task_input(user_input):
        substantive_count = sum(
            1 for m in ("Flask", "React", "Python", "SQLite", "架构", "模块", "API")
            if m.lower() in text.lower()
        )
        if substantive_count < 2:
            return {
                "is_hollow": True,
                "reason": f"分析任务回答极短 ({len(text)} 字) 且无实质技术内容",
            }

    return {"is_hollow": False, "reason": ""}


def _is_substantive_answer(text: str) -> bool:
    """检查文本是否为实质性回答，而非计划描述/元语言。

    实质性回答的特征:
    1. 包含具体内容（代码、分析、数据等）
    2. 不以"我将...""让我..."等计划描述开头

    计划描述示例（应返回 False）:
    - "好的，让我先读取文件结构，然后分析代码质量"
    - "我先检查一下项目目录..."
    - "我将分析这个项目的代码..."

    实质性回答示例（应返回 True）:
    - "## 分析报告\\n\\n项目采用Flask架构..."
    - "这个问题可以通过以下方式解决：..."
    """
    text_stripped = text.strip()
    if len(text_stripped) < 20:
        return False

    # ── 计划描述语言检测 ──
    plan_patterns = [
        r"^(好的|好[的，]|嗯[，,]|OK[，,])\s*(让我|我先|我将|我会|接下来|现在)",
        r"^(让我|我先|我将|我会|接下来|现在)[，，]?\s*(查看|读取|检查|分析|搜索|找|确认|看看|研究|理解|探索|扫描)",
        r"^(正在|准备|开始|尝试|需要)\s*(读取|分析|检查|搜索)",
        r"^首先[，,]?\s*(让?我)",
        r"^(基于|根据).*(分析|结果|信息).*(，|,).*(我将|我会|可以|给出|提供|总结)",
        r"好的，让我先",
        r"让我先.*(，|。|\n)",
        r"请稍等.*让我",
        r"我先.*一下",
    ]
    for pattern in plan_patterns:
        if re.search(pattern, text_stripped):
            return False

    # ── 检查实质性内容标记 ──
    content_markers = [
        "##", "###", "```", "def ", "class ", "import ",
        "项目", "架构", "技术栈", "代码", "建议", "改进",
        "Flask", "React", "Python", "SQLite", "API",
        "分析报告", "总结", "解决方案", "修复",
    ]
    has_content = any(marker in text_stripped for marker in content_markers)

    # ── 检查是否空洞 ──
    hollow_check = _check_hollow_answer(text_stripped)
    if hollow_check.get("is_hollow"):
        return False

    return has_content or len(text_stripped) >= 200


def _is_action_task_input(text: str) -> bool:
    """检测用户输入是否为纯操作类任务（允许简短回答）。"""
    action_verbs = ("创建", "写入", "删除", "移动", "复制", "安装", "执行", "运行")
    return any(v in text for v in action_verbs)


def _build_observation_summary(
    messages: list[dict],
    tracker: ToolExecutionTracker,
    max_items: int = 15,
) -> str:
    """从消息历史和 tool tracker 构建观察摘要，供合成阶段使用。

    提取所有工具调用记录和观察结果，格式化为结构化摘要。
    """
    parts: list[str] = []

    # 1. 工具调用统计
    if tracker.calls:
        parts.append(f"**工具调用**: {len(tracker.calls)} 次")
        parts.append(tracker.detail_log()[:2000])
        parts.append("")

    # 2. 从消息中提取关键观察内容摘要
    observations: list[str] = []
    for m in messages:
        content = m.get("content", "") if isinstance(m, dict) else ""
        if not content or len(content) < 50:
            continue
        # 收集观察消息（工具执行结果）
        if "📋" in content and "执行结果" in content:
            # 提取文件内容的关键信息
            # 截取前 300 字符作为摘要
            body = content.split("\n", 1)
            if len(body) > 1:
                snippet = body[1].strip()[:300]
                # 去掉末尾的提示语
                hint = "请根据此结果决定下一步"
                if hint in snippet:
                    snippet = snippet[: snippet.index(hint)].strip()
                if snippet:
                    observations.append(snippet)

    if observations:
        # 限制摘要长度
        total = 0
        included: list[str] = []
        for obs in observations:
            if total + len(obs) > 3000:
                included.append(f"... (还有 {len(observations) - len(included)} 条观察结果)")
                break
            included.append(obs)
            total += len(obs)
        parts.append("**已收集的文件内容摘要**:")
        for idx, snippet in enumerate(included[:max_items], 1):
            # 提取文件路径（通常在第一行或内容开头）
            first_line = snippet.split("\n")[0][:120]
            parts.append(f"{idx}. {first_line}")
        parts.append("")

    return "\n".join(parts) if parts else "(尚未收集到数据)"


def _compile_exhaustion_report(
    tracker: ToolExecutionTracker,
    messages: list[dict],
    max_iterations: int,
) -> str:
    """迭代耗尽时，将所有观察结果编译成结构化报告。

    这是在 LLM 未能产出 final_answer 时的最后兜底方案。
    """
    calls = tracker.calls

    report_parts = [
        f"## ⚠️ 自动编译报告（达到最大迭代次数 {max_iterations}）",
        "",
        f"**工具调用**: {len(calls)} 次（{sum(1 for c in calls if c.success)} 成功, {sum(1 for c in calls if not c.success)} 失败）",
        "",
        "### 执行的工具",
        "",
    ]

    # 列出所有工具调用
    for idx, call in enumerate(calls, 1):
        status_icon = "✅" if call.success else "❌"
        params_str = ", ".join(f"{k}={str(v)[:60]}" for k, v in call.params.items())
        report_parts.append(f"{idx}. {status_icon} **{call.tool_name}**({params_str})")
        if call.result_summary:
            summary = call.result_summary[:200].replace("\n", " ")
            report_parts.append(f"   → {summary}")

    report_parts.append("")
    report_parts.append("### 收集到的数据摘要")
    report_parts.append("")

    # 提取观察结果中的关键文件内容
    obs_count = 0
    for m in messages:
        content = m.get("content", "") if isinstance(m, dict) else ""
        if "📋" in content and "执行结果" in content:
            body = content.split("\n", 1)
            if len(body) > 1:
                snippet = body[1].strip()
                hint = "请根据此结果决定下一步"
                if hint in snippet:
                    snippet = snippet[: snippet.index(hint)].strip()
                if len(snippet) > 20:
                    obs_count += 1
                    report_parts.append(f"**观察 #{obs_count}**:")
                    report_parts.append("```")
                    report_parts.append(snippet[:800])
                    report_parts.append("```")
                    report_parts.append("")
                    if obs_count >= 10:
                        break

    if obs_count == 0:
        report_parts.append("_(未收集到有效数据)_")

    report_parts.append("---")
    report_parts.append("💡 **提示**: 请重新运行任务并给出更具体的指令以获取完整分析。")
    report_parts.append(f"   建议: 减少探索范围或增加 max_iterations（当前={max_iterations}）。")

    return "\n".join(report_parts)


def _extract_last_observation(messages: list[dict]) -> str:
    """从消息历史中提取最后一条有意义的观察结果。"""
    for m in reversed(messages):
        content = m.get("content", "") if isinstance(m, dict) else ""
        if not content:
            continue
        # 匹配 "📋 工具 '...' 执行结果" 格式（我们注入的观察消息）
        if "📋" in content and "执行结果" in content:
            # 提取实际内容（去掉前缀提示）
            parts = content.split("\n", 1)
            if len(parts) > 1:
                # 去掉最后一行提示语
                body = parts[1].strip()
                hint = "请根据此结果决定下一步"
                if hint in body:
                    body = body[:body.index(hint)].strip()
                if len(body) > 50:
                    return body
            continue
        # 匹配旧的 "Observation:" 格式
        if content.startswith("Observation:"):
            return content[len("Observation:"):].strip()

    return ""


class ReActEngine:
    """ReAct 思考-行动-观察循环引擎。"""

    # 用于标记"未提供"的哨兵值
    _UNSET = object()

    def __init__(
        self,
        model_priority: list[str],
        *,
        max_iterations: int = 10,
        system_prompt: str | None = None,
        tools: dict[str, dict] | None = None,
        callback: EngineCallback | None = None,
        # ── Trace 记录（可选，用于调试/审计）──
        trace_dir: str | None = None,
        # ── 中断机制（可选）──
        interrupt_event: object | None = None,  # threading.Event
        # ── 可配置阈值（None 表示根据 max_iterations 动态缩放）──
        min_final_answer_length: int = _DEFAULT_MIN_FINAL_ANSWER_LENGTH,
        min_structured_sections: int = _DEFAULT_MIN_STRUCTURED_SECTIONS,
        max_no_tool_streak: int = _DEFAULT_MAX_NO_TOOL_STREAK,
        compact_interval: int = _DEFAULT_COMPACT_INTERVAL,
        tool_retry_attempts: int = _DEFAULT_TOOL_RETRY_ATTEMPTS,
        # 探索预算相关 — 如果传入 0 或省略，则根据 max_iterations 动态计算
        exploration_budget_start: int | None = None,
        exploration_budget_synthesize: int | None = None,
        hurry_warning_threshold: int | None = None,
        force_synthesis_threshold: int | None = None,
        midpoint_check_calls: int | None = None,
    ) -> None:
        self.model_priority = model_priority
        self.max_iterations = max_iterations
        self.tools = tools or BUILTIN_TOOLS
        self.callback = callback or EngineCallback()
        self.breaker = CircuitBreaker()  # 工具断路器
        self._interrupt_event = interrupt_event  # 中断事件（Esc/Ctrl+C）

        # ── 子 Agent 通知轮询 ──
        self._active_subagent_ids: set[str] = set()  # 已 spawn 但未通知的 task_id
        self._subagent_notifier: SubagentNotifier | None = None

        # ── 静态阈值 ──
        self.min_final_answer_length = min_final_answer_length
        self.min_structured_sections = min_structured_sections
        self.max_no_tool_streak = max_no_tool_streak
        self.compact_interval = compact_interval
        self.tool_retry_attempts = tool_retry_attempts

        # ── 动态缩放阈值：基于 max_iterations 按比例计算 ──
        # 原始默认值基于 max_iterations=10 设计，现在按比例缩放到实际的 max_iterations
        self.exploration_budget_start = (
            exploration_budget_start
            if exploration_budget_start is not None
            else max(1, int(_EXPLORATION_START_RATIO * max_iterations))
        )
        self.exploration_budget_synthesize = (
            exploration_budget_synthesize
            if exploration_budget_synthesize is not None
            else max(2, int(_EXPLORATION_SYNTHESIZE_RATIO * max_iterations))
        )
        self.hurry_warning_threshold = (
            hurry_warning_threshold
            if hurry_warning_threshold is not None
            else max(2, int(_HURRY_WARNING_RATIO * max_iterations))
        )
        self.force_synthesis_threshold = (
            force_synthesis_threshold
            if force_synthesis_threshold is not None
            else max(1, int(_FORCE_SYNTHESIS_RATIO * max_iterations))
        )
        self.midpoint_check_calls = (
            midpoint_check_calls
            if midpoint_check_calls is not None
            else max(2, int(_MIDPOINT_CALLS_RATIO * max_iterations))
        )

        self.system_prompt = system_prompt or self._build_system_prompt()

        # ── 可选的 Trace 记录器（用于调试/审计）──
        self._trace = None
        if trace_dir:
            from omniagent.engine.trace import TraceWriter
            self._trace = TraceWriter(trace_dir)

    def _build_system_prompt(self) -> str:
        import sys
        tools_desc = "\n".join(
            f"- {t['name']}: {t['description']} (参数: {t['params']})"
            for t in self.tools.values()
        )

        # 动态生成探索预算文本
        exploration_budget = (
            f"分析项目时，你有严格的探索预算：\n"
            f"- **前 {self.exploration_budget_start} 步**：用 list_files 了解项目根目录 + 一级子目录结构\n"
            f"- **第 {self.exploration_budget_start + 1}-{self.exploration_budget_synthesize - 1} 步**：用 read_file 读取最核心的源文件（入口文件、主要模块、配置文件）\n"
            f"- **第 {self.exploration_budget_synthesize} 步起**：必须开始合成 final_answer，不要再探索新文件\n"
            f"- 如果你在第 {self.exploration_budget_synthesize} 步还在读 README/build脚本/配置文件，说明探索策略失败"
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
        return REACT_SYSTEM_PROMPT.format(
            tools_desc=tools_desc,
            exploration_budget=exploration_budget,
        ) + env_info

    def _poll_subagents(self, iteration: int, messages: list[dict]) -> int:
        """轮询已完成的子 Agent 并将结果注入消息列表。

        混合通知策略：
        - 每轮：非阻塞检查 _active_subagent_ids 中指定的任务
        - 每 6 轮（≈60s）：全量扫描兜底

        Returns:
            本轮注入的消息数量
        """
        if self._subagent_notifier is None:
            self._subagent_notifier = SubagentNotifier(get_background_registry())

        notifier = self._subagent_notifier
        injected = 0

        # 1. 检查已知活跃子 Agent（最快路径）
        if self._active_subagent_ids:
            completed = notifier.poll_completed(list(self._active_subagent_ids))
            for task_id, task in completed.items():
                self._active_subagent_ids.discard(task_id)
                status_icon = "✅" if task.status == "success" else "❌"
                result_preview = task.result[:800] if task.result else "(无输出)"
                msg = (
                    f"[子 Agent 完成] {status_icon} {task_id}\n"
                    f"目标: {task.goal}\n"
                    f"结果:\n{result_preview}"
                )
                messages.append({"role": "user", "content": msg})
                injected += 1
                logger.info(
                    "ReAct: 子 Agent %s 完成 (%s)，结果已注入上下文 (%d chars)",
                    task_id, task.status, len(task.result),
                )

        # 2. 长周期全量扫描兜底（每 6 轮强制一次）
        force_full = (iteration > 0 and iteration % 6 == 0)
        all_completed = notifier.poll_all(force=force_full)
        for task in all_completed:
            if task.task_id in self._active_subagent_ids:
                self._active_subagent_ids.discard(task.task_id)
            status_icon = "✅" if task.status == "success" else "❌"
            result_preview = task.result[:800] if task.result else "(无输出)"
            msg = (
                f"[子 Agent 完成·全量扫描] {status_icon} {task.task_id}\n"
                f"目标: {task.goal}\n"
                f"结果:\n{result_preview}"
            )
            messages.append({"role": "user", "content": msg})
            injected += 1
            logger.info(
                "ReAct: 全量扫描发现已完成子 Agent %s (%s)，结果已注入",
                task.task_id, task.status,
            )

        if injected > 0:
            self.callback.on_observe(f"📬 {injected} 个子 Agent 结果已注入上下文")
        return injected

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
        # 注入对话历史（最近 10 条，包括最近的 system 消息以保留 system_hint）
        history = ctx.get_conversation_messages()
        if history:
            non_system = [m for m in history if m.get("role") != "system"][-10:]
            system_msgs = [m for m in history if m.get("role") == "system"][-2:]
            recent = system_msgs + non_system
            messages.extend(recent)
            logger.debug(f"ReAct 注入 {len(recent)} 条对话历史 (含 {len(system_msgs)} 条 system)")
        else:
            logger.warning("ReAct: 无对话历史可注入！")
        messages.append({"role": "user", "content": user_input})

        # ── 初始化上下文压缩器 ──
        session_dir = Path.cwd() / ".omniagent" / "sessions" / getattr(ctx, "run_id", "default")
        compactor = Compactor(session_dir)
        compact_count = 0

        # 判断输入是否需要工具操作
        requires_tools = self._input_requires_tools(user_input)
        no_tool_streak = 0  # 连续未执行工具的轮次
        thought_only_streak = 0  # 连续 thought-only 轮次
        correction_count = 0  # P3-Fix11: 纠正消息计数器

        for i in range(self.max_iterations):
            # ── 中断检查（Esc / Ctrl+C）──
            if self._interrupt_event is not None and self._interrupt_event.is_set():
                logger.info(f"ReAct: 第 {i} 轮检测到中断信号，停止执行")
                interrupted_msg = (
                    f"## ⚠️ 任务被用户中断\n\n"
                    f"任务在第 {i + 1}/{self.max_iterations} 轮被中断。\n"
                    f"已执行的工具调用: {len(tracker.calls)} 次。\n\n"
                    f"{tracker.execution_summary() if tracker.has_executions() else '(未执行任何工具)'}"
                )
                self.callback.on_warning("用户中断")
                self.callback.on_finish(interrupted_msg)
                return interrupted_msg

            # ── 子 Agent 通知轮询（混合通知：push + poll + 长周期兜底）──
            subagent_result = self._poll_subagents(i, messages)

            # ── 上下文压缩检查（按可配置间隔 + 初始检查）──
            if i == 0 or (i > 0 and i % self.compact_interval == 0):
                estimated = Compactor._estimate_tokens(messages)
                if compactor.needs_compact(estimated):
                    logger.info(f"ReAct: 第 {i} 轮触发压缩 (≈{estimated} tokens)")
                    # compact 对话部分（保留 system prompt）
                    to_compact = messages[1:]  # 跳过 system prompt
                    result = compactor.compact(to_compact, self.model_priority)
                    if result:
                        compacted = compactor.apply_compact(to_compact, result)
                        messages = [messages[0], *compacted]
                        compact_count += 1
                        logger.info(
                            f"ReAct: 压缩完成 (第 {compact_count} 次), "
                            f"{result.original_token_estimate} → {result.summary_tokens} tokens"
                        )
            logger.debug(f"ReAct 迭代 {i + 1}/{self.max_iterations}")

            # ── P2-Fix6: 自适应合成时机（基于实际工具使用进度）──
            remaining = self.max_iterations - i
            tool_count = len(tracker.calls)
            successful_tools = sum(1 for c in tracker.calls if c.success)
            # 检测是否在进行文件探索（read_file/list_files）
            exploring = any(
                c.tool_name in ("read_file", "list_files", "search_files", "github_fetch")
                for c in tracker.calls[-3:]
            ) if tracker.calls else False

            # 场景 A: 预算消耗30%+但无任何工具使用 → 强制要求
            if i >= max(2, int(self.max_iterations * 0.3)) and tool_count == 0:
                hurry_msg = (
                    f"你已经用了 {i + 1} 轮但没有使用任何工具。"
                    f"请立即使用工具开始执行任务。"
                    f"用 list_files 了解结构，用 read_file 读取关键文件。"
                )
                messages.append({"role": "user", "content": hurry_msg})
                logger.info(f"ReAct: 30%预算无工具使用，注入加速提示 (第 {i + 1} 轮)")
            # 场景 B: 已用工具且接近上限 → 强制合成（附带数据摘要）
            elif remaining <= self.force_synthesis_threshold and tool_count > 0:
                obs_summary = _build_observation_summary(messages, tracker)
                hurry_msg = (
                    f"仅剩 {remaining} 轮！直接基于已收集数据输出 final_answer。\n\n"
                    f"## 已收集的数据\n{obs_summary}\n\n"
                    f"输出 {{\"final_answer\": \"完整的分析报告\"}}，不要再调用工具。"
                )
                messages.append({"role": "user", "content": hurry_msg})
                logger.info(f"ReAct: 强制合成 (剩余 {remaining} 轮，已执行 {tool_count} 次工具)")
            # 场景 C: 提醒预算但给更多空间（仅当未在探索新文件时）
            elif remaining <= self.hurry_warning_threshold and tool_count > 0 and not exploring:
                midpoint_msg = (
                    f"已执行 {tool_count} 次工具调用，剩余 {remaining} 轮。"
                    f"如果已读取核心文件，现在开始合成 final_answer。"
                )
                messages.append({"role": "user", "content": midpoint_msg})
                logger.info(f"ReAct: 合成提醒 (已 {tool_count} 次调用，剩余 {remaining} 轮)")
            # 场景 D: 还在探索文件但接近预算 → 温和提醒
            elif remaining <= self.hurry_warning_threshold + 1 and exploring and tool_count >= 3:
                gentle_msg = (
                    f"已读取 {successful_tools} 个文件，剩余 {remaining} 轮。"
                    f"再读最关键的 1-2 个文件就可以开始合成了。"
                )
                messages.append({"role": "user", "content": gentle_msg})

            # ── 调用 LLM（优先原生工具调用，回退 JSON 解析）──
            try:
                native_response = self._call_llm_native(messages)
            except Exception as e:
                logger.error(f"ReAct: LLM 调用异常 (第 {i + 1} 轮): {e}", exc_info=True)
                if tracker.has_executions():
                    compiled = _compile_exhaustion_report(tracker, messages, self.max_iterations)
                    self.callback.on_warning(f"LLM 调用异常，已自动编译结果: {e}")
                    self.callback.on_finish(compiled)
                    return compiled
                error_msg = f"## 引擎异常\n\nLLM 调用失败 (第 {i + 1} 轮): {e}\n请检查模型配置后重试。"
                self.callback.on_error(error_msg)
                return error_msg
            response_text = native_response.get("raw_text", "")
            messages.append({"role": "assistant", "content": response_text})

            # 解析结果：优先使用原生 tool_calls，回退到 JSON 解析
            parsed = native_response

            # ── 处理 JSON 解析失败（仅回退路径）──
            if parsed.get("parse_error"):
                correction_count += 1
                # P3-Fix11: 最多2次格式纠正后强制退出
                if correction_count >= 3:
                    logger.warning(
                        f"ReAct: 连续 {correction_count} 次格式错误，停止纠正并退出"
                    )
                    self.callback.on_warning(
                        f"LLM 格式错误过多（{correction_count}次），停止执行"
                    )
                    last_obs = _extract_last_observation(messages)
                    if last_obs and len(last_obs) > 50:
                        result = (
                            f"## 任务执行中断\n\n"
                            f"原因: LLM 连续 {correction_count} 次输出格式错误。\n\n"
                            f"### 最后收集到的数据\n{last_obs[:2000]}"
                        )
                    else:
                        result = (
                            f"## 任务执行中断\n\n"
                            f"原因: LLM 连续 {correction_count} 次输出格式错误。\n"
                            f"请检查 LLM 配置后重试。"
                        )
                    self.callback.on_finish(result)
                    return result
                logger.warning(
                    f"ReAct: 第 {i + 1} 轮 JSON 解析失败（原生工具调用也失败），"
                    f"要求 LLM 以正确格式重试 (纠正 #{correction_count})"
                )
                self.callback.on_warning(
                    f"LLM 输出格式错误，要求重试（第 {i + 1} 轮）"
                )
                fmt_correction = (
                    "你的上一条回复无法被解析。请直接使用工具或输出最终答案。"
                    "调用工具时输出: action=工具名, action_input=参数。"
                    "任务完成时直接输出分析报告文本。"
                )
                messages.append({"role": "user", "content": fmt_correction})
                continue

            thought = parsed.get("thought", "")
            if thought:
                self.callback.on_think(thought)

            final_answer = parsed.get("final_answer", "")
            if final_answer and final_answer.strip():
                # ── 关键验证：如果需要工具但未执行，拒绝接受 final_answer ──
                if requires_tools and not tracker.has_executions():
                    no_tool_streak += 1
                    if no_tool_streak <= self.max_no_tool_streak:
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

                # ── P1 修复: final_answer 空洞化检测 ──
                hollow_check = _check_hollow_answer(
                    final_answer, user_input, tracker,
                    min_length=self.min_final_answer_length,
                    min_sections=self.min_structured_sections,
                )
                if hollow_check["is_hollow"]:
                    remaining = self.max_iterations - i
                    if remaining >= 1:
                        correction = (
                            f"❌ 你的 final_answer 不符合质量标准：{hollow_check['reason']}\n\n"
                            f"请基于已收集的所有数据，直接交付一个完整的、结构化的最终报告。\n"
                            f"不要在回答中说'我将...'、'继续...'、'基于收集到的信息...'这类元语言。\n"
                            f"直接写出分析内容本身——就像你是一个分析师在提交报告。\n"
                            f"还剩 {remaining} 轮，请立即重新输出包含完整分析内容的 final_answer。"
                        )
                        messages.append({"role": "user", "content": correction})
                        self.callback.on_warning(f"final_answer 空洞: {hollow_check['reason']}")
                        logger.warning(f"ReAct: final_answer 空洞，要求重新合成 (剩余 {remaining} 轮)")
                        continue
                    logger.warning("ReAct: final_answer 空洞但无剩余轮次，附带警告返回")
                    warning = (
                        "\n\n⚠️ **注意**: 最终回答可能不够完整。"
                        "建议重新运行并给出更具体的分析指令。"
                    )
                    self.callback.on_finish(final_answer + warning)
                    return final_answer + warning

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

                # ── 使用统一 ToolExecutor（共享引擎级断路器，含权限检查+重试+验证）──
                from omniagent.engine.tool_executor import ToolExecutor
                executor = ToolExecutor(retry_attempts=self.tool_retry_attempts, breaker=self.breaker)
                exec_result = executor.execute(action, action_input, ctx, tracker)
                # 通知回调：成功时用通知格式，失败时用错误摘要
                notify_text = exec_result.format_notification() if exec_result.success else (exec_result.error or exec_result.summary or "")
                self.callback.on_observe(notify_text, card_data=exec_result.to_card_data())

                # 使用结构化结果格式化观察消息
                obs_msg = exec_result.format_observation()
                messages.append({"role": "user", "content": obs_msg})
                logger.debug(f"ReAct 观察: {exec_result.summary[:200] if exec_result.summary else exec_result.error}")

                # ── 追踪 spawn_agent 的 task_id 用于子 Agent 通知轮询 ──
                if action == "spawn_agent" and exec_result.success:
                    import re as _re_tid
                    tid_match = _re_tid.search(r'task_id:\s*([a-zA-Z0-9_-]+)', exec_result.summary)
                    if tid_match:
                        self._active_subagent_ids.add(tid_match.group(1))
                        logger.debug("ReAct: 追踪子 Agent task_id=%s", tid_match.group(1))

                no_tool_streak = 0
                thought_only_streak = 0
            else:
                # ── LLM 只输出了 thought，既没有 action 也没有 final_answer ──
                thought_only_streak += 1
                remaining = self.max_iterations - i

                # ── 核心修复: 非工具任务不需要 action ──
                # 当 _input_requires_tools 判定任务为纯分析/解释/顾问类时，
                # 注入 action prompt 是无意义的——LLM 没有工具可调用。
                # 直接接受 thought 文本作为最终回答，避免无效的纠正循环。
                if not requires_tools:
                    raw_response = native_response.get("raw_text", "")
                    answer = (thought or raw_response or "").strip()
                    if answer:
                        # P2-Fix5: 非工具任务也要检查空洞回答质量
                        hollow_check = _check_hollow_answer(
                            answer, user_input, tracker,
                            min_length=self.min_final_answer_length,
                            min_sections=self.min_structured_sections,
                        )
                        if hollow_check["is_hollow"]:
                            remaining = self.max_iterations - i
                            if remaining >= 1:
                                logger.warning(
                                    "ReAct: 非工具任务回答空洞 (%s)，要求重新回答",
                                    hollow_check['reason'],
                                )
                                correction = (
                                    "你的回答存在问题：{reason}\n\n"
                                    "请直接回答用户的问题，给出完整、有实质内容的回答。\n"
                                    "不要用'我将...'、'基于...'这类元语言开头。"
                                ).format(reason=hollow_check['reason'])
                                messages.append({"role": "user", "content": correction})
                                self.callback.on_warning(
                                    f"非工具任务回答空洞: {hollow_check['reason']}"
                                )
                                continue
                            logger.warning(
                                "ReAct: 非工具任务回答空洞但无剩余轮次，附带警告返回"
                            )
                            warning = "\n\n[注意] 回答可能不够完整。"
                            self.callback.on_finish(answer + warning)
                            return answer + warning
                        logger.info("ReAct: 非工具任务，接受 thought 作为 final_answer")
                        self.callback.on_finish(answer)
                        return answer
                    # thought 为空，进入下一轮让 LLM 再试一次
                    logger.warning("ReAct: 非工具任务 thought 为空，进入下一轮")
                    continue

                # ── 以下逻辑仅对需要工具的任务生效 ──

                # 连续 3 次 thought-only + 有工具执行记录 → 怜悯编译
                if thought_only_streak >= 3 and tracker.has_executions():
                    logger.warning(
                        f"ReAct: 连续 {thought_only_streak} 次 thought-only，"
                        f"纠正无效，触发怜悯编译"
                    )
                    self.callback.on_warning(
                        f"连续 {thought_only_streak} 次 thought-only 纠正无效，直接编译分析报告"
                    )
                    compiled = self._mercy_compile(messages, tracker, user_input)
                    self.callback.on_finish(compiled)
                    return compiled

                if remaining >= 1 and tracker.has_executions():
                    # 已经用过工具但还在输出纯 thought → 要求合成 final_answer
                    if remaining <= 1 or thought_only_streak >= 2:
                        # 最后一轮 / 连续2次 thought-only — 给具体模板，强制输出
                        obs_summary = _build_observation_summary(messages, tracker)
                        correction = (
                            f"🛑 这是最后通牒！你已连续 {thought_only_streak} 次只输出 thought 而不采取行动。\n\n"
                            f"## 你已收集的数据\n{obs_summary}\n\n"
                            "## 你必须立即做的\n"
                            "直接输出 final_answer，格式如下：\n"
                            '```json\n'
                            '{"thought": "基于以上数据做最终总结", '
                            '"final_answer": "## 分析报告\\n\\n'
                            '### 1. 项目概述\\n（基于README和代码的实际内容）\\n\\n'
                            '### 2. 技术栈\\n（列出具体框架和版本）\\n\\n'
                            '### 3. 架构分析\\n（模块划分和职责）\\n\\n'
                            '### 4. 代码质量\\n优点：...\\n问题：...\\n\\n'
                            '### 5. 改进建议\\n1. ...\\n2. ..."}\n'
                            '```\n'
                            "❌ 不要只输出 thought！❌ 不要调用工具！直接输出上面的 JSON！"
                        )
                    else:
                        correction = (
                            "❌ 你的上一条回复只有 thought 字段，没有 action 也没有 final_answer。\n\n"
                            "你必须做出选择：\n"
                            "1. 如果需要继续执行操作 → 输出 action + action_input\n"
                            "2. 如果任务已完成 → 输出 final_answer 直接交付最终报告\n\n"
                            f"还剩 {remaining} 轮。如果你已经收集了足够的数据，请立即输出 final_answer。\n"
                            "final_answer 必须包含完整的分析内容，不要描述'我将要做什么'。"
                        )
                    messages.append({"role": "user", "content": correction})
                    self.callback.on_warning("LLM 仅输出 thought 无 action/final_answer，要求明确表态")
                    logger.warning(f"ReAct: thought-only 输出，注入选择提示 (剩余 {remaining} 轮)")
                    continue
                if remaining >= 1:
                    # 还没用过工具 → 要求开始行动
                    correction = (
                        "❌ 你的上一条回复只有 thought 字段，没有 action 也没有 final_answer。\n\n"
                        "请立即采取行动：用 action + action_input 调用工具开始执行任务，"
                        "或者如果你的任务不需要工具，直接输出 final_answer。\n"
                        "不要只输出 thought 而不采取任何行动。"
                    )
                    messages.append({"role": "user", "content": correction})
                    self.callback.on_warning("LLM 仅输出 thought 无 action/final_answer (无工具执行)，要求行动")
                    logger.warning("ReAct: thought-only 输出 (无工具执行)，注入行动提示")
                    continue
                # 无剩余轮次，尝试从最后观察中提取
                last_obs = _extract_last_observation(messages)
                if last_obs and len(last_obs) > 50:
                    result = f"达到最大迭代次数，以下是最后执行结果：\n\n{last_obs[:2000]}"
                else:
                    result = thought or response_text.strip() or "任务已执行，但未生成明确的回复内容。"
                self.callback.on_finish(result)
                return result

        # 达到最大迭代次数 — 强制编译观察摘要
        if tracker.has_executions():
            # 有工具执行记录 → 编译结构化摘要
            compiled = _compile_exhaustion_report(tracker, messages, self.max_iterations)
            self.callback.on_warning(f"达到最大迭代次数，已自动编译 {len(tracker.calls)} 条观察记录")
            self.callback.on_finish(compiled)
            return compiled

        last_obs = _extract_last_observation(messages)
        if last_obs and len(last_obs) > 100:
            msg = (
                f"⚠️ 达到最大迭代次数 ({self.max_iterations})。\n\n"
                f"基于收集到的数据，以下自动生成摘要：\n\n"
                f"{last_obs[:3000]}\n\n"
                f"💡 提示：请重新运行任务以获取更完整的分析结果。"
            )
        elif last_obs and len(last_obs) > 50:
            msg = f"达到最大迭代次数 ({self.max_iterations})，以下是最后的执行结果：\n\n{last_obs[:2000]}"
        else:
            msg = f"达到最大迭代次数 ({self.max_iterations})，未能得出最终答案。请尝试简化问题或使用更具体的指令。"
        self.callback.on_warning(msg)
        self.callback.on_finish(msg)
        return msg

    def _mercy_compile(
        self,
        messages: list[dict],
        tracker: ToolExecutionTracker,
        user_input: str,
    ) -> str:
        """怜悯编译：当 LLM 在 ReAct 循环中连续 thought-only 纠正无效时，
        用一次独立的 LLM 调用（无 ReAct 格式约束）直接生成分析报告。

        这解决了 LLM 在压力下无法遵守 JSON 格式的深层问题——
        新调用不受之前纠正消息的干扰，可以自由输出自然语言。
        """
        # 提取所有收集到的数据
        data_parts = []
        for idx, call in enumerate(tracker.calls, 1):
            if call.success and call.result_summary:
                data_parts.append(
                    f"### 工具 #{idx}: {call.tool_name}\n"
                    f"参数: {json.dumps(call.params, ensure_ascii=False)[:300]}\n"
                    f"结果: {call.result_summary[:1000]}"
                )
            elif not call.success:
                data_parts.append(
                    f"### 工具 #{idx}: {call.tool_name} (失败)\n"
                    f"错误: {call.error or '未知错误'}"
                )

        collected_data = "\n\n---\n\n".join(data_parts[:15])  # 最多 15 条工具结果

        # 构建一次性的分析请求（无 ReAct JSON 格式约束）
        compile_messages = [
            {
                "role": "system",
                "content": (
                    "你是一个专业的代码分析报告撰写专家。"
                    "请基于提供的工具执行结果，撰写一份完整的分析报告。"
                    "用 Markdown 格式，直接输出报告内容，不要描述你在做什么。"
                    "用中文。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"## 原始任务\n{user_input}\n\n"
                    f"## 工具执行结果（{len(tracker.calls)} 次调用）\n"
                    f"{collected_data}\n\n"
                    "## 要求\n"
                    "请基于以上真实数据撰写完整的分析报告，包含：\n"
                    "1. 项目概述\n"
                    "2. 技术栈\n"
                    "3. 架构分析\n"
                    "4. 代码质量评估（优点 + 问题）\n"
                    "5. 改进建议（至少 3 条具体建议）\n\n"
                    "直接输出报告，不要写'我将...'、'基于...'等元语言。"
                ),
            },
        ]

        # P3-Fix9: 主模型 + 降级模型 + 丰富耗尽报告
        for model_idx, model_id in enumerate(self.model_priority[:2]):  # 最多尝试2个模型
            try:
                result = chat_completion(
                    model_id,
                    compile_messages,
                    max_tokens=8192,
                    temperature=0.3,
                )
                if result and len(result.strip()) > 100 and _is_substantive_answer(result):
                    return result
                logger.warning(
                    f"怜悯编译: 模型 #{model_idx} 输出无效 (len=%d)", len(result.strip()) if result else 0,
                )
            except Exception as e:
                logger.warning(f"怜悯编译: 模型 #{model_idx} ({model_id}) 失败: {e}")

        # 所有 LLM 失败 — 回退到丰富的耗尽报告
        compiled = _compile_exhaustion_report(tracker, messages, self.max_iterations)
        # 在报告前添加结构化摘要
        summary_prefix = f"## 任务执行摘要\n\n共执行 {len(tracker.calls)} 次工具调用"
        if tracker.has_executions():
            success_count = sum(1 for c in tracker.calls if c.success)
            summary_prefix += f"（{success_count} 成功, {len(tracker.calls) - success_count} 失败）"
            summary_prefix += "\n\n### 执行记录\n"
            for c in tracker.calls[:10]:
                icon = "OK" if c.success else "FAIL"
                params_str = ", ".join(f"{k}={str(v)[:60]}" for k, v in c.params.items())
                summary_prefix += f"\n- [{icon}] **{c.tool_name}**({params_str})"
                if c.result_summary:
                    summary_prefix += f"\n  -> {c.result_summary[:200]}"
        return summary_prefix + "\n\n---\n\n" + compiled

    def _call_llm(self, messages: list[dict[str, str]], max_tokens: int = _REACT_LOOP_MAX_TOKENS) -> str:
        """调用 LLM，支持多模型 fallback。"""
        last_error = None
        for model_id in self.model_priority:
            try:
                return chat_completion(model_id, messages, max_tokens=max_tokens, temperature=0.3)
            except Exception as e:
                last_error = e
                logger.warning(f"模型 {model_id} 失败: {e}，尝试下一个...")
        msg = f"所有模型均调用失败: {last_error}"
        raise RuntimeError(msg)

    def _call_llm_native(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        """调用 LLM — 优先使用原生工具调用，回退 JSON 解析。

        这是 P0 根因修复的核心：通过 API 的 function calling 机制获取
        结构化的 tool_calls，消除对 LLM 文本输出的 JSON 解析依赖。

        Returns:
            兼容 parse_react 返回格式的 dict，额外包含 "raw_text" 字段
        """
        from omniagent.utils.llm_client import chat_completion_with_tools, NativeToolResponse

        last_text = ""
        for model_id in self.model_priority:
            try:
                native: NativeToolResponse = chat_completion_with_tools(
                    model_id, messages,
                    tools=self.tools,
                    max_tokens=_REACT_LOOP_MAX_TOKENS,
                    temperature=0.3,
                )

                # ── Trace: LLM 调用 ──
                if self._trace:
                    self._trace.emit_llm("CORE→LLM", model=model_id,
                        request_preview=str(messages[-1].get("content", ""))[:500],
                        response_preview=native.text[:500] if native.text else "[tool_calls]")

                # ── 优先: 原生 tool_calls（完全不需要 JSON 解析）──
                if native.has_tool_calls:
                    tc = native.first_tool_call()
                    logger.debug(
                        f"ReAct 原生工具调用: {tc['name']}({str(tc['arguments'])[:200]})"
                    )
                    if self._trace:
                        self._trace.emit_event(kind="agent.tool_call",
                            tool_name=tc["name"], tool_args=str(tc["arguments"])[:500])
                    return {
                        "thought": f"调用工具 {tc['name']}",
                        "action": tc["name"],
                        "action_input": tc["arguments"],
                        "raw_text": native.text or f"tool_call: {tc['name']}",
                    }

                # ── 次优: 文本中包含 final_answer ──
                if native.text and native.text.strip():
                    last_text = native.text
                    # 尝试解析文本中的 JSON（兼容不支持 function calling 的模型）
                    parsed = parse_react(native.text)
                    if not parsed.get("parse_error"):
                        # 成功解析 → 返回
                        parsed["raw_text"] = native.text
                        return parsed
                    # 解析失败但文本有意义 → 检查是否为实质性回答
                    if _is_substantive_answer(native.text):
                        return {
                            "thought": "任务完成",
                            "final_answer": native.text,
                            "raw_text": native.text,
                        }
                    # 计划描述/空洞文本 → 当作解析错误，让引擎纠正
                    logger.warning(
                        "_call_llm_native: 文本不是实质性回答 (len=%d): %.100s",
                        len(native.text), native.text.strip(),
                    )

                break  # 模型成功但不含有效响应，跳出尝试下一个

            except Exception as e:
                logger.warning(f"模型 {model_id} 原生工具调用失败: {e}，尝试下一个...")
                continue

        # ── 最终回退: 如果所有原生调用都失败，尝试传统 JSON 调用 ──
        if not last_text:
            try:
                last_text = self._call_llm(messages)
            except Exception:
                pass

        parsed = parse_react(last_text) if last_text else {"parse_error": True, "raw_text": ""}
        parsed["raw_text"] = last_text
        return parsed

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
        """执行工具并返回观察结果文本。

        委托给统一的 ToolExecutor，避免与其他引擎重复实现
        断路器/重试/参数验证逻辑。
        """
        # 验证工具存在
        tool_info = self.tools.get(action)
        if not tool_info and action in _DYNAMIC_TOOLS:
            tool_info = _DYNAMIC_TOOLS[action]
        if not tool_info:
            error_msg = f"错误: 未知工具 '{action}'，可用工具: {list(self.tools.keys()) + list(_DYNAMIC_TOOLS.keys())}"
            if tracker:
                tracker.record(action, action_input, False, error_msg, error=error_msg)
            return error_msg

        # 委托给统一 ToolExecutor
        from omniagent.engine.tool_executor import ToolExecutor
        executor = ToolExecutor(retry_attempts=self.tool_retry_attempts)
        result = executor.execute(action, action_input, context, tracker)

        return result.summary if result.success else (result.error or result.summary)

    @staticmethod
    def _input_requires_tools(text: str) -> bool:
        """判断用户输入是否大概率需要工具执行。

        正向关键词匹配 + 负向排除（纯分析/咨询类不需要工具）。
        """
        # ── 负向排除: 纯分析/诊断/顾问类任务不需要工具 ──
        analysis_only = [
            "分析", "诊断", "评估", "审查", "评审", "review",
            "差距", "建议", "方案", "推荐", "意见",
            "是什么", "什么是", "为什么", "如何理解",
            "总结", "概括", "解释", "说明一下",
            "有什么", "有哪些", "还有哪些",
            "检查", "核对", "验证", "核查", "校验", "审视", "审阅",
            "是否符合", "是否一致", "是否合理", "是否匹配",
        ]
        text_lower = text.lower()
        # 如果任务明确是 pure analysis，不需要工具
        is_pure_analysis = any(kw in text_lower for kw in analysis_only)
        has_action_marker = any(kw in text_lower for kw in [
            "做", "执行", "跑", "创建", "写入", "修改", "删除", "克隆",
            "do ", "make", "run", "build", "fix", "write", "clone",
        ])
        # ── 检查是否需要读取文件内容（章节/文件/大纲/剧情等实体）──
        # 即使任务是"检查/分析"，如果需要读取实际文件内容，仍然需要工具
        needs_file_read = any(kw in text_lower for kw in [
            "章", "章节", "文件", "大纲", "剧情", "代码", "第", "内容",
            "chapter", "file", "code", "content",
            ".py", ".js", ".ts", ".md", ".txt", ".json", ".yaml",
        ])
        if is_pure_analysis and not has_action_marker and not needs_file_read:
            return False

        # ── 正向关键词 ──
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
        return any(kw in text_lower for kw in tool_keywords)


# ── 自动注册内置工具到 response_adapter ──
register_tools_from_dict(BUILTIN_TOOLS)
