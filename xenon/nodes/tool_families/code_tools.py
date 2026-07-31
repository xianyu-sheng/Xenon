"""Code indexing, AST inspection, refactoring and diff preview tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from xenon.engine.context import AgentContext


class CodeToolsMixin:
    """Expose code-analysis tools through the ToolNode host contract."""

    def _code_index(self, context: AgentContext) -> dict[str, Any]:
        """代码索引搜索。"""
        from xenon.utils.code_index import CodeIndex

        query = self._resolve_template(self.search_pattern or self.symbol or "", context)
        file_path = self._resolve_template(self.file_path or "", context)

        if not query:
            return {
                "action_type": "code_index",
                "success": False,
                "error": "需要 search_pattern 或 symbol 参数",
            }

        # 确定索引根目录
        root = file_path if file_path and Path(file_path).is_dir() else "."
        try:
            root = str(self._validate_path(root, for_write=False))
        except Exception:
            root = "."

        index = CodeIndex(root)
        index.build(max_files=200)
        results = index.search(query, limit=30)
        stats = index.stats()

        matches = []
        for sym in results:
            matches.append({
                "name": sym.name,
                "kind": sym.kind,
                "file": sym.file_path,
                "line": sym.line,
                "parent": sym.parent or "",
                "signature": sym.signature,
            })

        display = f"索引 {stats['files']} 个文件, {stats['symbols']} 个符号\n"
        display += f"搜索 '{query}': 找到 {len(matches)} 个匹配\n"
        for m in matches[:20]:
            parent = f"{m['parent']}." if m['parent'] else ""
            sig = f"({m['signature']})" if m['signature'] else ""
            display += f"  {m['kind']} {parent}{m['name']}{sig} @ {m['file']}:{m['line']}\n"

        result = {
            "action_type": "code_index",
            "query": query,
            "total_files": stats["files"],
            "total_symbols": stats["symbols"],
            "matches": matches,
            "success": True,
        }
        self._write_output(context, display)
        return result

    def _ast_analyze(self, context: AgentContext) -> dict[str, Any]:
        """AST 代码分析。"""
        from xenon.utils.ast_analyzer import ASTAnalyzer

        file_path = self._resolve_template(self.file_path or "", context)
        if not file_path:
            return {
                "action_type": "ast_analyze",
                "success": False,
                "error": "需要 file_path 参数",
            }

        path = self._validate_path(file_path, for_write=False)
        if not path.exists():
            return {
                "action_type": "ast_analyze",
                "success": False,
                "error": f"文件不存在: {path}",
            }

        analyzer = ASTAnalyzer()
        try:
            result = analyzer.analyze_file(path)
        except Exception as e:
            return {
                "action_type": "ast_analyze",
                "success": False,
                "error": f"分析失败: {e}",
            }

        display = result.summary()

        # 函数签名
        if result.functions:
            display += "\n\n函数:\n"
            for f in result.functions[:20]:
                async_kw = "async " if f.is_async else ""
                display += f"  {async_kw}def {f.name}({', '.join(f.args)}) -> {f.return_annotation or 'None'} @ 行{f.line} [复杂度:{f.complexity}]\n"

        # 类
        if result.classes:
            display += "\n\n类:\n"
            for c in result.classes[:10]:
                bases = f"({', '.join(c.bases)})" if c.bases else ""
                display += f"  class {c.name}{bases} @ 行{c.line}, {len(c.methods)} 个方法\n"

        ret = {
            "action_type": "ast_analyze",
            "file": str(path),
            "syntax_valid": result.syntax_valid,
            "functions": len(result.functions),
            "classes": len(result.classes),
            "complexity": result.complexity,
            "unused_imports": result.unused_imports,
            "success": True,
        }
        self._write_output(context, display)
        return ret

    def _refactor(self, context: AgentContext) -> dict[str, Any]:
        """代码重构操作。"""
        from xenon.utils.refactor import RefactorEngine

        action = self._resolve_template(self.refactor_action, context)
        file_path = self._resolve_template(self.file_path or "", context)

        if not action:
            return {
                "action_type": "refactor",
                "success": False,
                "error": "需要 refactor_action 参数: rename | clean_imports | analyze",
            }

        # 确定项目根目录
        root = "."
        if file_path and Path(file_path).is_dir():
            root = str(file_path)
        elif file_path:
            root = str(Path(file_path).parent)

        try:
            root = str(self._validate_path(root, for_write=False))
        except Exception:
            root = "."

        engine = RefactorEngine(root)
        engine.build_index(max_files=200)

        if action == "rename":
            old_name = self._resolve_template(self.old_name, context)
            new_name = self._resolve_template(self.new_name, context)
            if not old_name or not new_name:
                return {
                    "action_type": "refactor",
                    "success": False,
                    "error": "rename 需要 old_name 和 new_name 参数",
                }
            # A8: rename 默认单文件作用域，防 LLM 跨全部文件盲目文本重命名误改同名符号
            if not file_path or not Path(file_path).is_file():
                return {
                    "action_type": "refactor",
                    "success": False,
                    "error": "rename 需指定 file_path（单文件作用域重命名）；跨文件批量重命名易误改其他模块同名符号，已禁用",
                }
            result = engine.rename_symbol(old_name, new_name, definition_file=file_path)
            display = f"重命名 '{old_name}' → '{new_name}'\n"
            display += f"修改 {len(result['changes'])} 处\n"
            if result["errors"]:
                display += f"错误: {'; '.join(result['errors'])}\n"
            self._write_output(context, display)
            return {"action_type": "refactor", "refactor_action": "rename", **result}

        elif action == "clean_imports":
            if not file_path:
                return {
                    "action_type": "refactor",
                    "success": False,
                    "error": "clean_imports 需要 file_path 参数",
                }
            result = engine.clean_unused_imports(file_path)
            display = f"清理导入: {file_path}\n"
            if result.get("removed"):
                display += f"移除 {len(result['removed'])} 个未使用导入\n"
            else:
                display += "没有未使用的导入\n"
            self._write_output(context, display)
            return {"action_type": "refactor", "refactor_action": "clean_imports", **result}

        elif action == "analyze":
            if not file_path:
                return {
                    "action_type": "refactor",
                    "success": False,
                    "error": "analyze 需要 file_path 参数",
                }
            result = engine.analyze_for_refactor(file_path)
            display = result["summary"]
            if result["suggestions"]:
                display += "\n\n重构建议:\n"
                for s in result["suggestions"]:
                    display += f"  [{s['type']}] {s['message']}\n"
            self._write_output(context, display)
            return {"action_type": "refactor", "refactor_action": "analyze", **result}

        else:
            return {
                "action_type": "refactor",
                "success": False,
                "error": f"未知 refactor_action: {action}。支持: rename | clean_imports | analyze",
            }

    def _diff_preview(self, context: AgentContext) -> dict[str, Any]:
        """生成 diff 预览（不实际修改文件）。"""
        import difflib

        file_path = self._resolve_template(self.file_path or "", context)
        old_text = self._resolve_template(self.old_text, context)
        new_text = self._resolve_template(self.new_text, context)

        if not file_path:
            return {
                "action_type": "diff_preview",
                "success": False,
                "error": "需要 file_path 参数",
            }

        path = self._validate_path(file_path, for_write=False)

        if old_text and new_text:
            # edit 模式：展示替换 diff
            if not path.exists():
                return {
                    "action_type": "diff_preview",
                    "success": False,
                    "error": f"文件不存在: {path}",
                }
            content = path.read_text(encoding=self.encoding)
            if old_text not in content:
                return {
                    "action_type": "diff_preview",
                    "success": False,
                    "error": "未找到匹配文本",
                }
            new_content = content.replace(old_text, new_text, 1)
        elif new_text or self.content:
            # write 模式：展示新文件 diff
            target_content = new_text or self.content or ""
            content = path.read_text(encoding=self.encoding) if path.exists() else ""
            new_content = target_content
        else:
            return {
                "action_type": "diff_preview",
                "success": False,
                "error": "需要 old_text/new_text 或 content 参数",
            }

        # 生成 diff
        old_lines = content.splitlines(keepends=True)
        new_lines = new_content.splitlines(keepends=True)
        diff = list(difflib.unified_diff(
            old_lines, new_lines,
            fromfile=f"a/{Path(file_path).name}",
            tofile=f"b/{Path(file_path).name}",
            lineterm="",
        ))

        diff_text = "\n".join(diff) if diff else "(无变化)"

        result = {
            "action_type": "diff_preview",
            "file": str(path),
            "diff": diff_text,
            "has_changes": len(diff) > 0,
            "success": True,
        }
        self._write_output(context, diff_text)
        return result

