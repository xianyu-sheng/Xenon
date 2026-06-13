"""文件操作工具 — ReadFileTool, WriteFileTool, EditFileTool, CreateDirectoryTool, ListFilesTool。
"""

from __future__ import annotations

import difflib
import fnmatch
import logging
import os
from pathlib import Path
from typing import Any

from omniagent.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

MAX_READ_SIZE = 2 * 1024 * 1024       # 2MB
MAX_WRITE_SIZE = 10 * 1024 * 1024     # 10MB
MAX_VERIFY_SIZE = 1 * 1024 * 1024     # 1MB

_SENSITIVE_PATHS = [
    "c:\\windows", "c:\\program files", "c:\\programdata",
    "/etc", "/usr", "/bin", "/sbin", "/boot", "/dev", "/proc", "/sys",
]
_USER_SENSITIVE = [
    ".ssh", ".gnupg", ".aws", ".azure", ".config/gh",
    "credentials", "id_rsa", "id_ed25519",
]


def _validate_path(file_path: str, for_write: bool = False) -> Path:
    """验证文件路径安全性。"""
    path = Path(file_path).resolve()
    cwd = Path.cwd().resolve()

    try:
        path.relative_to(cwd)
    except ValueError:
        raise ValueError(f"路径越界: {path} 不在项目目录 {cwd} 下")

    if for_write:
        path_lower = str(path).lower().replace("\\", "/")
        for sensitive in _SENSITIVE_PATHS:
            if sensitive in path_lower:
                raise ValueError(f"禁止写入系统敏感路径: {path}")
        name_lower = path.name.lower()
        for sensitive in _USER_SENSITIVE:
            if sensitive in name_lower or sensitive in path_lower:
                raise ValueError(f"禁止写入敏感文件: {path}")

    return path


class ReadFileTool(BaseTool):
    name = "read_file"
    description = "读取本机文件内容并返回文本。支持分段读取。仅限本地文件。"
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "本地文件的绝对或相对路径"},
            "start_line": {"type": "integer", "description": "起始行号（可选，从1开始）"},
            "max_lines": {"type": "integer", "description": "最大读取行数（可选）"},
        },
        "required": ["file_path"],
    }

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        file_path = str(params.get("file_path", ""))
        if not file_path:
            return ToolResult.schema_error("read_file 需要 file_path 参数")

        try:
            path = _validate_path(file_path, for_write=False)
        except ValueError as e:
            return ToolResult.permission_denied(str(e))

        if not path.exists():
            return ToolResult.error(f"文件不存在: {path}", error_type="runtime_error")

        file_size = path.stat().st_size
        if file_size > MAX_READ_SIZE:
            return ToolResult.error(
                f"文件过大: {file_size} 字节，上限 {MAX_READ_SIZE} 字节",
                error_type="runtime_error",
            )

        start_line = params.get("start_line")
        max_lines = params.get("max_lines")

        if start_line is not None or max_lines is not None:
            all_lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            s = max(1, int(start_line or 1)) - 1
            e = s + int(max_lines) if max_lines else len(all_lines)
            content = "".join(all_lines[s:min(e, len(all_lines))])
            return ToolResult.ok(
                content,
                total_lines=len(all_lines),
                from_line=s + 1,
                to_line=min(e, len(all_lines)),
                file_path=str(path),
            )

        content = path.read_text(encoding="utf-8")
        return ToolResult.ok(content, size=len(content), file_path=str(path))


