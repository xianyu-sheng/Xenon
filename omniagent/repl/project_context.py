"""
Project Context — 项目上下文感知器。

自动检测项目类型、加载规则文件、构建文件树索引，
为 LLM 提供项目级上下文，使其能像 Claude Code 一样理解项目结构。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


# ── 项目类型检测标记 ───────────────────────────────────────

_PROJECT_MARKERS: dict[str, str] = {
    "pyproject.toml": "python",
    "setup.py": "python",
    "setup.cfg": "python",
    "requirements.txt": "python",
    "Pipfile": "python",
    "package.json": "node",
    "tsconfig.json": "node",
    "Cargo.toml": "rust",
    "go.mod": "go",
    "pom.xml": "java",
    "build.gradle": "java",
    "build.gradle.kts": "java",
    "Gemfile": "ruby",
    "composer.json": "php",
    "CMakeLists.txt": "cpp",
    "Makefile": "c",
}

_TYPE_DISPLAY = {
    "python": "Python",
    "node": "Node.js/TypeScript",
    "rust": "Rust",
    "go": "Go",
    "java": "Java",
    "ruby": "Ruby",
    "php": "PHP",
    "cpp": "C/C++",
    "c": "C",
    "unknown": "未知",
}

# 排除的目录（不进入文件树）
_EXCLUDE_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
    ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
    ".eggs", "*.egg-info", ".idea", ".vscode", ".claude",
    "target", "bin", "obj", ".next", ".nuxt", "coverage",
}

# 排除的文件模式
_EXCLUDE_FILES = {
    "*.pyc", "*.pyo", "*.class", "*.o", "*.so", "*.dll",
    "*.exe", "*.log", "*.cache", "*.lock", "*.min.js",
}


class ProjectContext:
    """项目上下文感知器。"""

    def __init__(self) -> None:
        self.root: Path | None = None
        self.project_type: str = "unknown"
        self.rules: str = ""
        self.file_tree: str = ""
        self.key_files: dict[str, str] = {}
        self._initialized: bool = False

    def detect(self, start_dir: Path | None = None) -> bool:
        """
        检测项目根目录和类型。

        从 start_dir（默认 cwd）向上查找项目标记文件。
        Returns: 是否找到项目。
        """
        search = start_dir or Path.cwd()

        # 向上查找包含项目标记的目录
        current = search.resolve()
        while True:
            for marker, ptype in _PROJECT_MARKERS.items():
                if (current / marker).exists():
                    self.root = current
                    self.project_type = ptype
                    self._load_all()
                    self._initialized = True
                    return True

            # 有 .git 目录也视为项目根
            if (current / ".git").is_dir():
                self.root = current
                self.project_type = self._detect_type_from_content()
                self._load_all()
                self._initialized = True
                return True

            parent = current.parent
            if parent == current:
                break
            current = parent

        # 没找到项目标记，用 cwd 作为根
        self.root = search.resolve()
        self.project_type = "unknown"
        self._load_all()
        self._initialized = True
        return False

    def _detect_type_from_content(self) -> str:
        """从目录内容推断项目类型。"""
        if not self.root:
            return "unknown"
        for marker, ptype in _PROJECT_MARKERS.items():
            if (self.root / marker).exists():
                return ptype
        return "unknown"

    def _load_all(self) -> None:
        """加载所有项目上下文。"""
        self._load_rules()
        self._build_file_tree()
        self._load_key_files()

    def _load_rules(self) -> None:
        """加载 .omniagent/rules.md。"""
        if not self.root:
            return
        rules_path = self.root / ".omniagent" / "rules.md"
        if rules_path.exists():
            for enc in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
                try:
                    content = rules_path.read_text(encoding=enc).strip()
                    if content:
                        self.rules = content[:3000]
                        return
                except (UnicodeDecodeError, UnicodeError):
                    continue
                except Exception:
                    return

    def _build_file_tree(self, max_depth: int = 3) -> None:
        """构建精简的文件树。"""
        if not self.root:
            return

        lines: list[str] = []
        root_name = self.root.name or str(self.root)

        def _walk(path: Path, prefix: str, depth: int) -> None:
            if depth > max_depth:
                return
            try:
                entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except PermissionError:
                return

            # 过滤
            filtered = []
            for e in entries:
                name = e.name
                if e.is_dir() and name in _EXCLUDE_DIRS:
                    continue
                if e.is_file():
                    skip = False
                    for pat in _EXCLUDE_FILES:
                        if pat.startswith("*.") and name.endswith(pat[1:]):
                            skip = True
                            break
                    if skip:
                        continue
                filtered.append(e)

            for i, entry in enumerate(filtered):
                is_last = i == len(filtered) - 1
                connector = "└── " if is_last else "├── "
                child_prefix = prefix + ("    " if is_last else "│   ")

                if entry.is_dir():
                    lines.append(f"{prefix}{connector}{entry.name}/")
                    _walk(entry, child_prefix, depth + 1)
                else:
                    size = ""
                    try:
                        s = entry.stat().st_size
                        if s > 1024 * 1024:
                            size = f" ({s // (1024*1024)}MB)"
                        elif s > 1024:
                            size = f" ({s // 1024}KB)"
                    except Exception:
                        pass
                    lines.append(f"{prefix}{connector}{entry.name}{size}")

        lines.append(f"{root_name}/")
        _walk(self.root, "", 1)

        # 限制行数
        if len(lines) > 150:
            lines = lines[:148] + ["... (截断)"]

        self.file_tree = "\n".join(lines)

    def _load_key_files(self) -> None:
        """加载关键配置文件。"""
        if not self.root:
            return

        key_patterns = [
            "pyproject.toml", "package.json", "Cargo.toml", "go.mod",
            "pom.xml", "build.gradle", "README.md", "README.rst",
            ".env.example", "docker-compose.yml", "Dockerfile",
        ]

        for name in key_patterns:
            path = self.root / name
            if path.exists():
                try:
                    content = path.read_text(encoding="utf-8")
                    # 截取前 50 行
                    lines = content.splitlines()[:50]
                    self.key_files[name] = "\n".join(lines)
                except Exception:
                    pass

    def refresh(self) -> None:
        """刷新项目上下文。"""
        if self.root:
            self._load_all()

    def format_for_context(self) -> str:
        """格式化为注入 LLM 的上下文文本。"""
        if not self._initialized:
            return ""

        parts: list[str] = []
        parts.append(f"[项目上下文] 类型: {_TYPE_DISPLAY.get(self.project_type, self.project_type)}")
        parts.append(f"根目录: {self.root}")

        if self.rules:
            parts.append(f"\n[项目规则]\n{self.rules}")

        if self.file_tree:
            parts.append(f"\n[文件结构]\n{self.file_tree}")

        if self.key_files:
            parts.append("\n[关键配置]")
            for name, content in self.key_files.items():
                parts.append(f"--- {name} ---\n{content}")

        return "\n".join(parts)

    def get_summary(self) -> str:
        """返回简短的项目摘要（用于 /project 命令）。"""
        if not self._initialized:
            return "未检测到项目上下文。"

        lines = [
            f"项目类型: {_TYPE_DISPLAY.get(self.project_type, self.project_type)}",
            f"根目录: {self.root}",
            f"规则文件: {'有' if self.rules else '无 (.omniagent/rules.md)'}",
            f"文件树: {len(self.file_tree.splitlines())} 项",
            f"关键配置: {', '.join(self.key_files.keys()) if self.key_files else '无'}",
        ]
        return "\n".join(lines)
