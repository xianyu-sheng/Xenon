"""DirectoryScout — 项目结构扫描（P2-E1 / §Q4 第一道防线）。

防路径幻觉：在 ReAct/Plan-Execute 启动前静默扫描项目根，把结构化文件树注入
用户输入，让 LLM 基于真实文件而非凭空猜测路径。无数据时改提示词强制首步
``list_files``；有历史 ``Observation(list_files ...)`` 则复用。

安全属性（纠正 §8.21 ``project_context`` 的同类问题，此处作为正确实现示范）：
- 限制扫描深度（``max_depth``）+ 每层条目上限（``max_entries_per_dir``）；
- **不跟随符号链接**（``is_symlink`` → skip，防循环/扫全盘）；
- 排除常见大目录（``node_modules``/``.git``/``venv``/``__pycache__``/…），
  且排除用 ``fnmatch`` 匹配（修复 §8.21.3 ``*.egg-info`` 在 set 里失效）；
- 根解析不向上搜（避免 §8.21.1 扫整个家目录）。
"""

from __future__ import annotations

import fnmatch
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


# 常见大/无关目录，扫描时排除（含 glob 模式，fnmatch 匹配）。
DEFAULT_EXCLUDE: frozenset[str] = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist",
    "build", ".eggs", "*.egg-info", "vendor", "third_party", ".next",
    ".nuxt", "target", ".gradle", ".idea", ".vscode", "bower_components",
    ".pnpm-store", "logs", "tmp", "temp", ".cache", "site-packages",
})

# 文件路径启发式（用于 scout_from_history 提取）。
_RE_PATH = re.compile(r"[\w\-./\\]+\.\w{1,6}")


class DirectoryScout:
    """项目结构扫描器。

    用法::

        scout = DirectoryScout(project_root=".")
        enriched = scout.inject(user_input, messages=history)
        engine.run(enriched, ctx)
    """

    def __init__(
        self,
        project_root: str | Path | None = None,
        *,
        max_depth: int = 2,
        max_entries_per_dir: int = 50,
        exclude_dirs: frozenset[str] | set[str] | None = None,
    ) -> None:
        self.project_root = Path(project_root) if project_root else Path.cwd()
        self.max_depth = max_depth
        self.max_entries_per_dir = max_entries_per_dir
        self.exclude_dirs = frozenset(exclude_dirs) if exclude_dirs is not None else DEFAULT_EXCLUDE

    def _excluded(self, name: str) -> bool:
        """fnmatch 匹配排除目录（支持 ``*.egg-info`` 等 glob 模式）。"""
        return any(fnmatch.fnmatch(name, pat) for pat in self.exclude_dirs)

    def scan(self) -> dict[str, object] | None:
        """扫项目根，返回 ``{root, tree, file_count}`` 或 ``None``（无数据）。

        不跟随符号链接；超出深度/条目上限时标注截断。
        """
        root = self.project_root
        if not root.exists() or not root.is_dir() or root.is_symlink():
            return None
        tree_lines: list[str] = []
        file_count = self._walk(root, 0, tree_lines)
        if file_count == 0 and not tree_lines:
            return None
        return {"root": str(root), "tree": "\n".join(tree_lines), "file_count": file_count}

    def _walk(self, dir_path: Path, depth: int, out: list[str]) -> int:
        """递归遍历，返回文件数。目录优先排序，符号链接跳过。"""
        if depth > self.max_depth:
            return 0
        count = 0
        try:
            entries = sorted(
                dir_path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())
            )
        except (PermissionError, OSError) as e:
            logger.debug(f"DirectoryScout: 跳过无权限目录 {dir_path}: {e}")
            return 0

        shown = 0
        for entry in entries:
            if entry.is_symlink():
                continue  # §8.21.2：不跟随符号链接
            name = entry.name
            if self._excluded(name):
                continue
            if shown >= self.max_entries_per_dir:
                out.append(f"{'  ' * depth}... (more in {dir_path.name}/)")
                break
            is_dir = entry.is_dir()
            prefix = "📁 " if is_dir else "📄 "
            out.append(f"{'  ' * depth}{prefix}{name}")
            shown += 1
            if is_dir:
                count += self._walk(entry, depth + 1, out)
            else:
                count += 1
        return count

    def scout_from_history(self, messages: list[dict[str, str]] | None) -> str | None:
        """从历史 ``Observation(list_files ...)`` 提取已扫过的文件列表。"""
        for m in reversed(messages or []):
            content = m.get("content", "") if isinstance(m, dict) else ""
            if not isinstance(content, str) or not content.startswith("Observation:"):
                continue
            paths = [
                ln.strip()
                for ln in content.splitlines()
                if ln.strip() and not ln.startswith("Observation")
                and (_RE_PATH.search(ln) or "/" in ln or "\\" in ln)
            ]
            if paths:
                return "\n".join(paths[:80])
        return None

    def inject(self, user_input: str, messages: list[dict[str, str]] | None = None) -> str:
        """增强 ``user_input``：

        - 有扫描数据 → 前缀注入真实文件树；
        - 无扫描但有历史 list_files → 复用历史文件列表；
        - 都没有 → 追加"首步先 list_files"提示，强制基于真实文件规划。
        """
        scan = self.scan()
        if scan and scan["file_count"]:
            return (
                f"[项目结构预览（来自 DirectoryScout，真实文件，请据此规划路径）]\n"
                f"{scan['tree']}\n"
                f"[/项目结构预览]\n\n"
                f"{user_input}"
            )
        hist = self.scout_from_history(messages) if messages else None
        if hist:
            return (
                f"[历史已扫描的文件（来自此前 list_files）]\n{hist}\n"
                f"[/历史扫描]\n\n{user_input}"
            )
        return (
            f"{user_input}\n\n"
            "（提示：项目结构未知，请第一步先调用 list_files 查看真实文件再规划，"
            "勿凭空猜测路径。）"
        )
