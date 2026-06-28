"""
Novel Engine — 小说创作专用引擎。

融合 AI 创作社区最佳实践（SillyTavern、KoboldAI、Sudowrite、Novelcrafter）：
- 多小说隔离（每本小说独立目录和记忆）
- 角色卡系统（性格/外貌/动机/弧线）
- 世界观词条管理
- 场景级生成（目标-冲突-结局框架）
- 多操作模式（大纲/写作/续写/润色/扩写/角色/世界观/分析）
- 创作记忆累积（context.md 随创作不断增长）
"""

from __future__ import annotations

import logging
from typing import Any

from omniagent.engine.base_engine import BaseEngine
from omniagent.engine.callbacks import EngineCallback
from omniagent.engine.context import AgentContext
from omniagent.engine.novel_manager import NovelManager
from omniagent.engine.tool_executor import _validate_tool_params
from omniagent.engine.react_engine import (
    _DEFAULT_EXPLORATION_SYNTHESIZE,
    _DEFAULT_FORCE_SYNTHESIS,
    _DEFAULT_HURRY_WARNING,
    _DEFAULT_MIN_FINAL_ANSWER_LENGTH,
    _DEFAULT_TOOL_RETRY_ATTEMPTS,
    _check_hollow_answer,
    _compile_exhaustion_report,
)
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

## 多小说管理

你管理着多本小说，每本小说有独立的目录和记忆。当前活跃的小说信息已注入上下文。

### 识别小说
- 用户说"切换到xxx"、"打开xxx"时，切换到对应小说
- 用户提到某本小说的标题时，自动切换
- 如果用户没有指定，使用当前活跃的小说

### 创作记忆
- **context.md** 是你对这本小说的累积理解，包含故事核心、角色状态、关键决策
- 每次重要操作后，你必须更新 context.md，记录：
  - 这次做了什么
  - 关键决策和理由
  - 对后续创作的影响
  - 需要注意的一致性问题
- 这确保下次创作时你能无缝衔接，不走偏

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

每本小说有独立的项目目录（由系统自动管理，你不需要手动创建目录）：
- meta.json — 小说元数据
- characters.json — 角色卡
- world.json — 世界观设定
- outline.md — 故事大纲
- style.md — 风格指南
- summary.md — 已完成内容摘要
- context.md — 创作记忆（你的累积理解）
- chapters/ — 章节文件目录

## 工具使用规则

1. **写操作前先读** — 修改章节前先 read_file 了解已有内容
2. **保存到文件** — 所有创作内容用 write_file 保存到对应小说目录
3. **参数名用标准名** — file_path（不是 path）、content（不是 text）
4. **一个 JSON 只调用一个工具**
5. **严禁发明工具** — 只使用下方列出的工具
6. **更新记忆** — 重要操作后用 write_file 更新 context.md

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

