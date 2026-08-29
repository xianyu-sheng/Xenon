"""
语义边界检测 - Phase 2 核心模块。

在模型中断续写时，自动回滚到最近的完整语义单元（代码块、段落、句子），
确保续写内容语义连贯。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class ContentType(Enum):
    """内容类型"""

    AUTO = "auto"  # 自动检测
    CODE = "code"  # 代码
    MARKDOWN = "markdown"  # Markdown 文档
    TEXT = "text"  # 纯文本


class BoundaryType(Enum):
    """边界类型（按优先级排序）"""

    CODE_BLOCK = "code_block"  # 代码块（完整的 {} 或缩进块）
    FUNCTION = "function"  # 函数定义
    CLASS = "class"  # 类定义
    STATEMENT = "statement"  # 语句（完整的一行或 ;）
    MARKDOWN_CODE = "markdown_code"  # Markdown 代码块（```）
    MARKDOWN_HEADER = "markdown_header"  # Markdown 标题
    MARKDOWN_LIST = "markdown_list"  # Markdown 列表项
    PARAGRAPH = "paragraph"  # 段落（双换行）
    SENTENCE = "sentence"  # 句子（。！？. ! ?）
    WORD = "word"  # 词语（空格、标点）
    NONE = "none"  # 无边界（保留全部）

    @property
    def priority(self) -> int:
        """返回边界类型的优先级（数字越小优先级越高）"""
        priorities = {
            BoundaryType.FUNCTION: 1,
            BoundaryType.CLASS: 2,
            BoundaryType.CODE_BLOCK: 3,
            BoundaryType.STATEMENT: 4,
            BoundaryType.MARKDOWN_CODE: 5,
            BoundaryType.MARKDOWN_HEADER: 6,
            BoundaryType.MARKDOWN_LIST: 7,
            BoundaryType.PARAGRAPH: 8,
            BoundaryType.SENTENCE: 9,
            BoundaryType.WORD: 10,
            BoundaryType.NONE: 99,
        }
        return priorities.get(self, 99)


@dataclass
class BoundaryResult:
    """边界检测结果"""

    complete_part: str
    """完整部分（到边界为止）"""

    incomplete_part: str
    """不完整部分（边界之后）"""

    boundary_type: BoundaryType
    """边界类型"""

    boundary_position: int
    """边界位置（字符索引）"""

    confidence: float
    """检测置信度（0-1）"""

    metadata: dict = field(default_factory=dict)
    """额外的元数据（如检测到的语言、缩进等）"""

    def __len__(self) -> int:
        """返回完整部分的长度"""
        return len(self.complete_part)

    def is_valid(self, min_length: int = 50) -> bool:
        """判断完整部分是否有效（长度是否达到阈值）"""
        return len(self.complete_part) >= min_length

    def rollback_ratio(self) -> float:
        """计算回滚比例（不完整部分占总长度的比例）"""
        total = len(self.complete_part) + len(self.incomplete_part)
        if total == 0:
            return 0.0
        return len(self.incomplete_part) / total

    def to_dict(self) -> dict:
        """转换为字典（用于日志和调试）"""
        return {
            "complete_length": len(self.complete_part),
            "incomplete_length": len(self.incomplete_part),
            "boundary_type": self.boundary_type.value,
            "boundary_position": self.boundary_position,
            "confidence": self.confidence,
            "rollback_ratio": self.rollback_ratio(),
            "metadata": self.metadata,
        }


@dataclass
class BoundaryPattern:
    """边界检测模式"""

    name: str
    """模式名称"""

    pattern: str | re.Pattern
    """正则表达式模式"""

    boundary_type: BoundaryType
    """对应的边界类型"""

    confidence: float = 0.8
    """默认置信度"""

    reverse: bool = False
    """是否从后向前匹配"""

    def match(self, text: str) -> int | None:
        """
        匹配文本，返回边界位置。

        Args:
            text: 要匹配的文本

        Returns:
            边界位置（字符索引），如果未匹配返回 None
        """
        if isinstance(self.pattern, str):
            pattern = re.compile(self.pattern, re.MULTILINE | re.DOTALL)
        else:
            pattern = self.pattern

        if self.reverse:
            # 从后向前查找最后一个匹配
            matches = list(pattern.finditer(text))
            if matches:
                last_match = matches[-1]
                return last_match.end()
        else:
            # 从前向后查找第一个匹配
            match = pattern.search(text)
            if match:
                return match.end()

        return None


# 预定义的常用边界模式
COMMON_PATTERNS = [
    # Python 函数定义（完整的缩进块）
    BoundaryPattern(
        name="python_function",
        pattern=r"def\s+\w+\([^)]*\):\s*\n(?:    .*\n)*",
        boundary_type=BoundaryType.FUNCTION,
        confidence=0.9,
        reverse=True,
    ),
    # Python 类定义
    BoundaryPattern(
        name="python_class",
        pattern=r"class\s+\w+[^:]*:\s*\n(?:    .*\n)*",
        boundary_type=BoundaryType.CLASS,
        confidence=0.9,
        reverse=True,
    ),
    # Markdown 代码块
    BoundaryPattern(
        name="markdown_code",
        pattern=r"```[^\n]*\n.*?\n```",
        boundary_type=BoundaryType.MARKDOWN_CODE,
        confidence=0.95,
        reverse=True,
    ),
    # Markdown 标题
    BoundaryPattern(
        name="markdown_header",
        pattern=r"^#{1,6}\s+.*$",
        boundary_type=BoundaryType.MARKDOWN_HEADER,
        confidence=0.9,
        reverse=True,
    ),
    # 段落（双换行）
    BoundaryPattern(
        name="paragraph",
        pattern=r".*?\n\n",
        boundary_type=BoundaryType.PARAGRAPH,
        confidence=0.7,
        reverse=True,
    ),
    # 中文句子
    BoundaryPattern(
        name="chinese_sentence",
        pattern=r"[^。！？]*[。！？]",
        boundary_type=BoundaryType.SENTENCE,
        confidence=0.8,
        reverse=True,
    ),
    # 英文句子
    BoundaryPattern(
        name="english_sentence",
        pattern=r"[^.!?]*[.!?]\s",
        boundary_type=BoundaryType.SENTENCE,
        confidence=0.8,
        reverse=True,
    ),
]


# ============================================================
# 代码边界检测器
# ============================================================


class CodeBoundaryDetector:
    """代码边界检测器"""

    def detect(self, code: str) -> BoundaryResult | None:
        """
        检测代码的语义边界。

        优先级：函数定义 > 类定义 > 代码块 > 语句

        Args:
            code: 代码文本

        Returns:
            边界检测结果，如果未找到边界返回 None
        """
        # 尝试函数定义边界
        result = self._detect_function_boundary(code)
        if result and result.is_valid(min_length=20):
            return result

        # 尝试类定义边界
        result = self._detect_class_boundary(code)
        if result and result.is_valid(min_length=20):
            return result

        # 尝试代码块边界（括号平衡）
        result = self._detect_block_boundary(code)
        if result and result.is_valid(min_length=20):
            return result

        # 尝试语句边界
        result = self._detect_statement_boundary(code)
        if result and result.is_valid(min_length=10):
            return result

        return None

    def _detect_function_boundary(self, code: str) -> BoundaryResult | None:
        """检测完整函数定义的边界"""
        # Python: def name(...):
        # JavaScript: function name(...) {
        # Java/C++: type name(...) {

        patterns = [
            # Python 函数（检测完整的缩进块）
            (
                r"(def\s+\w+\([^)]*\):[^\n]*\n(?:(?:    |\t).*\n)*)",
                BoundaryType.FUNCTION,
                "python",
            ),
            # JavaScript/TypeScript 函数
            (
                r"(function\s+\w+\([^)]*\)\s*\{[^}]*\})",
                BoundaryType.FUNCTION,
                "javascript",
            ),
            # Arrow function
            (
                r"(const\s+\w+\s*=\s*\([^)]*\)\s*=>\s*\{[^}]*\})",
                BoundaryType.FUNCTION,
                "javascript",
            ),
        ]

        for pattern, btype, lang in patterns:
            matches = list(re.finditer(pattern, code, re.MULTILINE | re.DOTALL))
            if matches:
                last_match = matches[-1]
                complete = code[: last_match.end()]
                incomplete = code[last_match.end() :]

                return BoundaryResult(
                    complete_part=complete,
                    incomplete_part=incomplete,
                    boundary_type=btype,
                    boundary_position=last_match.end(),
                    confidence=0.9,
                    metadata={"language": lang, "pattern": "function"},
                )

        return None

    def _detect_class_boundary(self, code: str) -> BoundaryResult | None:
        """检测完整类定义的边界"""
        # Python: class Name:
        # Java/C++: class Name {

        patterns = [
            # Python 类
            (
                r"(class\s+\w+[^:]*:[^\n]*\n(?:(?:    |\t).*\n)*)",
                BoundaryType.CLASS,
                "python",
            ),
            # Java/C++ 类
            (r"(class\s+\w+[^{]*\{[^}]*\})", BoundaryType.CLASS, "java"),
        ]

        for pattern, btype, lang in patterns:
            matches = list(re.finditer(pattern, code, re.MULTILINE | re.DOTALL))
            if matches:
                last_match = matches[-1]
                complete = code[: last_match.end()]
                incomplete = code[last_match.end() :]

                return BoundaryResult(
                    complete_part=complete,
                    incomplete_part=incomplete,
                    boundary_type=btype,
                    boundary_position=last_match.end(),
                    confidence=0.9,
                    metadata={"language": lang, "pattern": "class"},
                )

        return None

    def _detect_block_boundary(self, code: str) -> BoundaryResult | None:
        """检测代码块边界（括号平衡）"""
        # 找到最后一个完整的 {} 或 Python 缩进块

        # 尝试大括号语言
        brace_pos = self._find_last_balanced_brace(code)
        if brace_pos is not None and brace_pos > 20:
            return BoundaryResult(
                complete_part=code[:brace_pos],
                incomplete_part=code[brace_pos:],
                boundary_type=BoundaryType.CODE_BLOCK,
                boundary_position=brace_pos,
                confidence=0.85,
                metadata={"pattern": "brace_balance"},
            )

        # 尝试 Python 缩进块
        indent_pos = self._find_last_python_block(code)
        if indent_pos is not None and indent_pos > 20:
            return BoundaryResult(
                complete_part=code[:indent_pos],
                incomplete_part=code[indent_pos:],
                boundary_type=BoundaryType.CODE_BLOCK,
                boundary_position=indent_pos,
                confidence=0.8,
                metadata={"pattern": "python_indent"},
            )

        return None

    def _detect_statement_boundary(self, code: str) -> BoundaryResult | None:
        """检测语句边界"""
        lines = code.split("\n")
        if len(lines) < 2:
            return None

        # 查找最后一个完整的语句行（非空、有内容）
        for i in range(len(lines) - 1, 0, -1):
            line = lines[i - 1].rstrip()

            # 跳过空行和注释
            if not line or line.strip().startswith(("#", "//")):
                continue

            # Python: 完整的语句行（不以 \ 结尾）
            if not line.endswith("\\"):
                # 检查缩进是否完整（不在字符串/表达式中间）
                complete = "\n".join(lines[:i])
                incomplete = "\n".join(lines[i:])

                if len(complete) > 10:
                    return BoundaryResult(
                        complete_part=complete,
                        incomplete_part=incomplete,
                        boundary_type=BoundaryType.STATEMENT,
                        boundary_position=len(complete),
                        confidence=0.7,
                        metadata={"pattern": "line_boundary"},
                    )

        return None

    def _find_last_balanced_brace(self, code: str) -> int | None:
        """找到最后一个平衡的大括号位置"""
        stack = []
        last_balanced = None

        for i, char in enumerate(code):
            if char == "{":
                stack.append(i)
            elif char == "}":
                if stack:
                    stack.pop()
                    if not stack:  # 栈为空，找到一个平衡点
                        last_balanced = i + 1

        return last_balanced

    def _find_last_python_block(self, code: str) -> int | None:
        """找到最后一个完整的 Python 缩进块"""
        lines = code.split("\n")
        if len(lines) < 2:
            return None

        # 从后向前查找缩进减少的位置（块结束）
        for i in range(len(lines) - 1, 0, -1):
            current_line = lines[i]
            prev_line = lines[i - 1]

            # 跳过空行
            if not current_line.strip():
                continue

            current_indent = len(current_line) - len(current_line.lstrip())
            prev_indent = len(prev_line) - len(prev_line.lstrip())

            # 如果当前行缩进减少，且前一行不是空行
            if current_indent < prev_indent and prev_line.strip():
                # 找到块边界
                pos = sum(len(line) + 1 for line in lines[:i])
                if pos > 20:
                    return pos

        return None


# ============================================================
# Markdown 边界检测器
# ============================================================


class MarkdownBoundaryDetector:
    """Markdown 边界检测器"""

    def detect(self, text: str) -> BoundaryResult | None:
        """
        检测 Markdown 的语义边界。

        优先级：代码块 > 标题 > 列表项

        Args:
            text: Markdown 文本

        Returns:
            边界检测结果，如果未找到边界返回 None
        """
        # 尝试代码块边界
        result = self._detect_code_block_boundary(text)
        if result and result.is_valid(min_length=20):
            return result

        # 尝试标题边界
        result = self._detect_header_boundary(text)
        if result and result.is_valid(min_length=10):
            return result

        # 尝试列表项边界
        result = self._detect_list_boundary(text)
        if result and result.is_valid(min_length=10):
            return result

        return None

    def _detect_code_block_boundary(self, text: str) -> BoundaryResult | None:
        """检测完整的 Markdown 代码块（```）"""
        # 查找所有代码块
        pattern = r"```[^\n]*\n.*?\n```"
        matches = list(re.finditer(pattern, text, re.MULTILINE | re.DOTALL))

        if matches:
            last_match = matches[-1]
            complete = text[: last_match.end()]
            incomplete = text[last_match.end() :]

            return BoundaryResult(
                complete_part=complete,
                incomplete_part=incomplete,
                boundary_type=BoundaryType.MARKDOWN_CODE,
                boundary_position=last_match.end(),
                confidence=0.95,
                metadata={"pattern": "markdown_code_block"},
            )

        return None

    def _detect_header_boundary(self, text: str) -> BoundaryResult | None:
        """检测 Markdown 标题边界"""
        # 查找所有标题（# 到 ######）
        pattern = r"^#{1,6}\s+.*$"
        lines = text.split("\n")

        # 从后向前查找最后一个标题
        for i in range(len(lines) - 1, 0, -1):
            if re.match(pattern, lines[i]):
                # 找到标题，包含到这一行
                complete = "\n".join(lines[: i + 1])
                incomplete = "\n".join(lines[i + 1 :])

                return BoundaryResult(
                    complete_part=complete,
                    incomplete_part=incomplete,
                    boundary_type=BoundaryType.MARKDOWN_HEADER,
                    boundary_position=len(complete),
                    confidence=0.9,
                    metadata={"pattern": "markdown_header"},
                )

        return None

    def _detect_list_boundary(self, text: str) -> BoundaryResult | None:
        """检测 Markdown 列表项边界"""
        # 查找列表项（- * + 或 1. 2. 等）
        pattern = r"^(?:[-*+]|\d+\.)\s+"
        lines = text.split("\n")

        for i in range(len(lines) - 1, 0, -1):
            if re.match(pattern, lines[i].lstrip()):
                complete = "\n".join(lines[: i + 1])
                incomplete = "\n".join(lines[i + 1 :])

                return BoundaryResult(
                    complete_part=complete,
                    incomplete_part=incomplete,
                    boundary_type=BoundaryType.MARKDOWN_LIST,
                    boundary_position=len(complete),
                    confidence=0.85,
                    metadata={"pattern": "markdown_list"},
                )

        return None


# ============================================================
# 文本边界检测器
# ============================================================


class TextBoundaryDetector:
    """文本边界检测器"""

    def detect(self, text: str) -> BoundaryResult | None:
        """
        检测文本的语义边界。

        优先级：段落 > 句子 > 词语

        Args:
            text: 文本内容

        Returns:
            边界检测结果，如果未找到边界返回 None
        """
        # 尝试段落边界
        result = self._detect_paragraph_boundary(text)
        if result and result.is_valid(min_length=20):
            return result

        # 尝试句子边界
        result = self._detect_sentence_boundary(text)
        if result and result.is_valid(min_length=10):
            return result

        # 尝试词语边界（保底）
        result = self._detect_word_boundary(text)
        if result:
            return result

        return None

    def _detect_paragraph_boundary(self, text: str) -> BoundaryResult | None:
        """检测段落边界（双换行）"""
        # 查找最后一个双换行
        pattern = r"\n\n"
        matches = list(re.finditer(pattern, text))

        if matches:
            last_match = matches[-1]
            complete = text[: last_match.end()]
            incomplete = text[last_match.end() :]

            return BoundaryResult(
                complete_part=complete.rstrip(),
                incomplete_part=incomplete.lstrip(),
                boundary_type=BoundaryType.PARAGRAPH,
                boundary_position=last_match.end(),
                confidence=0.8,
                metadata={"pattern": "double_newline"},
            )

        return None

    def _detect_sentence_boundary(self, text: str) -> BoundaryResult | None:
        """检测句子边界"""
        # 中文句号、英文句号（避免缩写和小数点）
        # 从后向前查找

        # 中文句子结束符
        cn_pattern = r"[。！？]"
        cn_matches = list(re.finditer(cn_pattern, text))

        # 英文句子结束符（后面必须跟空格或换行，避免小数点）
        en_pattern = r"[.!?](?=\s|\n|$)"
        en_matches = list(re.finditer(en_pattern, text))

        # 合并所有匹配，取最后一个
        all_matches = sorted(cn_matches + en_matches, key=lambda m: m.end())

        if all_matches:
            last_match = all_matches[-1]
            complete = text[: last_match.end()]
            incomplete = text[last_match.end() :]

            return BoundaryResult(
                complete_part=complete.rstrip(),
                incomplete_part=incomplete.lstrip(),
                boundary_type=BoundaryType.SENTENCE,
                boundary_position=last_match.end(),
                confidence=0.75,
                metadata={"pattern": "sentence_end"},
            )

        return None

    def _detect_word_boundary(self, text: str) -> BoundaryResult | None:
        """检测词语边界（保底方案）"""
        # 查找最后一个空格或标点符号
        pattern = r"[\s,;:，；：、]"
        matches = list(re.finditer(pattern, text))

        if matches:
            last_match = matches[-1]
            complete = text[: last_match.end()]
            incomplete = text[last_match.end() :]

            return BoundaryResult(
                complete_part=complete.rstrip(),
                incomplete_part=incomplete.lstrip(),
                boundary_type=BoundaryType.WORD,
                boundary_position=last_match.end(),
                confidence=0.5,
                metadata={"pattern": "word_boundary"},
            )

        # 如果连空格都没有，只能返回 80% 的内容
        if len(text) > 10:
            pos = int(len(text) * 0.8)
            return BoundaryResult(
                complete_part=text[:pos],
                incomplete_part=text[pos:],
                boundary_type=BoundaryType.WORD,
                boundary_position=pos,
                confidence=0.3,
                metadata={"pattern": "fallback_80_percent"},
            )

        return None


# ============================================================
# 主检测器（统一入口）
# ============================================================


class BoundaryDetector:
    """语义边界检测器（统一入口）"""

    def __init__(self):
        self.code_detector = CodeBoundaryDetector()
        self.markdown_detector = MarkdownBoundaryDetector()
        self.text_detector = TextBoundaryDetector()

    def find_last_boundary(
        self, text: str, content_type: ContentType = ContentType.AUTO
    ) -> BoundaryResult:
        """
        找到最后一个完整的语义边界。

        Args:
            text: 要检测的文本
            content_type: 内容类型（AUTO 为自动检测）

        Returns:
            边界检测结果（保证非 None，至少返回保底方案）
        """
        # 1. 自动检测内容类型
        if content_type == ContentType.AUTO:
            content_type = self._detect_content_type(text)

        # 2. 根据内容类型选择检测器
        result = None

        if content_type == ContentType.CODE:
            result = self.code_detector.detect(text)
            if result:
                return result

        if content_type == ContentType.MARKDOWN:
            result = self.markdown_detector.detect(text)
            if result:
                return result

        # 3. 文本边界（保底，保证有返回值）
        result = self.text_detector.detect(text)
        if result:
            return result

        # 4. 最终保底：返回全部内容
        return BoundaryResult(
            complete_part=text,
            incomplete_part="",
            boundary_type=BoundaryType.NONE,
            boundary_position=len(text),
            confidence=0.1,
            metadata={"pattern": "no_boundary_found"},
        )

    def _detect_content_type(self, text: str) -> ContentType:
        """
        自动检测内容类型。

        启发式规则：
        - 有代码关键字 → CODE
        - 有 Markdown 标记 → MARKDOWN
        - 其他 → TEXT

        Args:
            text: 要检测的文本

        Returns:
            检测到的内容类型
        """
        # 代码关键字（优先级最高）
        code_keywords = [
            "def ",
            "class ",
            "function ",
            "public ",
            "private ",
            "import ",
            "from ",
            "const ",
            "let ",
            "var ",
            "=>",
            "{",
            "}",
        ]

        for keyword in code_keywords:
            if keyword in text:
                return ContentType.CODE

        # Markdown 标记
        markdown_markers = ["```", "# ", "## ", "- [ ]", "- [x]", "* ", "1. "]

        for marker in markdown_markers:
            if marker in text:
                return ContentType.MARKDOWN

        # 默认文本
        return ContentType.TEXT

