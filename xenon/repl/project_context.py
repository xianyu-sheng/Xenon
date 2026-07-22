"""
Project Context — 项目上下文感知器。

自动检测项目类型、加载规则文件、构建文件树索引，
为 LLM 提供项目级上下文，使其能像 Claude Code 一样理解项目结构。
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path


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

# 排除的目录（不进入文件树）。
# §8.21.3：原 `_EXCLUDE_DIRS` 是含 `*.egg-info` 的 set，但用 `name in set` 精确匹配，
# glob 模式永远不命中 → `*.egg-info` 目录实际未被排除。拆成 literal（精确名）+ glob
#（fnmatch 模式），glob 走 fnmatch.fnmatchcase。
_EXCLUDE_DIRS_LITERAL = frozenset({
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
    ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
    ".eggs", ".idea", ".vscode", ".claude",
    "target", "bin", "obj", ".next", ".nuxt", "coverage",
})
_EXCLUDE_DIRS_GLOB = ("*.egg-info",)


def _is_excluded_dir(name: str) -> bool:
    """目录是否应被排除：literal 精确匹配 + glob fnmatch。"""
    if name in _EXCLUDE_DIRS_LITERAL:
        return True
    return any(fnmatch.fnmatchcase(name, pat) for pat in _EXCLUDE_DIRS_GLOB)

# 排除的文件模式
_EXCLUDE_FILES = {
    "*.pyc", "*.pyo", "*.class", "*.o", "*.so", "*.dll",
    "*.exe", "*.log", "*.cache", "*.lock", "*.min.js",
}


class ProjectContext:
    """项目上下文感知器。"""

    def __init__(self, *, global_config_root: Path | None = None) -> None:
        self.root: Path | None = None
        self.working_dir: Path | None = None
        self.project_type: str = "unknown"
        self.rules: str = ""
        self.rule_sources: list[str] = []
        self.file_tree: str = ""
        self.key_files: dict[str, str] = {}
        self._initialized: bool = False
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        self._global_config_root = global_config_root or (config_home / "xenon")
        # §8.9.5：关键文件 mtime 缓存——refresh 时 mtime 未变的文件复用已读内容，
        # 避免每次 refresh 重读整批关键文件。
        self._key_file_mtimes: dict[str, float] = {}

    def detect(self, start_dir: Path | None = None) -> bool:
        """
        检测项目根目录和类型。

        从 start_dir（默认 cwd）向上查找项目标记文件。
        Returns: 是否找到项目。

        §8.21.1：向上查找有边界——最多 5 层，且遇 ``$HOME`` 停止。家目录常用
        git 管理 dotfiles（``~/.git`` 存在），无界上爬会把家目录当项目根 →
        扫描整个家目录（隐私泄露 + 性能）。``$HOME`` 下的 ``.git`` 视作
        dotfiles 仓库，不当项目根。
        """
        search = start_dir or Path.cwd()
        if search.is_file():
            search = search.parent
        self.working_dir = search.resolve()
        home = Path(os.path.expanduser("~")).resolve()
        allow_home_project = os.environ.get("XENON_ALLOW_HOME_PROJECT", "").lower() in {
            "1", "true", "yes", "on",
        }
        max_levels = 5

        # Reset all derived data so a refresh/directory change cannot retain a
        # previous project's tree, rules, or key files.
        self.root = None
        self.project_type = "unknown"
        self.rules = ""
        self.rule_sources = []
        self.file_tree = ""
        self.key_files = {}
        self._key_file_mtimes = {}

        # 向上查找包含项目标记的目录
        current = search.resolve()
        levels_up = 0
        while True:
            # HOME is an account boundary, not an implicit project. Check it
            # before markers: package.json/pyproject.toml in HOME often belong
            # to shell tooling or dotfiles and must not authorize a full scan.
            if current == home and not allow_home_project:
                break
            for marker, ptype in _PROJECT_MARKERS.items():
                if (current / marker).exists():
                    self.root = current
                    self.project_type = ptype
                    self._load_all()
                    self._initialized = True
                    return True

            # 有 .git 目录也视为项目根；但 $HOME 下的 .git 是 dotfiles，跳过
            if (
                (current / ".git").is_dir()
                and (current != home or allow_home_project)
            ):
                self.root = current
                self.project_type = self._detect_type_from_content()
                self._load_all()
                self._initialized = True
                return True

            # 限制向上层数
            if levels_up >= max_levels:
                break

            parent = current.parent
            if parent == current:
                break
            current = parent
            levels_up += 1

        # A specific working directory can still be a bounded scratch project
        # without markers. HOME and filesystem root remain explicitly
        # unscoped, preventing broad scans and accidental project-memory roots.
        resolved_search = search.resolve()
        if resolved_search not in {home, Path(resolved_search.anchor)}:
            self.root = resolved_search
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
        """Load layered Xenon instructions and root-bounded ``@path`` imports.

        Precedence is global → project/shared → legacy → project/local.  AGENTS.md
        is a fallback only when XENON.md is absent at the project scope.
        """
        self.rules = ""
        self.rule_sources = []
        global_path = self._global_config_root / "XENON.md"
        candidates: list[tuple[Path, Path]] = [
            (global_path, self._global_config_root),
        ]
        if not self.root:
            project_candidates: list[tuple[Path, Path]] = []
        else:
            project_path = self._primary_instruction(self.root)
            project_candidates = [
                (project_path, self.root),
                (self.root / ".xenon" / "rules.md", self.root),
                (self.root / "XENON.local.md", self.root),
            ]
        candidates.extend(project_candidates)
        # Like Claude-style hierarchical instructions, directory-specific files
        # override broader project rules as the working directory gets deeper.
        if self.root:
            work_dir = self.working_dir or self.root
            try:
                relative = work_dir.resolve().relative_to(self.root.resolve())
                current = self.root
                for component in relative.parts:
                    current = current / component
                    candidates.extend([
                        (self._primary_instruction(current), self.root),
                        (current / "XENON.local.md", self.root),
                    ])
            except (OSError, ValueError):
                pass
        sections: list[str] = []
        for path, allowed_root in candidates:
            content = self._load_instruction_tree(
                path,
                allowed_root=allowed_root,
                visited=set(),
                depth=0,
            ).strip()
            if not content:
                continue
            self.rule_sources.append(str(path))
            sections.append(f"<!-- source: {path} -->\n{content}")
        self.rules = "\n\n".join(sections)[:3000]

    @staticmethod
    def _primary_instruction(directory: Path) -> Path:
        xenon_path = directory / "XENON.md"
        return xenon_path if xenon_path.exists() else directory / "AGENTS.md"

    def _load_instruction_tree(
        self,
        path: Path,
        *,
        allowed_root: Path,
        visited: set[Path],
        depth: int,
    ) -> str:
        """Read an instruction file with cycle, depth, size, and path guards."""
        if depth > 5:
            return ""
        try:
            resolved_root = allowed_root.resolve()
            resolved = path.resolve()
            if not resolved.is_relative_to(resolved_root):
                return ""
            if resolved in visited or not resolved.is_file():
                return ""
            if resolved.stat().st_size > 24_000:
                return ""
        except (OSError, RuntimeError):
            return ""

        content = ""
        for encoding in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
            try:
                content = resolved.read_text(encoding=encoding)
                break
            except (UnicodeDecodeError, UnicodeError):
                continue
            except OSError:
                return ""
        if not content:
            return ""

        visited.add(resolved)
        output: list[str] = []
        for line in content[:12_000].splitlines():
            match = re.match(r"^\s*@(?P<path>[^\s]+)\s*$", line)
            if not match:
                output.append(line)
                continue
            raw_import = match.group("path").strip("<>\"'")
            imported_path = Path(raw_import)
            if not imported_path.is_absolute():
                imported_path = resolved.parent / imported_path
            imported = self._load_instruction_tree(
                imported_path,
                allowed_root=resolved_root,
                visited=visited,
                depth=depth + 1,
            )
            if imported:
                output.extend([
                    f"<!-- imported: {raw_import} -->",
                    imported,
                    f"<!-- end import: {raw_import} -->",
                ])
        visited.remove(resolved)
        return "\n".join(output)[:12_000]

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
                # §8.21.2：不跟随符号链接——避免循环链接递归或扫到链接目标整盘
                if e.is_symlink():
                    continue
                if e.is_dir() and _is_excluded_dir(name):
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
        """加载关键配置文件。

        §8.9.5：基于 mtime 增量——mtime 未变的文件复用已缓存内容，避免每次
        refresh 重读整批关键文件。已删除的文件从缓存中清理。
        """
        if not self.root:
            return

        key_patterns = [
            "pyproject.toml", "package.json", "Cargo.toml", "go.mod",
            "pom.xml", "build.gradle", "README.md", "README.rst",
            ".env.example", "docker-compose.yml", "Dockerfile",
        ]

        new_files: dict[str, str] = {}
        new_mtimes: dict[str, float] = {}
        for name in key_patterns:
            path = self.root / name
            if not path.exists():
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0

            # mtime 未变 → 复用已缓存内容，跳过读盘
            if mtime and name in self.key_files and self._key_file_mtimes.get(name) == mtime:
                new_files[name] = self.key_files[name]
                new_mtimes[name] = mtime
                continue

            try:
                content = path.read_text(encoding="utf-8")
                # 截取前 50 行
                lines = content.splitlines()[:50]
                new_files[name] = "\n".join(lines)
                new_mtimes[name] = mtime
            except Exception:
                pass

        self.key_files = new_files
        self._key_file_mtimes = new_mtimes

    def refresh(self) -> None:
        """刷新项目上下文。"""
        if self.root:
            self._load_all()
        else:
            self.detect(self.working_dir)

    def format_for_context(self) -> str:
        """格式化为注入 LLM 的上下文文本。"""
        if not self._initialized:
            return ""

        if not self.root:
            if self.rules:
                return f"[用户全局指令]\n{self.rules}"
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
        if not self.root:
            sources = ", ".join(self.rule_sources) if self.rule_sources else "无"
            return (
                "当前目录未检测到项目；已进入安全的无项目模式。\n"
                "家目录文件树、关键文件和项目记忆均不会自动加载。\n"
                f"用户全局规则: {sources}"
            )

        lines = [
            f"项目类型: {_TYPE_DISPLAY.get(self.project_type, self.project_type)}",
            f"根目录: {self.root}",
            f"规则文件: {', '.join(self.rule_sources) if self.rule_sources else '无'}",
            f"文件树: {len(self.file_tree.splitlines())} 项",
            f"关键配置: {', '.join(self.key_files.keys()) if self.key_files else '无'}",
        ]
        return "\n".join(lines)
