"""Read-only local file inspection tools."""

from __future__ import annotations

import fnmatch
import logging
import re
from pathlib import Path
from typing import Any

from xenon.engine.context import AgentContext

logger = logging.getLogger(__name__)

MAX_READ_SIZE = 2 * 1024 * 1024


class ReadOnlyFileToolsMixin:
    """Read, list and search workspace files without side effects."""

    def _read_file(self, context: AgentContext) -> dict[str, Any]:
        """读取文件内容。支持通过 start_line/max_lines 分段读取。"""
        file_path = self._resolve_template(self.file_path or "", context)

        if not file_path:
            raise ValueError(f"[{self.id}] read_file 需要 file_path")

        # 安全验证
        path = self._validate_path(file_path, for_write=False)

        if not path.exists():
            result = {
                "action_type": "read_file",
                "file_path": str(path),
                "content": "",
                "exists": False,
                "success": False,
                "error": f"文件不存在: {path}",
            }
            self._write_output(context, "")
            logger.warning(f"[{self.id}] 文件不存在: {path}")
            return result

        # 文件大小检查
        try:
            file_size = path.stat().st_size
            if file_size > MAX_READ_SIZE:
                return {
                    "action_type": "read_file",
                    "file_path": str(path),
                    "content": "",
                    "exists": True,
                    "success": False,
                    "error": f"文件过大: {file_size} 字节，读取上限 {MAX_READ_SIZE} 字节。请使用 command + head/tail 查看部分内容。",
                }
        except OSError:
            pass

        logger.info(f"[{self.id}] 读取文件: {path}")

        # 分段读取：start_line（从 1 开始）和 max_lines
        start_line = getattr(self, '_extra_start_line', None)
        max_lines = getattr(self, '_extra_max_lines', None)

        if start_line is not None or max_lines is not None:
            # 按行分段读取
            all_lines = path.read_text(encoding=self.encoding).splitlines(keepends=True)
            total_lines = len(all_lines)
            s = max(1, int(start_line)) - 1 if start_line else 0  # 转为 0-based
            e = s + int(max_lines) if max_lines else total_lines
            e = min(e, total_lines)
            content = "".join(all_lines[s:e])
            result = {
                "action_type": "read_file",
                "file_path": str(path),
                "content": content,
                "total_lines": total_lines,
                "from_line": s + 1,
                "to_line": e,
                "size": len(content),
                "exists": True,
                "success": True,
            }
        else:
            content = path.read_text(encoding=self.encoding)
            result = {
                "action_type": "read_file",
                "file_path": str(path),
                "content": content,
                "size": len(content),
                "exists": True,
                "success": True,
            }

        self._write_output(context, content)
        return result

    # ── 目录遍历 ──────────────────────────────────────────

    def _list_files(self, context: AgentContext) -> dict[str, Any]:
        """遍历目录，支持 glob 模式和递归深度限制。"""
        base_path = self._resolve_template(self.file_path or ".", context)
        pattern = self._resolve_template(self.pattern, context)

        # 安全验证
        path = self._validate_path(base_path, for_write=False)

        if not path.exists():
            result = {
                "action_type": "list_files", "path": str(path),
                "files": [], "count": 0, "success": False,
                "error": f"路径不存在: {path}",
            }
            self._write_output(context, f"路径不存在: {path}")
            return result

        files: list[str] = []
        if path.is_file():
            files.append(str(path))
        else:
            for item in self._walk_with_depth(path, pattern, self.max_depth):
                files.append(str(item))

        # Keep traversal deterministic so a cursor remains usable across
        # follow-up calls.  ``limit`` is opt-in for backwards compatibility;
        # without it list_files retains its historical full-list behaviour.
        files.sort()
        total = len(files)
        try:
            offset = max(0, int(self.cursor or 0))
        except (TypeError, ValueError):
            offset = 0
        try:
            limit = int(self.limit) if self.limit is not None else None
        except (TypeError, ValueError):
            limit = None
        if limit is not None:
            limit = max(1, min(limit, 1000))
            page = files[offset:offset + limit]
            next_cursor = str(offset + len(page)) if offset + len(page) < total else None
        else:
            page = files
            next_cursor = None

        display = "\n".join(page) if page else "(空目录)"
        result = {
            "action_type": "list_files", "path": str(path),
            "pattern": pattern, "files": page, "count": total,
            "returned_count": len(page), "offset": offset,
            "limit": limit, "next_cursor": next_cursor,
            "truncated": next_cursor is not None,
            "success": True,
        }
        self._write_output(context, display)
        logger.info(f"[{self.id}] 列出 {len(files)} 个文件: {path}")
        return result

    def _walk_with_depth(self, base: Path, pattern: str, max_depth: int):
        """递归遍历，受深度限制。支持 **/*.ext 递归 glob 模式。"""
        import os

        # 处理 **/*.ext 模式：拆分为前缀目录模式和文件名模式
        recursive_mode = "**" in pattern
        if recursive_mode:
            # "**/*.py" → file_pattern = "*.py"
            # "**/test_*.py" → file_pattern = "test_*.py"
            file_pattern = pattern.split("**/")[-1] if "**/" in pattern else pattern.replace("**", "*")
        else:
            file_pattern = pattern

        base_depth = len(base.parts)
        for root, dirs, files in os.walk(base):
            current_depth = len(Path(root).parts) - base_depth
            if not recursive_mode and current_depth > max_depth:
                dirs.clear()
                continue
            if current_depth > max_depth * 2:  # 递归模式给更多深度
                dirs.clear()
                continue
            for f in files:
                if fnmatch.fnmatch(f, file_pattern):
                    yield Path(root) / f

    # ── 文件内容搜索 ──────────────────────────────────────

    def _search_files(self, context: AgentContext) -> dict[str, Any]:
        """在文件中搜索内容（类似 grep）。"""
        search_dir = self._resolve_template(self.file_path or ".", context)
        search_pattern = self._resolve_template(self.search_pattern, context)
        file_filter = self._resolve_template(self.file_filter, context)

        if not search_pattern:
            raise ValueError(f"[{self.id}] search_files 需要 search_pattern")

        # 安全验证
        path = self._validate_path(search_dir, for_write=False)

        if not path.exists():
            result = {
                "action_type": "search_files", "path": str(path),
                "matches": [], "match_count": 0, "success": False,
                "error": f"路径不存在: {path}",
            }
            self._write_output(context, f"路径不存在: {path}")
            return result

        matches = []
        files_scanned = 0
        try:
            regex = re.compile(search_pattern, re.IGNORECASE)
        except re.error:
            regex = re.compile(re.escape(search_pattern), re.IGNORECASE)

        search_files = [path] if path.is_file() else self._walk_with_depth(path, file_filter or "*", self.max_depth)
        try:
            offset = max(0, int(self.cursor or 0))
        except (TypeError, ValueError):
            offset = 0
        try:
            page_limit = int(self.limit) if self.limit is not None else None
        except (TypeError, ValueError):
            page_limit = None
        if page_limit is not None:
            page_limit = max(1, min(page_limit, 1000))
        # With an explicit page size, scan enough matches to report a useful
        # total and to make cursor follow-ups deterministic.  The historical
        # no-limit path keeps its 200-match safety cap.
        scan_cap = 1000 if page_limit is not None else 200
        reached_cap = False

        for file_path in search_files:
            try:
                text = Path(file_path).read_text(encoding=self.encoding, errors="ignore")
                files_scanned += 1
                for i, line in enumerate(text.splitlines(), 1):
                    if regex.search(line):
                        matches.append({
                            "file": str(file_path), "line": i,
                            "content": line.strip()[:200],
                        })
                        if len(matches) >= scan_cap:
                            reached_cap = True
                            break
            except (OSError, UnicodeDecodeError):
                continue
            if reached_cap:
                break

        page = (
            matches[offset:offset + page_limit]
            if page_limit is not None
            else matches
        )
        has_more = page_limit is not None and (
            offset + len(page) < len(matches) or reached_cap
        )
        next_cursor = str(offset + len(page)) if has_more else None
        lines = [f"{m['file']}:{m['line']}: {m['content']}" for m in page[:50]]
        display = "\n".join(lines) if lines else "(无匹配结果)"

        result = {
            "action_type": "search_files", "path": str(path), "pattern": search_pattern,
            "matches": page, "match_count": len(matches),
            "returned_count": len(page), "offset": offset,
            "limit": page_limit, "next_cursor": next_cursor,
            "truncated": next_cursor is not None,
            "files_scanned": files_scanned,
            "stdout": display,  # v0.5.3: 文本表示，LLM 可直接读取
            "success": True,
        }
        self._write_output(context, display)
        logger.info(f"[{self.id}] 搜索到 {len(matches)} 处匹配: {search_pattern}")
        return result

