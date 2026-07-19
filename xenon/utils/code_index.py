"""
Code Index — 轻量级代码索引引擎。

支持：
- Python 文件：基于 ast 模块提取符号（函数、类、方法、变量、导入）
- 其他语言：基于正则表达式提取符号定义
- 项目级符号表构建
- 符号搜索（定义、引用）
- 依赖关系分析
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 支持的文件类型
_CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".swift",
    ".kt", ".scala", ".sh", ".bash", ".ps1", ".lua", ".r", ".m",
}

# 语言对应的符号提取正则
_LANG_PATTERNS: dict[str, list[tuple[str, str]]] = {
    # (pattern, symbol_type)
    "python": [],  # 用 ast 处理
    "javascript": [
        (r"(?:export\s+)?(?:async\s+)?function\s+(\w+)", "function"),
        (r"(?:export\s+)?class\s+(\w+)", "class"),
        (r"(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=", "variable"),
        (r"(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(", "function"),
        (r"(\w+)\s*:\s*(?:async\s+)?function", "method"),
        (r"(\w+)\s*\([^)]*\)\s*\{", "method"),
    ],
    "typescript": [
        (r"(?:export\s+)?(?:async\s+)?function\s+(\w+)", "function"),
        (r"(?:export\s+)?(?:abstract\s+)?class\s+(\w+)", "class"),
        (r"(?:export\s+)?interface\s+(\w+)", "interface"),
        (r"(?:export\s+)?type\s+(\w+)", "type"),
        (r"(?:export\s+)?enum\s+(\w+)", "enum"),
        (r"(?:export\s+)?(?:const|let|var)\s+(\w+)\s*[=:]", "variable"),
        (r"(?:public|private|protected|static)\s+(\w+)\s*\(", "method"),
    ],
    "go": [
        (r"func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)", "function"),
        (r"type\s+(\w+)\s+struct", "class"),
        (r"type\s+(\w+)\s+interface", "interface"),
    ],
    "rust": [
        (r"fn\s+(\w+)", "function"),
        (r"struct\s+(\w+)", "class"),
        (r"enum\s+(\w+)", "enum"),
        (r"trait\s+(\w+)", "interface"),
        (r"impl\s+(?:\w+\s+for\s+)?(\w+)", "class"),
        (r"(?:pub\s+)?const\s+(\w+)", "variable"),
    ],
    "java": [
        (r"(?:public|private|protected)?\s*(?:static\s+)?(?:\w+\s+)+(\w+)\s*\(", "method"),
        (r"(?:public|private|protected)?\s*class\s+(\w+)", "class"),
        (r"(?:public|private|protected)?\s*interface\s+(\w+)", "interface"),
    ],
}


@dataclass
class Symbol:
    """代码符号。"""
    name: str
    kind: str  # function, class, method, variable, interface, enum, type, import
    file_path: str
    line: int
    col: int = 0
    end_line: int | None = None
    parent: str | None = None  # 所属类/模块
    signature: str = ""  # 函数签名
    docstring: str = ""
    decorators: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        loc = f"{self.file_path}:{self.line}"
        parent = f"{self.parent}." if self.parent else ""
        sig = f"({self.signature})" if self.signature else ""
        return f"{self.kind} {parent}{self.name}{sig} @ {loc}"

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 持久化的字典。"""
        return {
            "name": self.name,
            "kind": self.kind,
            "file_path": self.file_path,
            "line": self.line,
            "col": self.col,
            "end_line": self.end_line,
            "parent": self.parent,
            "signature": self.signature,
            "docstring": self.docstring,
            "decorators": list(self.decorators),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Symbol":
        """从字典反序列化。"""
        return cls(**d)


@dataclass
class Reference:
    """符号引用。"""
    name: str
    file_path: str
    line: int
    col: int


class CodeIndex:
    """项目级代码索引。"""

    def __init__(self, root_dir: str | Path = ".", *, cache_dir: str | Path | None = None) -> None:
        self.root = Path(root_dir).resolve()
        # symbol_name -> list[Symbol]
        self.symbols: dict[str, list[Symbol]] = {}
        # file_path -> list[Symbol]
        self.file_symbols: dict[str, list[Symbol]] = {}
        # import info: file_path -> list[dict]
        self.imports: dict[str, list[dict[str, str]]] = {}
        # 所有已索引文件
        self.indexed_files: set[str] = set()
        # 排除目录
        self._exclude_dirs = {
            "node_modules", "__pycache__", ".git", ".venv", "venv",
            "env", ".env", "dist", "build", ".idea", ".vscode",
            "target", "vendor", ".tox", ".mypy_cache", ".pytest_cache",
        }
        # §8.9.1：可选磁盘缓存目录。设置后 build() 会把符号索引持久化到磁盘，
        # 下次 build 对 mtime/size 未变的文件复用缓存、跳过 AST 重解析。
        self._cache_dir: Path | None = Path(cache_dir).resolve() if cache_dir else None

    def build(self, max_files: int = 500) -> int:
        """构建整个项目的索引。返回索引的文件数。

        §8.9.1：若设置了 ``cache_dir``，按文件 mtime+size 增量索引——未变文件
        复用磁盘缓存、跳过 AST 重解析；已删除文件从缓存清理。
        """
        cache = self._load_cache()
        cached_files: dict[str, dict] = cache.get("files", {})
        count = 0
        hit = 0
        walked: set[str] = set()

        for file_path in self._walk_code_files():
            if count >= max_files:
                logger.warning(f"索引文件数达到上限 {max_files}")
                break
            path = Path(file_path).resolve()
            str_path = str(path)
            walked.add(str_path)
            try:
                st = path.stat()
                mtime, size = st.st_mtime, st.st_size
            except OSError:
                continue

            cached = cached_files.get(str_path)
            if cached and cached.get("mtime") == mtime and cached.get("size") == size:
                # 命中缓存：复用符号，不重新 AST 解析
                self._restore_from_cache(str_path, cached)
                hit += 1
            else:
                self.index_file(path)

            # 更新缓存条目（含本次解析结果，供下次复用）
            cached_files[str_path] = {
                "mtime": mtime,
                "size": size,
                "symbols": [s.to_dict() for s in self.file_symbols.get(str_path, [])],
                "imports": list(self.imports.get(str_path, [])),
            }
            count += 1

        # 清理缓存中已删除的文件（本次未走到但缓存里残留）
        for stale in [p for p in cached_files if p not in walked]:
            del cached_files[stale]

        if self._cache_dir is not None:
            self._save_cache({"version": 1, "root": str(self.root), "files": cached_files})

        logger.info(
            f"索引完成: {count} 个文件, {sum(len(v) for v in self.symbols.values())} 个符号"
            f" (缓存命中 {hit})"
        )
        return count

    def index_file(self, file_path: str | Path) -> list[Symbol]:
        """索引单个文件。"""
        path = Path(file_path).resolve()
        if not path.exists() or not path.is_file():
            return []

        ext = path.suffix.lower()
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return []

        str_path = str(path)
        self.indexed_files.add(str_path)

        if ext == ".py":
            symbols = self._index_python(content, str_path)
        else:
            lang = self._ext_to_lang(ext)
            symbols = self._index_regex(content, str_path, lang)

        # 移除旧索引条目（如果重新索引同一文件）
        old_symbols = self.file_symbols.get(str_path, [])
        for old_sym in old_symbols:
            if old_sym.name in self.symbols:
                self.symbols[old_sym.name] = [
                    s for s in self.symbols[old_sym.name]
                    if s.file_path != str_path
                ]
                if not self.symbols[old_sym.name]:
                    del self.symbols[old_sym.name]

        self.file_symbols[str_path] = symbols
        for sym in symbols:
            self.symbols.setdefault(sym.name, []).append(sym)

        return symbols

    def search(self, query: str, *, kind: str | None = None, limit: int = 50) -> list[Symbol]:
        """搜索符号。支持模糊匹配。"""
        query_lower = query.lower()
        results = []
        for name, syms in self.symbols.items():
            if query_lower in name.lower():
                for sym in syms:
                    if kind and sym.kind != kind:
                        continue
                    results.append(sym)
                    if len(results) >= limit:
                        return results
        return results

    def find_definition(self, name: str) -> list[Symbol]:
        """精确查找符号定义。"""
        return self.symbols.get(name, [])

    def find_references(self, name: str, *, limit: int = 100) -> list[Reference]:
        """查找符号的所有引用（使用正则搜索）。"""
        refs = []
        pattern = re.compile(r'\b' + re.escape(name) + r'\b')
        for file_path in self.indexed_files:
            try:
                content = Path(file_path).read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for i, line in enumerate(content.splitlines(), 1):
                for match in pattern.finditer(line):
                    refs.append(Reference(
                        name=name,
                        file_path=file_path,
                        line=i,
                        col=match.start(),
                    ))
                    if len(refs) >= limit:
                        return refs
        return refs

    def get_file_symbols(self, file_path: str) -> list[Symbol]:
        """获取文件的所有符号。"""
        abs_path = str(Path(file_path).resolve())
        return self.file_symbols.get(abs_path, [])

    def get_imports(self, file_path: str) -> list[dict[str, str]]:
        """获取文件的导入信息。"""
        abs_path = str(Path(file_path).resolve())
        return self.imports.get(abs_path, [])

    def stats(self) -> dict[str, Any]:
        """索引统计信息。"""
        kind_counts: dict[str, int] = {}
        for syms in self.symbols.values():
            for sym in syms:
                kind_counts[sym.kind] = kind_counts.get(sym.kind, 0) + 1
        return {
            "files": len(self.indexed_files),
            "symbols": sum(len(v) for v in self.symbols.values()),
            "unique_names": len(self.symbols),
            "by_kind": kind_counts,
        }

    # ── 缓存持久化（§8.9.1）──────────────────────────────────

    def _cache_path(self) -> Path | None:
        """缓存文件路径：``<cache_dir>/codeindex-<root_hash>.json``。"""
        if self._cache_dir is None:
            return None
        key = hashlib.sha1(str(self.root).encode("utf-8")).hexdigest()[:16]
        return self._cache_dir / f"codeindex-{key}.json"

    def _load_cache(self) -> dict[str, Any]:
        """从磁盘加载缓存；不存在或损坏时返回空 dict。"""
        p = self._cache_path()
        if p is None or not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1:
                return {}
            return data
        except (OSError, ValueError, json.JSONDecodeError):
            logger.warning("code_index 缓存读取失败，将全量重建", exc_info=True)
            return {}

    def _save_cache(self, data: dict[str, Any]) -> None:
        """原子写入缓存（写临时文件 + replace）；失败仅告警不影响索引。"""
        p = self._cache_path()
        if p is None:
            return
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(p)
        except OSError:
            logger.warning("code_index 缓存写入失败", exc_info=True)

    def _restore_from_cache(self, str_path: str, cached: dict[str, Any]) -> None:
        """从缓存条目恢复某文件的符号/导入，跳过 AST 解析。"""
        self.indexed_files.add(str_path)
        symbols = [Symbol.from_dict(d) for d in cached.get("symbols", [])]
        self.file_symbols[str_path] = symbols
        for sym in symbols:
            self.symbols.setdefault(sym.name, []).append(sym)
        self.imports[str_path] = list(cached.get("imports", []))

    # ── 内部方法 ──────────────────────────────────────────

    def _walk_code_files(self):
        """遍历项目中的代码文件。"""
        for root, dirs, files in os.walk(self.root):
            # 排除目录
            dirs[:] = [d for d in dirs if d not in self._exclude_dirs]
            for f in files:
                ext = Path(f).suffix.lower()
                if ext in _CODE_EXTENSIONS:
                    yield os.path.join(root, f)

    def _ext_to_lang(self, ext: str) -> str:
        """文件扩展名转语言名。"""
        mapping = {
            ".py": "python",
            ".js": "javascript", ".jsx": "javascript",
            ".ts": "typescript", ".tsx": "typescript",
            ".go": "go",
            ".rs": "rust",
            ".java": "java",
        }
        return mapping.get(ext, "generic")

    def _index_python(self, content: str, file_path: str) -> list[Symbol]:
        """用 ast 索引 Python 文件。"""
        symbols = []
        try:
            tree = ast.parse(content)
        except SyntaxError:
            # 语法错误，降级到正则
            return self._index_regex(content, file_path, "generic")

        # 预计算父类映射，避免 O(F*N) 性能问题
        parent_map = self._build_parent_map(tree)

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
                parent = parent_map.get(id(node))
                decorators = [self._get_decorator_name(d) for d in node.decorator_list]
                sig = self._build_signature(node)
                symbols.append(Symbol(
                    name=node.name,
                    kind="method" if parent else "function",
                    file_path=file_path,
                    line=node.lineno,
                    col=node.col_offset,
                    end_line=getattr(node, 'end_lineno', None),
                    parent=parent,
                    signature=sig,
                    docstring=ast.get_docstring(node) or "",
                    decorators=decorators,
                ))

            elif isinstance(node, ast.ClassDef):
                decorators = [self._get_decorator_name(d) for d in node.decorator_list]
                bases = []
                for base in node.bases:
                    if isinstance(base, ast.Name):
                        bases.append(base.id)
                    elif isinstance(base, ast.Attribute):
                        bases.append(ast.unparse(base))
                symbols.append(Symbol(
                    name=node.name,
                    kind="class",
                    file_path=file_path,
                    line=node.lineno,
                    col=node.col_offset,
                    end_line=getattr(node, 'end_lineno', None),
                    docstring=ast.get_docstring(node) or "",
                    decorators=decorators,
                    signature=f"({', '.join(bases)})" if bases else "",
                ))

            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        symbols.append(Symbol(
                            name=target.id,
                            kind="variable",
                            file_path=file_path,
                            line=node.lineno,
                            col=node.col_offset,
                        ))

            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                self._record_imports(node, file_path)

        return symbols

    def _index_regex(self, content: str, file_path: str, lang: str) -> list[Symbol]:
        """用正则索引非 Python 文件。"""
        symbols = []
        # 未知语言不使用任何模式（避免错误匹配）
        patterns = _LANG_PATTERNS.get(lang, [])

        for i, line in enumerate(content.splitlines(), 1):
            for pattern, kind in patterns:
                for match in re.finditer(pattern, line):
                    name = match.group(1)
                    if name and len(name) >= 1:
                        symbols.append(Symbol(
                            name=name,
                            kind=kind,
                            file_path=file_path,
                            line=i,
                            col=match.start(),
                        ))

        return symbols

    @staticmethod
    def _build_parent_map(tree: ast.Module) -> dict[int, str]:
        """预计算 AST 节点到所属类名的映射。O(N) 单次遍历。"""
        parent_map: dict[int, str] = {}

        def _walk_class(cls_node: ast.ClassDef, class_name: str) -> None:
            for child in ast.iter_child_nodes(cls_node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    parent_map[id(child)] = class_name
                # 不递归进入嵌套类
                if not isinstance(child, ast.ClassDef):
                    _walk_class_inner(child, class_name)

        def _walk_class_inner(node, class_name: str) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    parent_map[id(child)] = class_name
                if not isinstance(child, ast.ClassDef):
                    _walk_class_inner(child, class_name)

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                _walk_class(node, node.name)

        return parent_map

    @staticmethod
    def _build_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        """构建函数签名字符串，包含所有参数类型。"""
        try:
            args = node.args
            parts = []

            # positional-only args (Python 3.8+)
            for arg in getattr(args, 'posonlyargs', []):
                if arg.arg not in ("self", "cls"):
                    parts.append(arg.arg)

            # positional args
            for arg in args.args:
                if arg.arg not in ("self", "cls"):
                    parts.append(arg.arg)

            # *args
            if args.vararg:
                parts.append(f"*{args.vararg.arg}")

            # keyword-only args
            for arg in args.kwonlyargs:
                parts.append(f"{arg.arg}")

            # **kwargs
            if args.kwarg:
                parts.append(f"**{args.kwarg.arg}")

            return ", ".join(parts)
        except Exception:
            return ""

    def _get_decorator_name(self, node) -> str:
        """获取装饰器名称。"""
        try:
            if isinstance(node, ast.Name):
                return node.id
            elif isinstance(node, ast.Attribute):
                return ast.unparse(node)
            elif isinstance(node, ast.Call):
                return ast.unparse(node.func)
        except Exception:
            pass
        return ""

    def _record_imports(self, node, file_path: str) -> None:
        """记录导入信息。"""
        imports = self.imports.setdefault(file_path, [])
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append({
                    "module": alias.name,
                    "name": alias.asname or alias.name,
                    "line": node.lineno,
                })
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                imports.append({
                    "module": module,
                    "name": alias.asname or alias.name,
                    "line": node.lineno,
                })