class WriteFileTool(BaseTool):
    name = "write_file"
    description = "将文本内容完整写入本机文件（覆盖已有内容）。文件不存在时自动创建，父目录不存在时自动创建。"
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "本地文件路径"},
            "content": {"type": "string", "description": "要写入的完整文本内容"},
        },
        "required": ["file_path", "content"],
    }

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        file_path = str(params.get("file_path", ""))
        content = str(params.get("content", ""))
        if not file_path:
            return ToolResult.schema_error("write_file 需要 file_path 参数")

        try:
            path = _validate_path(file_path, for_write=True)
        except ValueError as e:
            return ToolResult.permission_denied(str(e))

        content_bytes = len(content.encode("utf-8"))
        if content_bytes > MAX_WRITE_SIZE:
            return ToolResult.error(f"内容过大: {content_bytes} 字节，上限 {MAX_WRITE_SIZE} 字节")

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

        # 写入后验证
        if path.stat().st_size <= MAX_VERIFY_SIZE:
            actual = path.read_text(encoding="utf-8")
            if actual != content:
                return ToolResult.error("内容验证失败: 写入内容与预期不一致")

        logger.info(f"写入文件: {path} ({content_bytes} 字节)")
        return ToolResult.ok(str(path), bytes_written=content_bytes, file_path=str(path))


class EditFileTool(BaseTool):
    name = "edit_file"
    description = "对本机文件进行精确的查找-替换编辑。old_text 必须与文件中的原文完全匹配（包括空格和缩进）。适合修改单处内容。"
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "要编辑的本地文件路径"},
            "old_text": {"type": "string", "description": "要被替换的原始文本（必须精确匹配）"},
            "new_text": {"type": "string", "description": "替换后的新文本"},
        },
        "required": ["file_path", "old_text", "new_text"],
    }

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        file_path = str(params.get("file_path", ""))
        old_text = str(params.get("old_text", ""))
        new_text = str(params.get("new_text", ""))

        if not file_path or not old_text:
            return ToolResult.schema_error("edit_file 需要 file_path 和 old_text 参数")

        try:
            path = _validate_path(file_path, for_write=True)
        except ValueError as e:
            return ToolResult.permission_denied(str(e))

        if not path.exists():
            return ToolResult.error(f"文件不存在: {path}")

        content = path.read_text(encoding="utf-8")
        count = content.count(old_text)
        if count == 0:
            return ToolResult.error("未找到匹配文本")
        if count > 1:
            return ToolResult.error(f"找到 {count} 处匹配，请提供更多上下文")

        new_content = content.replace(old_text, new_text, 1)
        path.write_text(new_content, encoding="utf-8")

        # 验证
        actual = path.read_text(encoding="utf-8")
        if actual != new_content:
            return ToolResult.error("编辑验证失败: 文件内容与预期不一致")

        logger.info(f"编辑文件: {path} (1 处替换)")
        return ToolResult.ok(str(path), replacements=1, file_path=str(path))


class CreateDirectoryTool(BaseTool):
    name = "create_directory"
    description = "在本机创建目录，如果父目录不存在会自动递归创建（类似 mkdir -p）。"
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "要创建的目录路径"},
        },
        "required": ["file_path"],
    }

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        dir_path = str(params.get("file_path", "") or params.get("path", ""))
        if not dir_path:
            return ToolResult.schema_error("create_directory 需要 file_path 参数")

        try:
            path = _validate_path(dir_path, for_write=True)
        except ValueError as e:
            return ToolResult.permission_denied(str(e))

        path.mkdir(parents=True, exist_ok=True)
        if not path.exists() or not path.is_dir():
            return ToolResult.error(f"目录创建后验证失败: {path}")

        logger.info(f"创建目录: {path}")
        return ToolResult.ok(str(path), path=str(path))


class ListFilesTool(BaseTool):
    name = "list_files"
    description = "列出本机指定目录下的文件和子目录，支持 glob 过滤模式。仅限本地目录。"
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "本地目录路径", "default": "."},
            "pattern": {"type": "string", "description": "glob 过滤模式，如 *.py 或 src/**/*.ts", "default": "*"},
            "max_depth": {"type": "integer", "description": "最大递归深度", "default": 5},
        },
        "required": [],
    }

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        base = str(params.get("file_path", ".") or ".")
        pattern = str(params.get("pattern", "*") or "*")
        max_depth = int(params.get("max_depth", 5) or 5)

        try:
            path = _validate_path(base, for_write=False)
        except ValueError:
            path = Path(base)

        if not path.exists():
            return ToolResult.error(f"路径不存在: {path}")

        if path.is_file():
            return ToolResult.ok(str(path), count=1, files=[str(path)])

        files = []
        base_depth = len(path.parts)
        for root, dirs, filenames in os.walk(path):
            depth = len(Path(root).parts) - base_depth
            if depth > max_depth:
                dirs.clear()
                continue
            for f in filenames:
                if fnmatch.fnmatch(f, pattern):
                    files.append(str(Path(root) / f))

        display = "\n".join(files) if files else "(空目录)"
        return ToolResult.ok(display, count=len(files), files=files, path=str(path))


