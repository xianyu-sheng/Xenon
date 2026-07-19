"""
AST Analyzer — Python 代码结构分析器。

提供深度代码理解能力：
- 函数/类结构分析
- 代码复杂度估算
- 语法错误检测
- 未使用导入检测
- 代码质量指标
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class FunctionInfo:
    """函数详细信息。"""
    name: str
    args: list[str]
    defaults: list[str]
    return_annotation: str
    decorators: list[str]
    docstring: str
    line: int
    end_line: int | None
    is_async: bool
    is_method: bool
    complexity: int  # 圈复杂度
    local_vars: list[str]
    calls: list[str]  # 调用的函数


@dataclass
class ClassInfo:
    """类详细信息。"""
    name: str
    bases: list[str]
    methods: list[FunctionInfo]
    class_vars: list[str]
    decorators: list[str]
    docstring: str
    line: int
    end_line: int | None


@dataclass
class AnalysisResult:
    """文件分析结果。"""
    file_path: str
    language: str
    encoding: str
    syntax_valid: bool
    syntax_errors: list[str]
    functions: list[FunctionInfo]
    classes: list[ClassInfo]
    top_level_vars: list[str]
    imports: list[dict[str, Any]]
    unused_imports: list[str]
    complexity: int  # 文件总复杂度
    lines: int
    blank_lines: int
    comment_lines: int
    docstring_lines: int

    def summary(self) -> str:
        """生成分析摘要。"""
        lines = [
            f"文件: {self.file_path}",
            f"语言: {self.language}",
            f"行数: {self.lines} (空行: {self.blank_lines}, 注释: {self.comment_lines}, 文档: {self.docstring_lines})",
            f"语法: {'✓ 有效' if self.syntax_valid else '✗ 错误'}",
        ]
        if self.syntax_errors:
            for err in self.syntax_errors:
                lines.append(f"  错误: {err}")
        lines.append(f"函数: {len(self.functions)}")
        lines.append(f"类: {len(self.classes)}")
        lines.append(f"导入: {len(self.imports)}")
        if self.unused_imports:
            lines.append(f"未使用导入: {', '.join(self.unused_imports)}")
        lines.append(f"复杂度: {self.complexity}")
        return "\n".join(lines)


class ASTAnalyzer:
    """Python 代码分析器。"""

    def analyze_file(self, file_path: str | Path) -> AnalysisResult:
        """分析单个文件。"""
        path = Path(file_path)
        try:
            content = path.read_text(encoding="utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            try:
                content = path.read_text(encoding="latin-1")
                encoding = "latin-1"
            except Exception:
                return self._error_result(str(path), f"文件读取失败: {path}")
        except FileNotFoundError:
            return self._error_result(str(path), f"文件不存在: {path}")
        except PermissionError:
            return self._error_result(str(path), f"无权限读取: {path}")
        except Exception as e:
            return self._error_result(str(path), f"文件读取失败: {e}")

        return self.analyze_code(content, str(path), encoding)

    @staticmethod
    def _error_result(file_path: str, error: str) -> AnalysisResult:
        """生成错误结果。"""
        return AnalysisResult(
            file_path=file_path, language="python", encoding="utf-8",
            syntax_valid=False, syntax_errors=[error],
            functions=[], classes=[], top_level_vars=[],
            imports=[], unused_imports=[], complexity=0,
            lines=0, blank_lines=0, comment_lines=0, docstring_lines=0,
        )

    def analyze_code(self, code: str, file_path: str = "<string>", encoding: str = "utf-8") -> AnalysisResult:
        """分析代码字符串。"""
        lines = code.splitlines()
        total_lines = len(lines)
        blank_lines = sum(1 for l in lines if not l.strip())
        comment_lines = sum(1 for l in lines if l.strip().startswith("#"))

        # 尝试解析
        syntax_valid = True
        syntax_errors = []
        tree = None
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            syntax_valid = False
            syntax_errors.append(f"行 {e.lineno}: {e.msg}")

        if tree is None:
            return AnalysisResult(
                file_path=file_path, language="python", encoding=encoding,
                syntax_valid=False, syntax_errors=syntax_errors,
                functions=[], classes=[], top_level_vars=[],
                imports=[], unused_imports=[], complexity=0,
                lines=total_lines, blank_lines=blank_lines,
                comment_lines=comment_lines, docstring_lines=0,
            )

        # 分析
        functions = self._extract_functions(tree)
        classes = self._extract_classes(tree)
        top_vars = self._extract_top_vars(tree)
        imports = self._extract_imports(tree)
        unused = self._find_unused_imports(tree, code)
        complexity = self._calc_complexity(tree)
        docstring_lines = self._count_docstring_lines(tree)

        return AnalysisResult(
            file_path=file_path, language="python", encoding=encoding,
            syntax_valid=syntax_valid, syntax_errors=syntax_errors,
            functions=functions, classes=classes, top_level_vars=top_vars,
            imports=imports, unused_imports=unused, complexity=complexity,
            lines=total_lines, blank_lines=blank_lines,
            comment_lines=comment_lines, docstring_lines=docstring_lines,
        )

    def check_syntax(self, code: str) -> list[str]:
        """检查语法错误，返回错误列表。"""
        errors = []
        try:
            ast.parse(code)
        except SyntaxError as e:
            errors.append(f"行 {e.lineno}: {e.msg}")
        return errors

    def extract_signatures(self, code: str) -> list[dict[str, str | bool]]:
        """提取所有函数签名。"""
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return []

        sigs = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = []
                # positional-only
                for arg in getattr(node.args, 'posonlyargs', []):
                    if arg.arg not in ("self", "cls"):
                        a = arg.arg
                        if arg.annotation:
                            a += f": {ast.unparse(arg.annotation)}"
                        args.append(a)
                # positional
                for arg in node.args.args:
                    if arg.arg not in ("self", "cls"):
                        a = arg.arg
                        if arg.annotation:
                            a += f": {ast.unparse(arg.annotation)}"
                        args.append(a)
                # *args
                if node.args.vararg:
                    va = node.args.vararg
                    a = f"*{va.arg}"
                    if va.annotation:
                        a += f": {ast.unparse(va.annotation)}"
                    args.append(a)
                # keyword-only
                for arg in node.args.kwonlyargs:
                    a = arg.arg
                    if arg.annotation:
                        a += f": {ast.unparse(arg.annotation)}"
                    args.append(a)
                # **kwargs
                if node.args.kwarg:
                    ka = node.args.kwarg
                    a = f"**{ka.arg}"
                    if ka.annotation:
                        a += f": {ast.unparse(ka.annotation)}"
                    args.append(a)
                ret = ""
                if node.returns:
                    ret = ast.unparse(node.returns)
                sigs.append({
                    "name": node.name,
                    "args": ", ".join(args),
                    "return": ret,
                    "line": str(node.lineno),
                    "async": isinstance(node, ast.AsyncFunctionDef),
                })
        return sigs

    # ── 内部方法 ──────────────────────────────────────────

    def _extract_functions(self, tree: ast.Module) -> list[FunctionInfo]:
        """提取所有顶层函数。"""
        functions = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append(self._analyze_function(node, is_method=False))
        return functions

    def _extract_classes(self, tree: ast.Module) -> list[ClassInfo]:
        """提取所有类。"""
        classes = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                methods = []
                class_vars = []
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        methods.append(self._analyze_function(child, is_method=True))
                    elif isinstance(child, ast.Assign):
                        for target in child.targets:
                            if isinstance(target, ast.Name):
                                class_vars.append(target.id)

                bases = []
                for base in node.bases:
                    bases.append(ast.unparse(base))

                decorators = [ast.unparse(d) for d in node.decorator_list]

                classes.append(ClassInfo(
                    name=node.name,
                    bases=bases,
                    methods=methods,
                    class_vars=class_vars,
                    decorators=decorators,
                    docstring=ast.get_docstring(node) or "",
                    line=node.lineno,
                    end_line=getattr(node, 'end_lineno', None),
                ))
        return classes

    def _analyze_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, is_method: bool) -> FunctionInfo:
        """分析单个函数。"""
        args = []
        for arg in node.args.args:
            if arg.arg not in ("self", "cls"):
                args.append(arg.arg)

        defaults = []
        for d in node.args.defaults:
            defaults.append(ast.unparse(d))

        ret = ast.unparse(node.returns) if node.returns else ""
        decorators = [ast.unparse(d) for d in node.decorator_list]

        # 局部变量
        local_vars = []
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                if child.id not in args:
                    local_vars.append(child.id)

        # 调用的函数
        calls = []
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                if isinstance(child.func, ast.Name):
                    calls.append(child.func.id)
                elif isinstance(child.func, ast.Attribute):
                    calls.append(ast.unparse(child.func))

        complexity = self._node_complexity(node)

        return FunctionInfo(
            name=node.name,
            args=args,
            defaults=defaults,
            return_annotation=ret,
            decorators=decorators,
            docstring=ast.get_docstring(node) or "",
            line=node.lineno,
            end_line=getattr(node, 'end_lineno', None),
            is_async=isinstance(node, ast.AsyncFunctionDef),
            is_method=is_method,
            complexity=complexity,
            local_vars=list(dict.fromkeys(local_vars)),
            calls=list(dict.fromkeys(calls)),
        )

    def _extract_top_vars(self, tree: ast.Module) -> list[str]:
        """提取顶层变量。"""
        vars_list = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        vars_list.append(target.id)
        return vars_list

    def _extract_imports(self, tree: ast.Module) -> list[dict[str, Any]]:
        """提取所有导入。"""
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append({
                        "module": alias.name,
                        "name": alias.asname or alias.name,
                        "line": node.lineno,
                        "from": False,
                    })
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    imports.append({
                        "module": module,
                        "name": alias.asname or alias.name,
                        "line": node.lineno,
                        "from": True,
                    })
        return imports

    def _find_unused_imports(self, tree: ast.Module, code: str) -> list[str]:
        """检测未使用的导入。"""
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_names.add(alias.asname or alias.name)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name != "*":
                        imported_names.add(alias.asname or alias.name)

        unused = []
        for name in imported_names:
            # 简单检查：导入名是否在代码中出现（排除导入行本身）
            pattern = r'\b' + re.escape(name) + r'\b'
            matches = re.findall(pattern, code)
            if len(matches) <= 1:  # 只在 import 行出现一次
                unused.append(name)
        return unused

    def _calc_complexity(self, tree: ast.Module) -> int:
        """计算文件总圈复杂度。避免函数内节点被重复计算。"""
        total = 0
        counted_nodes: set[int] = set()

        for node in ast.walk(tree):
            if id(node) in counted_nodes:
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                total += self._node_complexity(node)
                # 标记函数内所有子节点为已计算
                for child in ast.walk(node):
                    counted_nodes.add(id(child))
            elif isinstance(node, (ast.If, ast.While, ast.For, ast.AsyncFor)):
                total += 1
        return total

    def _node_complexity(self, node) -> int:
        """计算单个节点的圈复杂度。"""
        complexity = 1
        for child in ast.walk(node):
            if isinstance(child, (ast.If, ast.While, ast.For, ast.AsyncFor)):
                complexity += 1
            elif isinstance(child, ast.ExceptHandler):
                complexity += 1
            elif isinstance(child, ast.BoolOp):
                complexity += len(child.values) - 1
            elif isinstance(child, ast.Assert):
                complexity += 1
            elif isinstance(child, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                complexity += 1
        return complexity

    def _count_docstring_lines(self, tree: ast.Module) -> int:
        """统计文档字符串行数。"""
        count = 0
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
                ds = ast.get_docstring(node)
                if ds:
                    count += len(ds.splitlines())
        return count
