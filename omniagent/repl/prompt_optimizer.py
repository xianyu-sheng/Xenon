"""
Prompt Optimizer — 输入指令重构器。

将用户的口语化输入转换为结构化的高质量 prompt，
提升底层模型的理解准确度和输出质量。

工作方式：
1. 模式匹配 — 识别用户意图（写代码、解释、调试、重构等）
2. 结构化重组 — 按照最佳实践模板重新组织 prompt
3. 上下文注入 — 自动注入相关的历史上下文和项目信息
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass
class PromptTemplate:
    """Prompt 模板。"""

    intent: str             # 意图标识
    trigger_patterns: list[str]  # 触发词/正则
    template: str           # 结构化模板
    system_hint: str        # 对应的 system prompt 补充


# ── 意图识别规则 ──────────────────────────────────────────

TEMPLATES: list[PromptTemplate] = [
    # 调试/修复（最高优先级，因为有明显的错误关键词）
    PromptTemplate(
        intent="debug",
        trigger_patterns=[
            r"(?:报错|出错|错误|异常|bug|问题|不工作|运行不了|失败|崩溃)",
            r"(?:fix|debug|error|exception|bug|issue|broken|fail|crash)",
            r"怎么解决",
            r"为什么.*(?:不行|不能|失败|报错)",
        ],
        template=(
            "## 问题描述\n{task}\n\n"
            "## 期望行为\n（请描述期望的正确行为）\n\n"
            "## 调试要求\n"
            "1. 分析错误根因\n"
            "2. 提供修复方案\n"
            "3. 给出修复后的完整代码\n"
            "4. 说明修复原理"
        ),
        system_hint="你是一个调试专家。请先分析错误根因，再给出修复方案和代码。",
    ),

    # 测试（在写代码之前，因为"帮我写测试"不应匹配写代码）
    PromptTemplate(
        intent="write_test",
        trigger_patterns=[
            r"(?:写|编写|生成|创建).*(?:测试|单测|单元测试|测试用例)",
            r"(?:write|create|generate|build).*(?:test|spec|testing)",
            r"怎么测试",
        ],
        template=(
            "## 待测试代码\n{task}\n\n"
            "## 测试要求\n"
            "1. 覆盖正常流程\n"
            "2. 覆盖边界情况\n"
            "3. 覆盖异常情况\n"
            "4. 使用 pytest 框架\n"
            "5. 每个测试用例有清晰的命名和注释"
        ),
        system_hint="你是一个测试专家。请编写全面的单元测试，覆盖正常流程、边界情况和异常情况。",
    ),

    # 转换/迁移（在解释之前，因为"转成"不应匹配解释）
    PromptTemplate(
        intent="convert",
        trigger_patterns=[
            r"(?:转|迁移|改).*(?:成|到|为)",
            r"(?:convert|translate|migrate|port)",
            r"从.*(?:转|迁移|换成)",
            r"把.*(?:改成|转成|转换成|迁移到)",
        ],
        template=(
            "## 源内容\n{task}\n\n"
            "## 转换要求\n"
            "1. 保持功能完全一致\n"
            "2. 使用目标语言/框架的最佳实践\n"
            "3. 处理差异点并说明"
        ),
        system_hint="你是一个代码迁移专家。请确保转换后的代码功能完全一致，并使用目标平台的最佳实践。",
    ),

    # 分析/审查（在重构之前，因为"分析有什么不足"≠重构）
    PromptTemplate(
        intent="analyze",
        trigger_patterns=[
            r"分析.{0,20}(?:项目|代码|仓库|工程|架构|质量|性能)",
            r"(?:分析|审查|评审|评估).{0,10}(?:一下|这个|那个|当前|现有)",
            r"(?:有哪些|有什么|有没有).{0,10}(?:不足|问题|改进|优化|提升|缺陷|漏洞|风险)",
            r"(?:analyze|review|audit|assess|evaluate).{0,20}(?:project|code|repo|architecture)",
        ],
        template=(
            "## 分析任务\n{task}\n\n"
            "## 分析要求\n"
            "1. 先了解项目结构和核心代码（用 list_files + read_file）\n"
            "2. 分析项目用途、技术栈和架构\n"
            "3. 评估代码质量（优点和问题）\n"
            "4. 给出具体可行的改进建议\n"
            "5. 不要编造不存在的文件或代码"
        ),
        system_hint="你是一个代码分析专家。请先通过工具了解项目实际结构，基于真实代码给出分析，不要凭空猜测。",
    ),

    # 重构/优化（在分析之后，需要实际修改代码）
    PromptTemplate(
        intent="refactor",
        trigger_patterns=[
            r"(?:重构|重写|改写).{0,10}(?:这段|这个|以下|上面).{0,10}(?:代码|函数|类|模块)",
            r"(?:优化|改进|改善).{0,10}(?:这段|这个|以下|上面).{0,10}(?:代码|函数|类|模块|写法)",
            r"(?:refactor|rewrite)\s+(?:this|the|following)",
            r"更好的(?:写法|实现|方式)",
            r"性能.*(?:优化|提升|改进).{0,10}(?:这段|这个)",
            r"(?:帮我|请|给).{0,5}(?:优化|重构|重写|改进|整理).{0,5}(?:这段|这个|以下).{0,5}(?:代码|函数|类)",
        ],
        template=(
            "## 待优化代码\n{task}\n\n"
            "## 要求\n"
            "1. 先分析当前代码的问题\n"
            "2. 给出优化后的完整代码\n"
            "3. 说明优化理由\n"
            "4. 优化后的代码必须可直接运行"
        ),
        system_hint="你是一个代码质量专家。请先分析现有代码，再给出优化方案和完整可运行的优化代码。",
    ),

    # 写代码
    PromptTemplate(
        intent="write_code",
        trigger_patterns=[
            r"(?:写|编写|实现|创建|开发|生成).*(?:代码|函数|类|模块|程序|脚本|接口|API|算法|爬虫|服务器)",
            r"(?:write|implement|create|build|generate)\s+(?:a|an|the)?\s*\w+",
            r"帮我写(?!测试|单测|文档)",
            r"帮我实现",
            r"用.*写一个",
        ],
        template=(
            "## 任务\n{task}\n\n"
            "## 要求\n"
            "- 代码必须完整可运行\n"
            "- 包含必要的 import 和类型注释\n"
            "- 添加 docstring 和关键注释\n"
            "- 遵循 {lang} 最佳实践\n\n"
            "## 输出格式\n"
            "直接输出完整代码，不要解释性文字。"
        ),
        system_hint="你是一个高级编程专家。输出的代码必须完整、可运行、符合最佳实践。",
    ),

    # 设计/架构
    PromptTemplate(
        intent="design",
        trigger_patterns=[
            r"(?:设计|架构|规划).*(?:系统|模块|接口|数据库|表|服务|方案)",
            r"帮我设计",
            r"(?:design|architecture|plan|strategy|approach)",
            r"怎么.*(?:设计|架构|组织|结构)",
            r"最好.*(?:方案|方式|实践)",
        ],
        template=(
            "## 需求描述\n{task}\n\n"
            "## 设计要求\n"
            "1. 给出整体架构方案\n"
            "2. 列出核心模块和职责\n"
            "3. 定义模块间接口\n"
            "4. 考虑扩展性和可维护性\n"
            "5. 给出技术选型建议"
        ),
        system_hint="你是一个系统架构师。请给出清晰、可落地的架构设计方案。",
    ),

    # 小说创作
    PromptTemplate(
        intent="novel",
        trigger_patterns=[
            r"(?:写|创作|编写|生成).*(?:小说|故事|章节|短篇|长篇|网文)",
            r"(?:续写|接着写|往下写|继续写)",
            r"(?:润色|修改|改写|重写).*(?:文章|段落|文字|描写|对话|文笔)",
            r"(?:扩写|扩展|丰富).*(?:细节|描写|段落)",
            r"(?:创建|设计|构建).*(?:角色|人物|主角|反派|配角)",
            r"(?:构建|设定|创建).*(?:世界观|设定|背景|魔法体系)",
            r"(?:写|列|规划).*(?:大纲|提纲|故事线|情节)",
            r"(?:分析|评价|点评).*(?:故事|情节|角色|文笔|节奏)",
            r"(?:write|create|continue|revise|expand).*(?:novel|story|chapter|fiction)",
            r"(?:write|build|develop).*(?:character|world|outline|plot)",
            # 检查/核对/验证 章节/大纲/内容
            r"(?:检查|核对|验证|审查|审阅).*(?:章节|大纲|剧情|内容|第\d+章|关联|是否符合|衔接|连贯)",
            r"(?:check|verify|review|examine).{0,20}(?:chapter|outline|plot|content)",
            # 调整/修改 章节/内容
            r"(?:调整|修改|修正|改进).{0,20}(?:章节|章|内容|段落|描写|文字墙|词组)",
            # 多小说管理
            r"(?:切换|打开|继续写|继续创作).{0,5}(?:小说|故事)",
            r"(?:新建|创建).*(?:小说|故事|作品)",
            r"(?:列出|显示|查看).*(?:小说|作品)",
        ],
        template=(
            "## 创作任务\n{task}\n\n"
            "## 创作要求\n"
            "1. 明确操作类型（大纲/写作/续写/润色/扩写/角色/世界观/分析）\n"
            "2. 如有已有内容，先阅读再创作\n"
            "3. 保持风格和人物一致性\n"
            "4. 注重细节和感官描写\n"
            "5. 创作内容保存到文件"
        ),
        system_hint="你是一位资深小说创作助手。请运用专业的叙事技巧进行创作，注重展示而非叙述、角色驱动、感官沉浸。所有创作内容请保存到文件。",
    ),

    # 信息查询（天气、时间等需要工具的查询）
    PromptTemplate(
        intent="query",
        trigger_patterns=[
            r"(?:查询|查|看).{0,10}(?:天气|气温|温度|时间|日期|汇率|股价|新闻|黄金|金价|价格|行情)",
            r"(?:天气|气温|温度).{0,10}(?:怎么样|如何|多少|预报)",
            r"(?:黄金|金价|价格|股价|汇率|行情).{0,10}(?:多少|查询|怎么样|如何|今日|今天)",
            r"(?:多少度|几度|热不热|冷不冷)",
            r"该穿什么",
            r"(?:穿什么|穿衣).{0,10}(?:合适|建议|好)",
            r"(?:weather|forecast|temperature|time|date|gold|price).{0,15}",
            r"(?:今天|今日|现在).{0,6}(?:黄金|金价|价格)",
        ],
        template=(
            "## 查询需求\n{task}\n\n"
            "## 要求\n"
            "1. 使用工具获取实时数据\n"
            "2. 给出准确的结果\n"
            "3. 如有需要，给出实用建议"
        ),
        system_hint="你是一个信息查询助手。请使用工具获取实时数据，给出准确的回答。",
    ),

    # 解释代码（放最后，因为"代码"这个词太泛）
    PromptTemplate(
        intent="explain",
        trigger_patterns=[
            r"(?:解释|说明|讲解|解读)一下",
            r"(?:什么意思|怎么理解)",
            r"(?:explain|describe|understand|what does|how does)",
            r"是什么意思",
            r"怎么.*(?:工作|运行)",
            r"(?:分析|解读)(?:一下)?(?:这段|这个|下面的).*(?:代码|逻辑|算法|函数)",
        ],
        template=(
            "## 需要解释的内容\n{task}\n\n"
            "## 解释要求\n"
            "1. 先给出一句话总结\n"
            "2. 逐步详细解释\n"
            "3. 指出关键设计决策\n"
            "4. 列出可能的注意事项"
        ),
        system_hint="你是一个技术文档专家。请用清晰、结构化的方式解释代码和技术概念。",
    ),

]


def detect_intent(text: str) -> str | None:
    """
    检测用户输入的意图。

    Returns:
        意图标识字符串，或 None（无法识别）。
    """
    for tmpl in TEMPLATES:
        for pattern in tmpl.trigger_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return tmpl.intent
    return None


def assess_quality(user_input: str) -> tuple[bool, str]:
    """
    评估用户提示词质量，决定是否需要优化。

    Returns:
        (needs_optimization, reason) — 是否需要优化及原因。
    """
    text = user_input.strip()

    # 1. 太短（< 10 字符）— 需要优化
    if len(text) < 10:
        return True, "输入过短，补充细节有助于获得更好的回答"

    # 2. 已经结构化（包含 ## 标记或编号列表）— 不需要优化
    if re.search(r"^##\s+", text, re.MULTILINE) or re.search(r"^\d+\.\s+", text, re.MULTILINE):
        return False, "已结构化"

    # 3. 长度 > 150 且有换行 — 大概率是详细描述，不需要优化
    if len(text) > 150 and "\n" in text:
        return False, "输入已足够详细"

    # 4. 包含代码块 — 不需要优化
    if "```" in text:
        return False, "包含代码块"

    # 5. 有明确的动词+对象结构 — 不需要优化
    clear_patterns = [
        r"^(?:请|帮我|给我|让我)\s*\S+",
        r"^(?:please|help me|can you)\s+\w+",
    ]
    for p in clear_patterns:
        if re.search(p, text, re.IGNORECASE):
            # 但如果只是 "帮我写" 这种太短的，还是需要优化
            if len(text) < 20:
                return True, "指令过于简短"
            return False, "指令明确"

    # 6. 纯口语化、没有明确结构 — 需要优化
    return True, "口语化输入，建议结构化"


def optimize_prompt(
    user_input: str,
    *,
    lang: str = "Python",
    context_hints: dict[str, Any] | None = None,
) -> tuple[str, str | None, bool]:
    """
    优化用户输入，返回 (优化后的 prompt, 补充的 system hint, 是否实际优化)。

    先评估质量，如果已经足够好则跳过优化。

    Args:
        user_input: 用户原始输入。
        lang: 目标编程语言。
        context_hints: 额外上下文信息（如文件路径、项目结构等）。

    Returns:
        (optimized_prompt, system_hint, was_optimized) — was_optimized 表示是否实际做了优化。
    """
    intent = detect_intent(user_input)

    if intent is None:
        return user_input, None, False

    template = next((t for t in TEMPLATES if t.intent == intent), None)
    if template is None:
        return user_input, None, False

    # 按需优化：先评估质量
    needs_opt, reason = assess_quality(user_input)
    if not needs_opt:
        return user_input, template.system_hint, False

    # 应用模板
    optimized = template.template.format(task=user_input, lang=lang)

    # 注入上下文提示
    if context_hints:
        hints = []
        if "file_path" in context_hints:
            hints.append(f"- 相关文件: {context_hints['file_path']}")
        if "project_type" in context_hints:
            hints.append(f"- 项目类型: {context_hints['project_type']}")
        if "recent_errors" in context_hints:
            hints.append(f"- 最近错误: {context_hints['recent_errors']}")
        if hints:
            optimized += "\n\n## 上下文信息\n" + "\n".join(hints)

    return optimized, template.system_hint, True


def get_intent_display(intent: str | None) -> str:
    """获取意图的中文显示名。"""
    display_map = {
        "write_code": "📝 编写代码",
        "debug": "🐛 调试修复",
        "explain": "📖 解释代码",
        "analyze": "🔍 代码分析",
        "refactor": "🔧 重构优化",
        "write_test": "🧪 编写测试",
        "design": "🏗️ 架构设计",
        "convert": "🔄 转换迁移",
        "novel": "📖 小说创作",
        "query": "🔍 信息查询",
    }
    if intent is None:
        return "💬 通用对话"
    return display_map.get(intent, "💬 通用对话")


# ── 统一输入分类器 ────────────────────────────────────────


def classify_input(text: str) -> dict:
    """统一输入分类器 — 合并意图检测 + 工具需求检测为单一权威信号。

    解决三层分类器（detect_intent / _detect_tool_need / _input_requires_tools）
    发散导致的"Direct Mode Trap"问题。

    Returns:
        {
            "intent": str | None,       # 意图分类
            "requires_tools": bool,     # 是否需要工具（保守策略: 任一系统说需要→True）
            "confidence": str,          # "high" | "medium" | "low"
            "suggested_mode": str,      # "react" | "direct" | "novel"
            "reason": str,              # 分类理由
        }
    """
    intent = detect_intent(text)
    text_lower = text.lower()

    # ── 工具需求检测（合并 _detect_tool_need + _input_requires_tools 逻辑）──
    needs_tools_by_action = _has_action_keywords(text_lower)
    needs_tools_by_entity = _has_file_entities(text_lower)
    requires_tools = needs_tools_by_action or needs_tools_by_entity

    # ── 置信度评估 ──
    if requires_tools and intent is not None:
        confidence = "high"
        reason = f"检测到意图 '{intent}' 且需要工具操作"
        suggested_mode = "react"
        if intent == "novel":
            suggested_mode = "novel"
    elif requires_tools:
        confidence = "medium"
        reason = "检测到工具需求（但无明确意图分类）"
        suggested_mode = "react"
    elif intent is not None:
        confidence = "high"
        reason = f"检测到明确意图 '{intent}'（无需工具）"
        suggested_mode = "direct"
        if intent == "novel":
            suggested_mode = "novel"
    else:
        confidence = "low"
        reason = "纯对话/咨询，无需工具"
        suggested_mode = "direct"

    return {
        "intent": intent,
        "requires_tools": requires_tools,
        "confidence": confidence,
        "suggested_mode": suggested_mode,
        "reason": reason,
    }


def _has_action_keywords(text: str) -> bool:
    """检测文本是否包含需要工具执行的操作关键词。

    合并了 _detect_tool_need() 和 _input_requires_tools() 的正向关键词。
    """
    # 文件操作关键词
    file_ops = [
        "创建", "写入", "保存", "新建", "生成", "输出",
        "读取", "查看", "打开", "读", "看",
        "修改", "编辑", "替换", "改", "更新",
        "删除", "移除", "清除",
        "移动", "搬", "转移", "复制", "拷贝", "备份", "重命名",
        "write", "create", "save", "generate",
        "read", "open", "show", "cat", "view",
        "edit", "modify", "update", "replace", "patch",
        "delete", "remove",
        "move", "copy", "cp", "mv", "rename",
    ]
    if any(kw in text for kw in file_ops):
        return True

    # 编程任务关键词（暗含需要文件操作）
    coding_ops = [
        "写", "做", "实现", "开发", "搭", "建",
        "编写", "重构", "重写", "改写",
        "implement", "build", "make",
    ]
    # 仅在搭配明确产出物时触发
    coding_targets = [
        "代码", "函数", "类", "模块", "程序", "脚本", "接口",
        "API", "算法", "爬虫", "服务器", "项目", "工程",
        "code", "function", "class", "module", "script",
        "app", "application", "project",
        "小说", "故事", "章节", "文章",
    ]
    if any(kw in text for kw in coding_ops) and any(kw in text for kw in coding_targets):
        return True

    # 命令执行关键词
    cmd_ops = [
        "执行", "运行", "跑", "命令", "脚本", "程序",
        "run", "execute", "exec", "command", "script",
        "安装", "install", "pip", "npm", "yarn", "cargo",
        "pytest", "测试",
        "git ", "commit", "push", "pull", "clone", "branch",
    ]
    if any(kw in text for kw in cmd_ops):
        return True

    # 搜索关键词
    search_ops = ["搜索", "查找", "grep", "find", "search"]
    if any(kw in text for kw in search_ops):
        return True

    # 网页抓取
    web_ops = ["抓取", "下载", "获取", "访问", "fetch", "download", "scrape"]
    if any(kw in text for kw in web_ops):
        return True

    # 目录操作
    dir_ops = ["列出", "显示", "list", "ls", "dir", "tree", "mkdir", "创建.*(?:目录|文件夹)"]
    import re
    if any(re.search(p, text) for p in dir_ops):
        return True

    # 内容检查/审查（需要读取文件）
    content_check = [
        "检查", "核对", "验证", "审查", "审阅",
        "是否符合", "是否一致", "是否合理",
        "check", "verify", "review", "examine",
    ]
    # 内容检查 + 文件实体 = 需要工具
    entity_keywords = [
        "章", "章节", "文件", "大纲", "剧情", "代码", "内容",
        "chapter", "file", "code", "content",
        ".py", ".js", ".ts", ".md", ".txt", ".json", ".yaml",
        "第",  # 第X章
    ]
    if any(kw in text for kw in content_check) and any(kw in text for kw in entity_keywords):
        return True

    # 调整/修改章节（需要读写）
    adjust_ops = ["调整", "修正", "改进"]
    if any(kw in text for kw in adjust_ops) and any(kw in text for kw in entity_keywords):
        return True

    return False


def _has_file_entities(text: str) -> bool:
    """检测文本是否涉及文件实体（暗示需要文件操作）。"""
    import re

    # 文件扩展名
    if re.search(r'\b\w+\.(?:py|js|ts|jsx|tsx|java|c|cpp|h|go|rs|rb|php|html|css|json|yaml|yml|toml|xml|md|txt|sh|bat|ps1)\b', text):
        return True

    # 路径模式
    if re.search(r'(?:^|\s)(?:\./|\.\./|src/|tests?/|lib/|app/|dist/|build/)\S+', text):
        return True
    if re.search(r'(?:^|\s)[A-Z]:\\[\w\\/.]+', text):
        return True

    # 章/大纲/文件等实体
    entity_words = [
        "章", "章节", "大纲", "文件", "目录", "文件夹",
        "代码", "源码", "脚本", "配置", "文档",
    ]
    return any(kw in text for kw in entity_words)