class FileMoveTool(BaseTool):
    name = "file_move"
    description = "将文件或文件夹从一个位置移动到另一个位置。可用于重命名、整理文件。注意：移动操作不可撤销。"
    input_schema = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "源文件/文件夹的路径"},
            "destination": {"type": "string", "description": "目标路径（如果目标是目录且已存在，文件会被移入该目录；否则文件会被移动并重命名）"},
        },
        "required": ["source", "destination"],
    }

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        source = str(params.get("source", ""))
        destination = str(params.get("destination", ""))

        if not source:
            return ToolResult.schema_error("file_move 需要 source 参数")
        if not destination:
            return ToolResult.schema_error("file_move 需要 destination 参数")

        try:
            src_path = _validate_path(source, for_write=True)
        except ValueError as e:
            return ToolResult.permission_denied(str(e))

        if not src_path.exists():
            return ToolResult.error(f"源文件不存在: {src_path}", error_type="runtime_error")

        try:
            dst_path = Path(destination).resolve()
            dst_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return ToolResult.error(f"目标路径无效: {e}", error_type="runtime_error")

        try:
            import shutil
            shutil.move(str(src_path), str(dst_path))

            if not dst_path.exists():
                return ToolResult.error(f"移动后验证失败: 目标路径不存在", error_type="runtime_error")

            logger.debug(f"移动文件: {src_path} → {dst_path}")
            return ToolResult.ok(
                f"已移动: {src_path} → {dst_path}",
                source=str(src_path),
                destination=str(dst_path),
            )
        except Exception as e:
            return ToolResult.error(f"移动失败: {e}", error_type="runtime_error")


class FileCopyTool(BaseTool):
    name = "file_copy"
    description = "将文件复制到新位置。源文件保持不变。如果要备份文件或复制模板，请使用此工具。"
    input_schema = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "源文件的路径"},
            "destination": {"type": "string", "description": "目标路径（如果目标是目录且已存在，文件会被复制到该目录下；否则文件会被复制并重命名）"},
        },
        "required": ["source", "destination"],
    }

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        source = str(params.get("source", ""))
        destination = str(params.get("destination", ""))

        if not source:
            return ToolResult.schema_error("file_copy 需要 source 参数")
        if not destination:
            return ToolResult.schema_error("file_copy 需要 destination 参数")

        try:
            src_path = _validate_path(source, for_write=False)
        except ValueError as e:
            return ToolResult.permission_denied(str(e))

        if not src_path.exists():
            return ToolResult.error(f"源文件不存在: {src_path}", error_type="runtime_error")

        try:
            dst_path = Path(destination).resolve()
            dst_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return ToolResult.error(f"目标路径无效: {e}", error_type="runtime_error")

        try:
            import shutil
            if src_path.is_file():
                shutil.copy2(str(src_path), str(dst_path))
            else:
                if dst_path.exists():
                    dst_path = dst_path / src_path.name
                shutil.copytree(str(src_path), str(dst_path))

            logger.debug(f"复制文件: {src_path} → {dst_path}")
            return ToolResult.ok(
                f"已复制: {src_path} → {dst_path}",
                source=str(src_path),
                destination=str(dst_path),
            )
        except Exception as e:
            return ToolResult.error(f"复制失败: {e}", error_type="runtime_error")