# 小说引擎使用的工具
NOVEL_TOOLS = {
    "read_file": {
        "name": "read_file",
        "description": "读取文件内容。用于读取已有章节、角色卡、大纲、创作记忆等。",
        "params": {"file_path": "文件路径", "start_line": "起始行号（可选）", "max_lines": "读取行数（可选）"},
    },
    "write_file": {
        "name": "write_file",
        "description": "将内容写入文件。用于保存章节、角色卡、大纲、创作记忆等。自动创建父目录。",
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


class NovelEngine(BaseEngine):
    """小说创作专用引擎，支持多小说隔离和创作记忆累积。"""

    def __init__(
        self,
        model_priority: list[str],
        *,
        max_iterations: int = 15,
        system_prompt: str | None = None,
        callback: EngineCallback | None = None,
        novel_manager: NovelManager | None = None,
        # ── 可配置阈值 ──
        min_final_answer_length: int = _DEFAULT_MIN_FINAL_ANSWER_LENGTH,
        tool_retry_attempts: int = _DEFAULT_TOOL_RETRY_ATTEMPTS,
        hurry_warning_threshold: int = _DEFAULT_HURRY_WARNING,
        force_synthesis_threshold: int = _DEFAULT_FORCE_SYNTHESIS,
        exploration_budget_synthesize: int = _DEFAULT_EXPLORATION_SYNTHESIZE,
    ) -> None:
        super().__init__(model_priority=model_priority, callback=callback)
        self.max_iterations = max_iterations
        self.tools = NOVEL_TOOLS
        self.manager = novel_manager or NovelManager()

        # ── 可配置阈值（必须在 _build_system_prompt 之前设置）──
        self.min_final_answer_length = min_final_answer_length
        self.tool_retry_attempts = tool_retry_attempts
        self.hurry_warning_threshold = hurry_warning_threshold
        self.force_synthesis_threshold = force_synthesis_threshold
        self.exploration_budget_synthesize = exploration_budget_synthesize

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

        流程：
        1. 自动识别用户指的是哪本小说
        2. 加载该小说的完整上下文
        3. 执行创作任务
        4. 更新创作记忆
        """
        ctx = context or AgentContext()
        tracker = ToolExecutionTracker()

        # ── 1. 识别小说 ──
        project = self.manager.detect_novel(user_input)
        if not project:
            # 没有小说，也没有识别到
            novels = self.manager.list_novels()
            if not novels:
                return (
                    "你还没有创建任何小说项目。\n\n"
                    "使用 `/novel init <名称>` 创建一本新小说，例如：\n"
                    "  `/novel init 星际迷途 科幻`\n"
                    "  `/novel init 月下独行 武侠`"
                )
            novel_list = "\n".join(
                f"  - **{n['title']}** ({n['genre'] or '未分类'}) — {n['chapters']} 章, {n['words']} 字"
                for n in novels
            )
            return (
                f"你有多本小说，请指定要操作哪一本：\n\n{novel_list}\n\n"
                "使用 `/novel switch <名称>` 切换，或在输入中提到小说标题。"
            )

        slug = project.slug
        logger.debug(f"识别到小说: {project.title} ({slug})")

        # ── 2. 构建消息 ──
        messages = [{"role": "system", "content": self.system_prompt}]

        # 注入项目上下文（这是核心 — AI 的完整创作记忆）
        project_ctx = project.get_all_context()
        if project_ctx:
            messages.append({
                "role": "system",
                "content": f"## 当前小说: {project.title}\n\n{project_ctx}",
            })

        # 注入对话历史
        self._inject_history(messages, ctx)

        messages.append({"role": "user", "content": user_input})

        # ── 3. 执行创作循环 ──
        for i in range(self.max_iterations):
            logger.debug(f"Novel 迭代 {i + 1}/{self.max_iterations}")

            # ── 接近上限时注入合成提示 ──
            remaining = self.max_iterations - i
            if remaining <= self.force_synthesis_threshold and tracker.has_executions():
                hurry_msg = (
                    f"🛑 仅剩 {remaining} 轮！请立即输出 final_answer 交付创作结果。\n"
                    f"不要再调用工具——基于已收集的数据直接完成创作。"
                )
                messages.append({"role": "user", "content": hurry_msg})
                logger.info(f"Novel: 注入强制合成提示 (剩余 {remaining} 轮)")
            elif remaining <= self.hurry_warning_threshold and not tracker.has_executions():
                hurry_msg = (
                    f"⚠️ 仅剩 {remaining} 轮迭代机会。请立即使用工具开始创作。"
                )
                messages.append({"role": "user", "content": hurry_msg})
                logger.info(f"Novel: 注入加速提示 (剩余 {remaining} 轮)")

            response = self._call_llm(messages, max_tokens=8192, temperature=0.8)
            messages.append({"role": "assistant", "content": response})

            parsed = self._parse_response(response)

            thought = parsed.get("thought", "")
            if thought:
                self.callback.on_think(thought)

            if parsed.get("final_answer"):
                answer = parsed["final_answer"]

                # ── 空洞检测 ──
                hollow_check = _check_hollow_answer(
                    answer, user_input, tracker,
                    min_length=self.min_final_answer_length,
                )
                if hollow_check["is_hollow"]:
                    remaining = self.max_iterations - i
                    if remaining >= 1:
                        correction = (
                            f"❌ 你的 final_answer 不符合质量标准：{hollow_check['reason']}\n\n"
                            f"请直接交付完整的创作内容，不要描述'我将要做什么'。\n"
                            f"还剩 {remaining} 轮，请立即重新输出。"
                        )
                        messages.append({"role": "user", "content": correction})
                        self.callback.on_warning(f"final_answer 空洞: {hollow_check['reason']}")
                        logger.warning(f"Novel: final_answer 空洞，要求重新合成 (剩余 {remaining} 轮)")
                        continue

                if tracker.has_executions():
                    summary = tracker.execution_summary()
                    logger.debug(f"Novel 工具执行摘要: {summary}")

                # ── 4. 自动更新创作记忆 ──
                self._auto_update_context(slug, user_input, answer, tracker)

                self.callback.on_finish(answer)
                return answer

            if "action" in parsed:
                action = parsed["action"]
                action_input = parsed.get("action_input", {})

                # ── 参数验证 ──
                validated = _validate_tool_params(action, action_input)
                if not validated["valid"]:
                    error_msg = f"参数错误: {validated['reason']}"
                    messages.append({"role": "user", "content": f"❌ {error_msg}\n请用正确的文件路径重试。"})
                    if tracker:
                        tracker.record(action, action_input, False, error_msg, error=error_msg)
                    self.callback.on_warning(f"参数验证失败: {error_msg[:100]}")
                    continue

                logger.debug(f"Novel 思考: {thought}")
                logger.debug(f"Novel 行动: {action}({action_input})")
                self.callback.on_act(action, action_input)

                observation = self._execute_tool(action, action_input, ctx, tracker)
                self.callback.on_observe(observation)

                obs_msg = f"Observation: {observation}"
                messages.append({"role": "user", "content": obs_msg})
                logger.debug(f"Novel 观察: {observation[:200]}")
            else:
                # ── thought-only 输出修正 ──
                remaining = self.max_iterations - i
                if remaining >= 1 and tracker.has_executions():
                    correction = (
                        "你的上一条回复只有 thought 字段，没有 action 也没有 final_answer。\n\n"
                        f"还剩 {remaining} 轮。如果你已经完成任务，请立即输出 final_answer。"
                    )
                    messages.append({"role": "user", "content": correction})
                    self.callback.on_warning("LLM 仅输出 thought 无 action/final_answer，要求明确表态")
                    continue
                # 接受 thought 前验证: 不是计划描述
                result = parsed.get("thought", response)
                try:
                    from omniagent.engine.react_engine import _is_substantive_answer
                    if not _is_substantive_answer(result) and tracker.has_executions():
                        # 有工具执行但输出仍是计划 → 尝试一次纠正
                        correction = "你的回答读起来像计划而非实际结果。请直接输出创作内容或总结。"
                        messages.append({"role": "user", "content": correction})
                        self.callback.on_warning("Novel output appears plan-like, injecting correction")
                        continue
                except ImportError:
                    pass
                self._auto_update_context(slug, user_input, result, tracker)
                self.callback.on_finish(result)
                return result

        # 达到最大迭代次数 — 强制编译观察摘要
        if tracker.has_executions():
            compiled = _compile_exhaustion_report(tracker, messages, self.max_iterations)
            self.callback.on_warning(f"达到最大迭代次数，已自动编译 {len(tracker.calls)} 条观察记录")
            self.callback.on_finish(compiled)
            return compiled

        # 尝试从最后一条消息中提取有用内容
        last_content = ""
        for m in reversed(messages):
            content = m.get("content", "") if isinstance(m, dict) else ""
            if len(content) > 50 and "Observation" not in content:
                last_content = content[:500]
                break
        if last_content:
            msg = (
                f"## 创作进度\n\n"
                f"达到最大迭代次数 ({self.max_iterations})，以下是最新内容：\n\n"
                f"{last_content}\n\n"
                f"---\n"
                f"请重新运行以继续创作。"
            )
        else:
            msg = (
                f"## 创作暂停\n\n"
                f"达到最大迭代次数 ({self.max_iterations})，创作暂停。\n"
                f"已执行 {len(tracker.calls)} 次工具操作。\n"
                f"请重新运行并给出更具体的创作指令。"
            )
        self.callback.on_warning(msg)
        self.callback.on_finish(msg)
        return msg

    def _auto_update_context(
        self,
        slug: str,
        user_input: str,
        answer: str,
        tracker: ToolExecutionTracker,
    ) -> None:
        """
        自动更新创作记忆。

        根据操作类型和执行结果，将关键信息追加到 context.md。
        """
        # 提取操作类型
        operation = "创作操作"
        input_lower = user_input.lower()
        if any(k in input_lower for k in ["大纲", "outline", "规划"]):
            operation = "大纲规划"
        elif any(k in input_lower for k in ["写", "write", "新章", "第一章"]):
            operation = "章节写作"
        elif any(k in input_lower for k in ["续写", "continue", "继续"]):
            operation = "续写"
        elif any(k in input_lower for k in ["润色", "revise", "修改"]):
            operation = "润色修改"
        elif any(k in input_lower for k in ["扩写", "expand"]):
            operation = "扩写"
        elif any(k in input_lower for k in ["角色", "character"]):
            operation = "角色管理"
        elif any(k in input_lower for k in ["世界", "world"]):
            operation = "世界观构建"
        elif any(k in input_lower for k in ["分析", "analyze"]):
            operation = "文本分析"

        # 构建记录
        detail_parts = []
        detail_parts.append(f"**用户请求**: {user_input[:200]}")

        if tracker.has_executions():
            execs = tracker.get_history()
            tool_summary = []
            for ex in execs[:5]:
                status = "✅" if ex.get("success") else "❌"
                tool_summary.append(f"  - {status} {ex.get('tool', '?')}")
            detail_parts.append("**工具执行**:\n" + "\n".join(tool_summary))

        # 截取回答摘要（避免记忆文件过大）
        answer_summary = answer[:500] if len(answer) > 500 else answer
        detail_parts.append(f"**结果摘要**: {answer_summary}")

        self.manager.update_context(slug, operation, "\n".join(detail_parts))

    def _parse_response(self, response: str) -> dict[str, Any]:
        return parse_react(response)

    def _execute_tool(
        self,
        action: str,
        action_input: dict,
        context: AgentContext,
        tracker: ToolExecutionTracker | None = None,
    ) -> str:
        tool_info = self.tools.get(action)
        if not tool_info:
            error_msg = f"错误: 未知工具 '{action}'，可用工具: {list(self.tools.keys())}"
            if tracker:
                tracker.record(action, action_input, False, error_msg, error=error_msg)
            return error_msg

        max_attempts = self.tool_retry_attempts
        last_error_msg = ""

        for attempt in range(max_attempts):
            try:
                action_input = ToolNode.normalize_params(action_input)
                logger.debug(f"执行工具: {action}, 参数: {action_input} (attempt {attempt + 1}/{max_attempts})")
                node = ToolNode(
                    f"novel_{action}",
                    action_type=action,
                    **action_input,
                )
                result = node.execute(context)
                logger.debug(f"工具结果: {str(result)[:200]}")

                success = result.get("success", False)
                error = result.get("error")

                if success:
                    summary = ""
                    for key in ("content", "stdout", "output", "files"):
                        if result.get(key):
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
                last_error_msg = f"工具执行失败: {error or result}"
                error_str = str(error) if error else str(result)
                if attempt < max_attempts - 1:
                    logger.warning(f"工具 {action} 失败，准备重试 ({attempt + 1}/{max_attempts}): {error_str[:100]}")
                    continue
                if tracker:
                    tracker.record(action, action_input, False, last_error_msg, error=str(error))
                return last_error_msg

            except Exception as e:
                last_error_msg = f"工具执行异常: {e}"
                logger.error(f"工具执行异常: {action}({action_input}) -> {e}")
                if attempt < max_attempts - 1:
                    logger.warning(f"工具 {action} 异常，准备重试 ({attempt + 1}/{max_attempts}): {e}")
                    continue
                if tracker:
                    tracker.record(action, action_input, False, last_error_msg, error=str(e))
                return last_error_msg

        return last_error_msg
