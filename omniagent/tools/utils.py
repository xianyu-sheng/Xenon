"""工具共享工具函数 — 路径安全验证、模板变量替换、文本处理等。"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 路径安全常量
MAX_READ_SIZE = 2 * 1024 * 1024
MAX_WRITE_SIZE = 10 * 1024 * 1024
MAX_VERIFY_SIZE = 1 * 1024 * 1024

_SENSITIVE_PATHS = [
    "c:\\windows", "c:\\program files", "c:\\programdata",
    "/etc", "/usr", "/bin", "/sbin", "/boot", "/dev", "/proc", "/sys",
    "/var/log", "/root/.ssh", "/root/.gnupg",
]

_USER_SENSITIVE = [
    ".ssh", ".gnupg", ".aws", ".azure", ".config/gh",
    ".docker/config.json", "credentials", "id_rsa", "id_ed25519",
]


def validate_file_path(
    file_path: str,
    *,
    for_write: bool = False,
    cwd: str | Path | None = None,
) -> Path:
    """安全验证文件路径。

    Args:
        file_path: 原始文件路径
        for_write: 写入操作（更严格）
        cwd: 工作目录（默认当前目录）

    Returns:
        验证通过的 Path 对象

    Raises:
        ValueError: 路径不安全
    """
    if not file_path:
        raise ValueError("文件路径不能为空")

    path = Path(file_path)
    if cwd and not path.is_absolute():
        path = Path(cwd) / path

    resolved = path.resolve()
    root = Path(cwd).resolve() if cwd else Path.cwd().resolve()

    try:
        resolved.relative_to(root)
    except ValueError:
        raise ValueError(f"路径越界: {resolved} 不在允许的目录 {root} 下")

    if for_write:
        resolved_lower = str(resolved).lower().replace("\\", "/")
        for sensitive in _SENSITIVE_PATHS:
            if sensitive in resolved_lower:
                raise ValueError(f"禁止写入系统敏感路径: {resolved}")

        name_lower = resolved.name.lower()
        for sensitive in _USER_SENSITIVE:
            if sensitive in name_lower or sensitive in resolved_lower:
                raise ValueError(f"禁止写入敏感文件: {resolved}")

    return path


def resolve_template(template: str, context: dict[str, Any] | None = None) -> str:
    """将模板字符串中的 {key} 替换为 context 中的值。"""
    if not context:
        return template

    def _replace(m: re.Match) -> str:
        key = m.group(1)
        val = context.get(key)
        return str(val) if val is not None else m.group(0)

    return re.sub(r"\{(\w+)\}", _replace, template)


def truncate_content(content: str, max_len: int = 50_000) -> str:
    """截断过长内容并附加提示。"""
    if len(content) <= max_len:
        return content
    return content[:max_len] + f"\n\n... (内容已截断，超过 {max_len} 字符)"


def safe_read_file(path: Path, max_size: int = MAX_READ_SIZE) -> str | None:
    """安全读取文件内容。超过大小限制返回 None。"""
    if path.stat().st_size > max_size:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")
