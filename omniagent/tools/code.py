"""代码分析工具 — CodeIndexTool, AstAnalyzeTool, RefactorTool, DiffPreviewTool。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from omniagent.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


class CodeIndexTool(BaseTool):
    name = "code_index"
    description = "基于 AST 解析搜索项目中的代码符号（函数定义、类定义、变量名）。返回符号名称、所在文件和行号。仅支持 Python 文件。"
    input_schema = {
        "type": "object",
        "properties": {
            "search_pattern": {"type": "string", "description": "要搜索的符号名或部分关键词"},
            "file_path": {"type": "string", "description": "索引的根目录（可选，默认当前目录）"},
        },
        "required": ["search_pattern"],
    }

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        query = str(params.get("search_pattern", "") or params.get("symbol", ""))
        root = str(params.get("file_path", ".") or ".")

        if not query:
            return ToolResult.schema_error("code_index 需要 search_pattern 参数")

        try:
            from omniagent.utils.code_index import CodeIndex
        except ImportError:
            return ToolResult.error("code_index 模块不可用", error_type="runtime_error")

        index = CodeIndex(root)
        count = index.build(max_files=200)
        results = index.search(query, limit=30)

        matches = [{
            "name": sym.name, "kind": sym.kind,
            "file": sym.file_path, "line": sym.line,
            "parent": sym.parent or "", "signature": sym.signature,
        } for sym in results]

        display = f"索引 {index.stats()['files']} 个文件，搜索 '{query}': 找到 {len(matches)} 个匹配\n"
        for m in matches[:20]:
            sig = f"({m['signature']})" if m['signature'] else ""
            display += f"  {m['kind']} {m['name']}{sig} @ {m['file']}:{m['line']}\n"

        return ToolResult.ok(display, matches=matches, query=query)


class AstAnalyzeTool(BaseTool):
    name = "ast_analyze"
    description = "对 Python 文件进行 AST 深度分析：提取所有函数签名、类结构、圈复杂度、未使用的 import。仅支持 .py 文件。"
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "要分析的 Python 文件路径"},
        },
        "required": ["file_path"],
    }

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        file_path = str(params.get("file_path", ""))

        if not file_path:
            return ToolResult.schema_error("ast_analyze 需要 file_path 参数")

        path = Path(file_path)
        if not path.exists():
            return ToolResult.error(f"文件不存在: {path}")

        try:
            from omniagent.utils.ast_analyzer import ASTAnalyzer
        except ImportError:
            return ToolResult.error("ast_analyze 模块不可用", error_type="runtime_error")

        analyzer = ASTAnalyzer()
        try:
            result = analyzer.analyze_file(path)
        except Exception as e:
            return ToolResult.error(f"分析失败: {e}")

        display = f"分析: {path}\n"
        display += f"  函数: {len(result.functions)}, 类: {len(result.classes)}\n"
        display += f"  复杂度: {result.complexity}, 未使用导入: {len(result.unused_imports)}\n"

        if result.functions:
            for f in result.functions[:20]:
                display += f"  def {f.name}({', '.join(f.args)}) @ 行{f.line} [复杂度:{f.complexity}]\n"

        return ToolResult.ok(
            display,
            syntax_valid=result.syntax_valid,
            functions=len(result.functions),
            classes=len(result.classes),
        )


class RefactorTool(BaseTool):
    name = "refactor"
    description = "代码重构工具。rename: 跨文件精确重命名符号；clean_imports: 删除未使用的 import；analyze: 分析文件的重构建议。"
    input_schema = {
        "type": "object",
        "properties": {
            "refactor_action": {
                "type": "string",
                "description": "rename | clean_imports | analyze",
            },
            "file_path": {"type": "string", "description": "目标文件路径"},
            "old_name": {"type": "string", "description": "旧符号名（rename 时必填）"},
            "new_name": {"type": "string", "description": "新符号名（rename 时必填）"},
        },
        "required": ["refactor_action"],
    }

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        action = str(params.get("refactor_action", "analyze"))
        file_path = str(params.get("file_path", ""))

        try:
            from omniagent.utils.refactor import RefactorEngine
        except ImportError:
            return ToolResult.error("refactor 模块不可用", error_type="runtime_error")

        root = str(Path(file_path).parent) if file_path and Path(file_path).is_file() else (file_path or ".")

        engine = RefactorEngine(root)
        engine.build_index(max_files=200)

        if action == "rename":
            old_name = str(params.get("old_name", ""))
            new_name = str(params.get("new_name", ""))
            if not old_name or not new_name:
                return ToolResult.schema_error("rename 需要 old_name 和 new_name 参数")
            result = engine.rename_symbol(old_name, new_name)
            display = f"重命名 '{old_name}' → '{new_name}': 修改 {len(result['changes'])} 处"
            return ToolResult.ok(display, **result)

        elif action == "clean_imports":
            if not file_path:
                return ToolResult.schema_error("clean_imports 需要 file_path 参数")
            result = engine.clean_unused_imports(file_path)
            display = f"清理导入: {file_path}, 移除 {len(result.get('removed', []))} 个"
            return ToolResult.ok(display, **result)

        elif action == "analyze":
            if not file_path:
                return ToolResult.schema_error("analyze 需要 file_path 参数")
            result = engine.analyze_for_refactor(file_path)
            return ToolResult.ok(result["summary"], suggestions=result.get("suggestions", []))

        return ToolResult.schema_error(f"未知 refactor_action: {action}")


class DiffPreviewTool(BaseTool):
    name = "diff_preview"
    description = "预览对文件的修改效果（生成 diff），但不实际修改文件。用于在执行 edit_file 前确认修改是否正确。"
    input_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "要预览修改的文件路径"},
            "old_text": {"type": "string", "description": "要被替换的原文"},
            "new_text": {"type": "string", "description": "替换后的新文"},
        },
        "required": ["file_path"],
    }

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        import difflib

        file_path = str(params.get("file_path", ""))
        old_text = str(params.get("old_text", ""))
        new_text = str(params.get("new_text", ""))

        if not file_path:
            return ToolResult.schema_error("diff_preview 需要 file_path 参数")

        path = Path(file_path)

        if old_text and new_text:
            if not path.exists():
                return ToolResult.error(f"文件不存在: {path}")
            content = path.read_text(encoding="utf-8")
            if old_text not in content:
                return ToolResult.error("未找到匹配文本")
            new_content = content.replace(old_text, new_text, 1)
            old_lines = content.splitlines(keepends=True)
            new_lines = new_content.splitlines(keepends=True)
        elif new_text:
            old_lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
            new_lines = new_text.splitlines(keepends=True)
            old_text = "(原始文件)"
        else:
            return ToolResult.schema_error("需要 old_text/new_text 或 content 参数")

        diff = list(difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"a/{path.name}", tofile=f"b/{path.name}", lineterm="",
        ))
        diff_text = "\n".join(diff) if diff else "(无变化)"

        return ToolResult.ok(diff_text, has_changes=len(diff) > 0, file=str(path))
