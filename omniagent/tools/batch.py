"""批量操作工具 — BatchWriteTool, BatchEditTool。"""

from __future__ import annotations

import logging
from typing import Any

from omniagent.tools.base import BaseTool, ToolResult
from omniagent.tools.file_ops import _validate_path, MAX_WRITE_SIZE, MAX_VERIFY_SIZE

logger = logging.getLogger(__name__)


class BatchWriteTool(BaseTool):
    name = "batch_write"
    description = "一次性写入多个文件。全部文件先写后验证，返回每个文件的写入结果。"
    input_schema = {
        "type": "object",
        "properties": {
            "files": {
                "type": "array",
                "description": "文件列表，每个元素为 {path: ..., content: ...}",
                "items": {"type": "object", "properties": {
                    "path": {"type": "string"}, "content": {"type": "string"},
                }},
            },
        },
        "required": ["files"],
    }

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        files = params.get("files", [])
        if not files or not isinstance(files, list):
            return ToolResult.schema_error("batch_write 需要 files 参数（数组）")

        results = []
        for i, spec in enumerate(files):
            if not isinstance(spec, dict):
                results.append({"index": i, "success": False, "error": "无效的文件描述"})
                continue

            file_path = str(spec.get("path", "") or spec.get("file_path", ""))
            content = str(spec.get("content", ""))

            if not file_path:
                results.append({"index": i, "success": False, "error": "缺少 path"})
                continue

            try:
                path = _validate_path(file_path, for_write=True)
            except ValueError as e:
                results.append({"index": i, "path": file_path, "success": False, "error": str(e)})
                continue

            content_bytes = len(content.encode("utf-8"))
            if content_bytes > MAX_WRITE_SIZE:
                results.append({"index": i, "path": str(path), "success": False, "error": f"内容过大: {content_bytes} 字节"})
                continue

            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

            # 验证
            if path.stat().st_size <= MAX_VERIFY_SIZE:
                actual = path.read_text(encoding="utf-8")
                if actual != content:
                    results.append({"index": i, "path": str(path), "success": False, "error": "内容验证失败"})
                    continue

            results.append({"index": i, "path": str(path), "success": True, "bytes": content_bytes})

        success_count = sum(1 for r in results if r.get("success"))
        display = f"批量写入: {success_count}/{len(files)} 成功\n"
        for r in results:
            status = "✓" if r.get("success") else "✗"
            display += f"  {status} [{r.get('index')}] {r.get('path', '?')}"
            if r.get("error"):
                display += f" — {r['error']}"
            display += "\n"

        return ToolResult.ok(
            display,
            total=len(files), success_count=success_count,
            all_success=success_count == len(files),
            results=results,
        )


class BatchEditTool(BaseTool):
    name = "batch_edit"
    description = "一次性编辑多个文件，每个编辑操作独立执行和验证。适合跨文件重构。"
    input_schema = {
        "type": "object",
        "properties": {
            "edits": {
                "type": "array",
                "description": "编辑列表，每个元素为 {file_path: ..., old_text: ..., new_text: ...}",
                "items": {"type": "object", "properties": {
                    "file_path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                }},
            },
        },
        "required": ["edits"],
    }

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        edits = params.get("edits", [])
        if not edits or not isinstance(edits, list):
            return ToolResult.schema_error("batch_edit 需要 edits 参数（数组）")

        results = []
        for i, spec in enumerate(edits):
            if not isinstance(spec, dict):
                results.append({"index": i, "success": False, "error": "无效的编辑描述"})
                continue

            file_path = str(spec.get("file_path", ""))
            old_text = str(spec.get("old_text", ""))
            new_text = str(spec.get("new_text", ""))

            if not file_path or not old_text:
                results.append({"index": i, "success": False, "error": "缺少 file_path 或 old_text"})
                continue

            try:
                path = _validate_path(file_path, for_write=True)
            except ValueError as e:
                results.append({"index": i, "file": file_path, "success": False, "error": str(e)})
                continue

            if not path.exists():
                results.append({"index": i, "file": str(path), "success": False, "error": "文件不存在"})
                continue

            content = path.read_text(encoding="utf-8")
            count = content.count(old_text)
            if count == 0:
                results.append({"index": i, "file": str(path), "success": False, "error": "未找到匹配文本"})
            elif count > 1:
                results.append({"index": i, "file": str(path), "success": False, "error": f"找到 {count} 处匹配"})
            else:
                new_content = content.replace(old_text, new_text, 1)
                path.write_text(new_content, encoding="utf-8")
                results.append({"index": i, "file": str(path), "success": True, "replacements": 1})

        success_count = sum(1 for r in results if r.get("success"))
        display = f"批量编辑: {success_count}/{len(edits)} 成功\n"
        for r in results:
            status = "✓" if r.get("success") else "✗"
            display += f"  {status} [{r.get('index')}] {r.get('file', '?')}"
            if r.get("error"):
                display += f" — {r['error']}"
            display += "\n"

        return ToolResult.ok(
            display,
            total=len(edits), success_count=success_count,
            all_success=success_count == len(edits),
            results=results,
        )
