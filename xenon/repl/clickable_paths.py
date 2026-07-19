"""
v0.5.0: OSC-8 可点击文件路径工具。

在终端中生成可点击的文件链接（OSC-8 超文本协议），
支持现代终端模拟器（iTerm2、Kitty、WezTerm、Windows Terminal、VSCode 终端等）。

用法：
    from xenon.repl.clickable_paths import make_clickable, format_output_with_links

    # 手动创建可点击链接
    print(make_clickable("/path/to/file.py", line=42))

    # 自动扫描文本中的路径并添加链接
    print(format_output_with_links("See src/main.py:42 for details"))
"""

from __future__ import annotations

import os
import re
from urllib.parse import quote as _url_quote

# ── OSC-8 超链接序列 ─────────────────────────────────────

_OSC8 = "\033]8"
_ST = "\033\\"


def make_clickable(file_path: str, line: int = 0, display: str | None = None) -> str:
    """生成 OSC-8 可点击链接。

    Args:
        file_path: 绝对或相对文件路径
        line: 行号（0 表示不指定）
        display: 显示文本（默认使用 file_path）

    Returns:
        包含 OSC-8 转义序列的字符串
    """
    # 构造 file:// URI（路径需 URL 编码）
    abs_path = os.path.abspath(file_path)
    encoded_path = _url_quote(abs_path, safe="/")
    uri = f"file://{encoded_path}"
    if line > 0:
        uri += f"#L{line}"

    label = display or file_path
    # OSC-8 格式: ESC ] 8 ; <params> ; <uri> ST <label> ESC ] 8 ; ; ST
    return f"{_OSC8};;{uri}{_ST}{label}{_OSC8};;{_ST}"


# ── 文件路径检测正则 ──────────────────────────────────────

# 匹配模式：
#   /absolute/path/to/file.py
#   ./relative/path/to/file.py:42
#   ../parent/file.py
#   ~/home/file.py
#   C:\\Windows\\path (Windows)
_PATH_PATTERN = re.compile(
    r'(?<!\w)'
    r'('
    r'(?:~|\.\.?)?'          # ~ / . / ..
    r'(?:/[-\w.+@]+)+'       # /path/to/file
    r'(?:\.\w+)?'             # .extension (optional)
    r'(?::\d+)?'              # :line_number (optional)
    r'|'
    r'[A-Za-z]:\\[-\w.\\ +@]+'  # Windows: C:\path\to\file
    r'(?:\.\w+)?'
    r')'
    r'(?!\w)'
)

# 常见代码文件扩展名——用于过滤误匹配（如 URL、英文句子中的"a/b"）
_CODE_EXTS = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".cpp", ".h",
    ".go", ".rs", ".rb", ".php", ".html", ".css", ".json", ".yaml", ".yml",
    ".toml", ".xml", ".md", ".txt", ".sh", ".bat", ".ps1", ".sql", ".r",
    ".swift", ".kt", ".scala", ".clj", ".ex", ".exs", ".erl", ".hrl",
    ".vue", ".svelte", ".astro", ".tf", ".proto", ".cfg", ".ini", ".conf",
    ".lock", ".toml", ".gradle", ".sbt",
})


def _is_likely_path(text: str) -> bool:
    """检查文本是否可能是一个文件路径（而非误匹配）。"""
    # 绝对路径几乎肯定是
    if text.startswith("/") or text.startswith("~/"):
        return True
    # 相对路径需要检查扩展名
    if text.startswith("./") or text.startswith("../"):
        return True
    # 检查是否有代码文件扩展名
    _, ext = os.path.splitext(text)
    if ext.lower() in _CODE_EXTS:
        return True
    # 包含多个路径段的可能是路径
    if text.count("/") >= 2:
        return True
    return False


def format_output_with_links(text: str) -> str:
    """扫描文本中的文件路径，自动转为 OSC-8 可点击链接。

    Markdown 代码块内的路径不会被转换（避免破坏代码示例），
    但代码块内容原样保留。

    Args:
        text: 原始文本

    Returns:
        包含 OSC-8 链接的文本
    """
    if not text:
        return text

    result_parts: list[str] = []
    in_code_block = False

    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        # 检测 Markdown 代码块边界
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            result_parts.append(line)
            continue

        # 代码块内原样保留
        if in_code_block:
            result_parts.append(line)
            continue

        # 查找并替换路径为可点击链接
        cursor = 0
        for m in _PATH_PATTERN.finditer(line):
            path = m.group(0)
            if not _is_likely_path(path):
                continue

            start, end = m.start(), m.end()

            # 检查是否在 OSC-8 链接内（避免嵌套）
            before = line[cursor:start]
            if _OSC8 in before and before.rfind(_OSC8) > before.rfind(_ST if _ST in before else "\x00"):
                continue

            # 追加匹配之前的文本
            result_parts.append(line[cursor:start])

            # 解析行号
            line_num = 0
            clean_path = path
            if ":" in path and not path.startswith(("~", ".", "C:")) and "://" not in path:
                candidate_parts = path.rsplit(":", 1)
                if len(candidate_parts) == 2 and candidate_parts[1].isdigit():
                    clean_path = candidate_parts[0]
                    line_num = int(candidate_parts[1])

            clickable = make_clickable(clean_path, line=line_num, display=path)
            result_parts.append(clickable)
            cursor = end

        # 追加该行剩余文本
        result_parts.append(line[cursor:])

    return "".join(result_parts)
