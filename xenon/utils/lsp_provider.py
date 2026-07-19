"""
LSP Provider — 基于 Jedi 的 Python 代码智能。

提供跨文件导航能力，不作为外部语言服务器进程运行，
而是直接通过 Jedi 库进行静态分析。

支持的操作:
- goto_definition: 跳转到符号定义
- find_references: 查找所有引用
- get_hover: 获取悬停信息（类型、文档）
- get_diagnostics: 获取诊断信息（语法错误、警告）
- get_symbols: 获取文件/项目的符号列表
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import jedi

logger = logging.getLogger(__name__)

# jedi 不支持的文件扩展名（避免浪费时间分析非 Python 文件）
_SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({".py", ".pyi", ".pyx", ".pxd"})


class LSPProvider:
    """基于 Jedi 的代码智能提供者。

    所有方法都是静态方法，不需要初始化服务器连接。
    """

    @staticmethod
    def _resolve_path(file_path: str) -> Path:
        """解析文件路径，支持 ~ 展开。"""
        return Path(file_path).expanduser().resolve()

    @staticmethod
    def _check_supported(path: Path) -> str | None:
        """检查文件是否受支持，返回错误消息或 None。"""
        if not path.exists():
            return f"文件不存在: {path}"
        if path.suffix.lower() not in _SUPPORTED_EXTENSIONS:
            return f"不支持的文件类型: {path.suffix}（仅支持 Python 文件）"
        return None

    @staticmethod
    def goto_definition(
        file_path: str,
        line: int,
        column: int,
        *,
        follow_imports: bool = True,
    ) -> dict[str, Any]:
        """跳转到指定位置的符号定义。

        Args:
            file_path: 源文件路径
            line: 行号（1-based）
            column: 列号（0-based）
            follow_imports: 是否跟踪 import 语句到其他文件

        Returns:
            包含定义位置和代码片段的结果字典。
        """
        path = LSPProvider._resolve_path(file_path)
        err = LSPProvider._check_supported(path)
        if err:
            return {"success": False, "error": err}

        try:
            source = path.read_text(encoding="utf-8")
        except Exception as e:
            return {"success": False, "error": f"无法读取文件: {e}"}

        try:
            script = jedi.Script(source, path=str(path))
            definitions = script.goto(line, column, follow_imports=follow_imports)
        except Exception as e:
            logger.warning(f"jedi goto_definition 失败: {e}")
            return {"success": False, "error": f"分析失败: {e}"}

        if not definitions:
            return {
                "success": True,
                "found": False,
                "file_path": str(path),
                "line": line,
                "column": column,
                "message": "未找到定义（可能是内置函数或动态生成的符号）",
            }

        results = []
        for d in definitions[:20]:  # 最多返回 20 个定义
            def_path = d.module_path or ""
            def_line = d.line or 0
            def_column = d.column or 0
            code_snippet = ""
            if def_path and def_line:
                try:
                    lines = Path(def_path).read_text(encoding="utf-8").splitlines()
                    start = max(0, def_line - 1)
                    end = min(len(lines), def_line + 4)
                    code_snippet = "\n".join(
                        f"{i+1:4d}| {lines[i]}" for i in range(start, end)
                    )
                except Exception:
                    pass

            results.append({
                "name": d.name,
                "type": d.type,
                "module_path": def_path,
                "line": def_line,
                "column": def_column,
                "description": d.description,
                "docstring": d.docstring(raw=True)[:500] if d.docstring() else "",
                "code_snippet": code_snippet,
            })

        return {
            "success": True,
            "found": True,
            "file_path": str(path),
            "line": line,
            "column": column,
            "count": len(results),
            "definitions": results,
        }

    @staticmethod
    def find_references(
        file_path: str,
        line: int,
        column: int,
        *,
        scope: str = "project",
    ) -> dict[str, Any]:
        """查找指定位置符号的所有引用。

        Args:
            file_path: 源文件路径
            line: 行号（1-based）
            column: 列号（0-based）
            scope: 搜索范围 — "file"（仅当前文件）或 "project"（整个项目）

        Returns:
            包含所有引用位置的结果字典。
        """
        path = LSPProvider._resolve_path(file_path)
        err = LSPProvider._check_supported(path)
        if err:
            return {"success": False, "error": err}

        try:
            source = path.read_text(encoding="utf-8")
        except Exception as e:
            return {"success": False, "error": f"无法读取文件: {e}"}

        try:
            script = jedi.Script(source, path=str(path))
            references = script.get_references(line, column)
        except Exception as e:
            logger.warning(f"jedi find_references 失败: {e}")
            return {"success": False, "error": f"分析失败: {e}"}

        if not references:
            return {
                "success": True,
                "found": False,
                "file_path": str(path),
                "line": line,
                "column": column,
                "message": "未找到引用",
            }

        results = []
        for ref in references[:50]:  # 最多 50 个引用
            ref_path = ref.module_path or ""
            ref_line = ref.line or 0
            ref_column = ref.column or 0
            code_line = ""
            if ref_path and ref_line:
                try:
                    code_line = (
                        Path(ref_path)
                        .read_text(encoding="utf-8")
                        .splitlines()[ref_line - 1]
                        .strip()
                    )
                except Exception:
                    pass

            results.append({
                "name": ref.name,
                "module_path": ref_path,
                "line": ref_line,
                "column": ref_column,
                "code_line": code_line,
                "in_same_file": ref_path == str(path),
            })

        # 按文件分组统计
        file_groups: dict[str, int] = {}
        for r in results:
            key = r["module_path"] or "(unknown)"
            file_groups[key] = file_groups.get(key, 0) + 1

        return {
            "success": True,
            "found": True,
            "file_path": str(path),
            "line": line,
            "column": column,
            "count": len(results),
            "by_file": file_groups,
            "references": results,
        }

    @staticmethod
    def get_hover(file_path: str, line: int, column: int) -> dict[str, Any]:
        """获取指定位置的悬停信息（类型、文档字符串）。

        Args:
            file_path: 源文件路径
            line: 行号（1-based）
            column: 列号（0-based）

        Returns:
            包含类型信息和文档的结果字典。
        """
        path = LSPProvider._resolve_path(file_path)
        err = LSPProvider._check_supported(path)
        if err:
            return {"success": False, "error": err}

        try:
            source = path.read_text(encoding="utf-8")
        except Exception as e:
            return {"success": False, "error": f"无法读取文件: {e}"}

        try:
            script = jedi.Script(source, path=str(path))
            # 使用 infer 获取类型推断
            signatures = script.get_signatures(line, column)
            helps = script.help(line, column)
        except Exception as e:
            logger.warning(f"jedi hover 失败: {e}")
            return {"success": False, "error": f"分析失败: {e}"}

        # 收集签名信息
        sig_list = []
        for sig in signatures[:5]:
            params = []
            for p in sig.params:
                params.append({
                    "name": p.name,
                    "description": p.description,
                    "infer_type": getattr(p, "infer_type", ""),
                })
            sig_list.append({
                "name": sig.name,
                "description": sig.description,
                "params": params,
                "docstring": sig.docstring(raw=True)[:500] if sig.docstring() else "",
            })

        help_text = ""
        if helps:
            help_text = "\n".join(h.description for h in helps[:10] if h.description)

        # 也获取符号的类型信息
        try:
            names = script.infer(line, column)
            type_info = []
            for n in names[:5]:
                type_info.append({
                    "name": n.name,
                    "type": n.type,
                    "description": n.description,
                    "docstring": n.docstring(raw=True)[:300] if n.docstring() else "",
                })
        except Exception:
            type_info = []

        return {
            "success": True,
            "file_path": str(path),
            "line": line,
            "column": column,
            "signatures": sig_list,
            "type_info": type_info,
            "help": help_text[:1000] if help_text else "",
        }

    @staticmethod
    def get_diagnostics(file_path: str) -> dict[str, Any]:
        """获取文件的诊断信息（语法错误、警告等）。

        Args:
            file_path: 源文件路径

        Returns:
            包含诊断列表的结果字典。错误为 Error 级别，警告为 Warning。
        """
        path = LSPProvider._resolve_path(file_path)
        err = LSPProvider._check_supported(path)
        if err:
            return {"success": False, "error": err}

        try:
            source = path.read_text(encoding="utf-8")
        except Exception as e:
            return {"success": False, "error": f"无法读取文件: {e}"}

        diagnostics: list[dict[str, Any]] = []

        try:
            script = jedi.Script(source, path=str(path))
            # jedi 的 get_syntax_errors 返回语法错误
            errors = script.get_syntax_errors()
            for e in errors:
                diagnostics.append({
                    "severity": "Error",
                    "line": e.line,
                    "column": e.column,
                    "message": e.message,
                    "until_line": getattr(e, "until_line", e.line),
                    "until_column": getattr(e, "until_column", e.column + 1),
                })
        except Exception as e:
            logger.warning(f"jedi diagnostics 失败: {e}")

        return {
            "success": True,
            "file_path": str(path),
            "total_lines": len(source.splitlines()),
            "error_count": sum(1 for d in diagnostics if d["severity"] == "Error"),
            "warning_count": sum(1 for d in diagnostics if d["severity"] == "Warning"),
            "diagnostics": diagnostics,
        }

    @staticmethod
    def get_symbols(
        file_path: str,
        *,
        include_imports: bool = False,
    ) -> dict[str, Any]:
        """获取文件中所有符号（函数、类、变量）。

        Args:
            file_path: Python 文件路径
            include_imports: 是否包含 import 的符号

        Returns:
            按类型分组的符号列表。
        """
        path = LSPProvider._resolve_path(file_path)
        err = LSPProvider._check_supported(path)
        if err:
            return {"success": False, "error": err}

        try:
            source = path.read_text(encoding="utf-8")
        except Exception as e:
            return {"success": False, "error": f"无法读取文件: {e}"}

        try:
            script = jedi.Script(source, path=str(path))
            names = script.get_names(all_scopes=True, definitions=True, references=False)
        except Exception as e:
            logger.warning(f"jedi get_symbols 失败: {e}")
            return {"success": False, "error": f"分析失败: {e}"}

        by_type: dict[str, list[dict[str, Any]]] = {}
        for name in names:
            if name.type == "import" and not include_imports:
                continue
            entry = {
                "name": name.name,
                "line": name.line,
                "column": name.column,
                "description": name.description,
                "docstring": name.docstring(raw=True)[:200] if name.docstring() else "",
            }
            if name.type not in by_type:
                by_type[name.type] = []
            by_type[name.type].append(entry)

        total = sum(len(v) for v in by_type.values())

        return {
            "success": True,
            "file_path": str(path),
            "total_symbols": total,
            "by_type": {k: len(v) for k, v in sorted(by_type.items())},
            "symbols": {k: v for k, v in sorted(by_type.items())},
        }
