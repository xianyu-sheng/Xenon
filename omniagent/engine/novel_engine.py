"""
Novel Engine — 小说创作专用引擎。

融合 AI 创作社区最佳实践（SillyTavern、KoboldAI、Sudowrite、Novelcrafter）：
- 角色卡系统（性格/外貌/动机/弧线）
- 世界观词条管理
- 场景级生成（目标-冲突-结局框架）
- 多操作模式（大纲/写作/续写/润色/扩写/角色/世界观/分析）
- 项目持久化（.novel/ 目录）
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from omniagent.engine.callbacks import EngineCallback
from omniagent.engine.context import AgentContext
from omniagent.engine.tool_tracker import ToolExecutionTracker
from omniagent.nodes.tool_node import ToolNode
from omniagent.utils.llm_client import chat_completion
from omniagent.utils.response_adapter import parse_react

logger = logging.getLogger(__name__)

# ── 小说创作系统提示 ──────────────────────────────────────────

NOVEL_SYSTEM_PROMPT = """你是一位资深小说创作助手，精通叙事技巧、人物塑造、世界观构建和文字打磨。

## 核心写作原则

1. **展示而非叙述** (Show, don't tell) — 用动作、对话、感官细节展现角色和情感，而非直接陈述
2. **场景框架** — 每个场景有：目标（角色想要什么）→ 冲突（阻碍是什么）→ 结局（改变了什么）
3. **角色驱动** — 角色的行为由其性格、动机和背景驱动，而非剧情需要
4. **感官沉浸** — 调动视觉、听觉、触觉、嗅觉、味觉，让读者身临其境
5. **对话潜台词** — 角色说的和想的往往不同，潜台词创造张力
6. **节奏控制** — 紧张与舒缓交替，长句营造沉思感，短句制造紧迫感
7. **一致性** — 角色性格、世界观规则、时间线必须前后一致

## 支持的操作

根据用户请求自动识别操作类型：

### outline（大纲规划）
生成或修改故事大纲。支持结构框架：
- 三幕式：建置 → 对抗 → 解决
- 英雄之旅：冒险召唤 → 试炼 → 回归
- Save the Cat：15 个节拍
- 雪花法：一句话 → 一段 → 四段 → 章节列表

### write（写新章节/场景）
按大纲写新内容。要求：
- 明确 POV（视角角色）
- 明确场景目标和冲突
- 控制篇幅（建议 1500-3000 字/场景）
- 结尾留悬念或转折

### continue（续写）
从已有内容自然延续。要求：
- 先 read_file 读取已有内容
- 保持风格、语气、节奏一致
- 推进情节，不重复已有内容

### revise（润色修改）
改进已有文本。可指定方向：
- 风格调整（更简洁/更华丽/更口语化）
- 对话优化（更自然/更有个性）
- 节奏调整（加快/放慢）
- 增加感官细节

### expand（扩写）
将简略段落扩展为丰富描写。增加：
- 环境细节
- 角色内心活动
- 感官描写
- 动作分解

### character（角色创建/分析）
创建角色卡或分析已有角色。角色卡包含：
- 基本信息（姓名、年龄、外貌）
- 性格特征（至少 3 个正面 + 2 个缺陷）
- 动机与欲望（想要什么 vs 需要什么）
- 背景故事（塑造性格的关键事件）
- 角色弧线（从 A 状态到 B 状态的转变）
- 与其他角色的关系

### worldbuild（世界观构建）
创建或扩展世界观设定：
- 地理环境
- 社会结构/政治体系
- 历史事件/传说
- 魔法/科技体系（如适用）
- 文化习俗/禁忌
- 经济体系

### analyze（分析）
分析已有文本：
- 情节逻辑是否通顺
- 角色行为是否一致
- 节奏是否合理
- 文字质量评估
- 改进建议

## 项目结构

小说项目存储在 .novel/ 目录：
- .novel/characters.json — 角色卡
- .novel/world.json — 世界观设定
- .novel/outline.md — 故事大纲
- .novel/style.md — 风格指南
- .novel/summary.md — 已完成内容摘要
- chapters/ — 章节文件目录

## 工具使用规则

1. **写操作前先读** — 修改章节前先 read_file 了解已有内容
2. **保存到文件** — 所有创作内容用 write_file 保存到 .novel/ 或 chapters/
3. **参数名用标准名** — file_path（不是 path）、content（不是 text）
4. **一个 JSON 只调用一个工具**
5. **严禁发明工具** — 只使用下方列出的工具

## 输出格式

每次回复只输出一个 JSON：

调用工具时：
```json
{{"thought": "分析当前任务，决定下一步", "action": "工具名", "action_input": {{"参数名": "值"}}}}
```

任务完成时：
```json
{{"thought": "总结创作成果", "final_answer": "给用户的最终回答，包含创作内容"}}
```

## 可用工具（完整且唯一）

{tools_desc}

## 运行环境

- 操作系统: {os_info}
- 工作目录: 通过命令 `Get-Location`（Windows）或 `pwd`（Linux/macOS）获取
"""

# 小说引擎使用的工具（比编程引擎少，专注写作相关）
NOVEL_TOOLS = {
    "read_file": {
        "name": "read_file",
        "description": "读取文件内容。用于读取已有章节、角色卡、大纲等。",
        "params": {"file_path": "文件路径", "start_line": "起始行号（可选）", "max_lines": "读取行数（可选）"},
    },
    "write_file": {
        "name": "write_file",
        "description": "将内容写入文件。用于保存章节、角色卡、大纲等。自动创建父目录。",
        "params": {"file_path": "文件路径", "content": "要写入的完整内容"},
    },
    "edit_file": {
        "name": "edit_file",
        "description": "精确替换文件中的文本。用于修改章节中的特定段落或句子。",
        "params": {"file_path": "文件路径", "old_text": "原文（必须精确匹配）", "new_text": "替换后的新文"},
    },
    "list_files": {
        "name": "list_files",
        "description": "列出目录下的文件。用于查看项目结构和已有章节。",
        "params": {"file_path": "目录路径", "pattern": "glob 过滤（可选，如 *.md）"},
    },
    "search_files": {
        "name": "search_files",
        "description": "在文件中搜索关键词。用于查找角色名出现位置、检查一致性等。",
        "params": {"file_path": "搜索根目录", "search_pattern": "搜索关键词", "file_filter": "文件过滤（可选）"},
    },
    "create_directory": {
        "name": "create_directory",
        "description": "创建目录。用于初始化小说项目结构。",
        "params": {"file_path": "目录路径"},
    },
    "command": {
        "name": "command",
        "description": "执行终端命令。用于字数统计、文件管理等辅助操作。",
        "params": {"action": "要执行的命令"},
    },
}


class NovelEngine:
    """小说创作专用引擎。"""

    def __init__(
        self,
        model_priority: list[str],
        *,
        max_iterations: int = 15,
        system_prompt: str | None = None,
        callback: EngineCallback | None = None,
        project_dir: str = ".novel",
    ) -> None:
        self.model_priority = model_priority
        self.max_iterations = max_iterations
        self.tools = NOVEL_TOOLS
        self.callback = callback or EngineCallback()
        self.project_dir = project_dir
        self.system_prompt = system_prompt or self._build_system_prompt()

    def _build_system_prompt(self) -> str:
        import sys
        tools_desc = "\n".join(
            f"- {t['name']}: {t['description']} (参数: {t['params']})"
            for t in self.tools.values()
        )
        if sys.platform == "win32":
            os_info = "Windows（PowerShell）"
        elif sys.platform == "darwin":
            os_info = "macOS（bash）"
        else:
            os_info = "Linux（bash）"

        return NOVEL_SYSTEM_PROMPT.format(tools_desc=tools_desc, os_info=os_info)

    def run(self, user_input: str, context: AgentContext | None = None) -> str:
        """
        执行小说创作循环。

        Args:
            user_input: 用户输入
            context: 可选的共享上下文

        Returns:
            创作结果文本
        """
        ctx = context or AgentContext()
        tracker = ToolExecutionTracker()
        messages = [{"role": "system", "content": self.system_prompt}]

        # 注入对话历史
        history = ctx.get_conversation_messages()
        if history:
            recent = [m for m in history if m.get("role") != "system"][-10:]
            messages.extend(recent)
            logger.info(f"Novel 注入 {len(recent)} 条对话历史")

        # 注入项目上下文（角色、世界观等）
        project_ctx = self._load_project_context()
        if project_ctx:
            messages.append({"role": "system", "content": f"## 当前小说项目状态\n\n{project_ctx}"})

        messages.append({"role": "user", "content": user_input})

        for i in range(self.max_iterations):
            logger.info(f"Novel 迭代 {i + 1}/{self.max_iterations}")

            response = self._call_llm(messages)
            messages.append({"role": "assistant", "content": response})

            parsed = self._parse_response(response)

            thought = parsed.get("thought", "")
            if thought:
                self.callback.on_think(thought)

            if parsed.get("final_answer"):
                answer = parsed["final_answer"]
                if tracker.has_executions():
                    summary = tracker.execution_summary()
                    logger.info(f"Novel 工具执行摘要: {summary}")
                self.callback.on_finish(answer)
                return answer

            if "action" in parsed:
                action = parsed["action"]
                action_input = parsed.get("action_input", {})

                logger.info(f"Novel 思考: {thought}")
                logger.info(f"Novel 行动: {action}({action_input})")
                self.callback.on_act(action, action_input)

                observation = self._execute_tool(action, action_input, ctx, tracker)
                self.callback.on_observe(observation)

                obs_msg = f"Observation: {observation}"
                messages.append({"role": "user", "content": obs_msg})
                logger.info(f"Novel 观察: {observation[:200]}")
            else:
                result = parsed.get("thought", response)
                self.callback.on_finish(result)
                return result

        msg = f"达到最大迭代次数 ({self.max_iterations})，创作暂停。"
        self.callback.on_warning(msg)
        self.callback.on_finish(msg)
        return msg

    def _call_llm(self, messages: list[dict[str, str]], max_tokens: int = 8192) -> str:
        """调用 LLM，支持多模型 fallback。创意写作用较高 temperature。"""
        last_error = None
        for model_id in self.model_priority:
            try:
                return chat_completion(
                    model_id, messages,
                    max_tokens=max_tokens,
                    temperature=0.8,  # 创意写作用更高 temperature
                )
            except Exception as e:
                last_error = e
                logger.warning(f"模型 {model_id} 失败: {e}，尝试下一个...")
        raise RuntimeError(f"所有模型均调用失败: {last_error}")

    def _parse_response(self, response: str) -> dict[str, Any]:
        """解析 LLM 输出。"""
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
                f"novel_{action}",
                action_type=action,
                **action_input,
            )
            result = node.execute(context)
            logger.info(f"工具结果: {str(result)[:200]}")

            success = result.get("success", False)
            error = result.get("error")

            if success:
                summary = ""
                for key in ("content", "stdout", "output", "files"):
                    if key in result and result[key]:
                        val = result[key]
                        if isinstance(val, list):
                            summary = "\n".join(str(v) for v in val[:50])
                        else:
                            summary = str(val)[:5000]
                        break
                if not summary:
                    summary = str(result)[:5000]

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

    def _load_project_context(self) -> str:
        """加载 .novel/ 目录下的项目上下文。"""
        parts = []
        novel_dir = Path(self.project_dir)

        for filename, label in [
            ("characters.json", "角色卡"),
            ("world.json", "世界观设定"),
            ("outline.md", "故事大纲"),
            ("style.md", "风格指南"),
            ("summary.md", "内容摘要"),
        ]:
            filepath = novel_dir / filename
            if filepath.exists():
                try:
                    content = filepath.read_text(encoding="utf-8")
                    if content.strip():
                        # 截断过长的内容
                        if len(content) > 2000:
                            content = content[:2000] + "\n... (已截断)"
                        parts.append(f"### {label}\n{content}")
                except Exception:
                    pass

        return "\n\n".join(parts) if parts else ""
