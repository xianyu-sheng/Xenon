"""Transactional file mutation tools."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from xenon.engine.context import AgentContext
from xenon.utils.atomic_write import atomic_write_bytes, atomic_write_text

logger = logging.getLogger(__name__)

MAX_WRITE_SIZE = 10 * 1024 * 1024
MAX_VERIFY_SIZE = 1 * 1024 * 1024


def _normalize_ws(text: str) -> str:
    """归一化空白：连续空白（含换行）→ 单个空格，用于模糊匹配。"""
    import re

    return re.sub(r"\s+", " ", text).strip()


def _normalized_match(content: str, old_text: str) -> str | None:
    """空白归一化后查找 old_text 的唯一匹配，返回文件中的实际文本。

    LLM 生成的 old_text 常因缩进/换行/行尾空白与文件内容有细微差异，
    精确匹配失败。这里把 old_text 按空白分词，用 ``\\s+`` 连接后做正则
    搜索，等效于「空白不敏感」匹配。若唯一匹配，返回文件中的原始文本
    （保留原格式），用于精确替换。若 0 或 2+ 匹配，返回 None 让调用方
    继续报错。
    """
    import re

    tokens = re.split(r"\s+", old_text.strip())
    if not tokens:
        return None
    # 每个 token 独立转义，然后以 \s+ 连接 —— 空白（含换行/缩进）完全灵活。
    pattern = r"\s+".join(re.escape(t) for t in tokens)
    matches = list(re.finditer(pattern, content))
    if len(matches) != 1:
        return None
    m = matches[0]
    # 向前包含匹配起点前的行内缩进（空格/tab），使替换不破坏代码格式。
    # 不含换行：多行 old_text 的跨行空白已由 \s+ 吞入匹配本身。
    start = m.start()
    while start > 0 and content[start - 1] in " \t":
        start -= 1
    return content[start : m.end()]


def _nearby_context(content: str, old_text: str) -> str:
    """未找到匹配时，返回与 old_text 最相似片段的附近上下文，帮助 LLM 修正。"""
    import difflib

    norm_old = _normalize_ws(old_text)
    if not norm_old:
        return ""
    # 以行为单位找最相似的片段
    lines = content.splitlines()
    best_score = -1.0
    best_idx = 0
    # 滑动窗口：取归一化后的行组与 old_text 比较
    window = max(1, len(norm_old) // 40)  # 粗略估计行数
    for i in range(len(lines) - window + 1):
        chunk = _normalize_ws("\n".join(lines[i : i + window]))
        score = difflib.SequenceMatcher(None, chunk, norm_old).ratio()
        if score > best_score:
            best_score = score
            best_idx = i
    lo = max(0, best_idx - 2)
    hi = min(len(lines), best_idx + window + 2)
    snippet = "\n".join(lines[lo:hi])
    return (
        f"。\n文件中最接近的片段（行 {lo + 1}-{hi}，相似度 "
        f"{best_score:.0%}）：\n```\n{snippet[:400]}\n```\n"
        "请核对实际内容后重新生成 old_text（注意缩进与换行）。"
    )


class FileMutationToolsMixin:
    """Write, edit and batch-mutate files with rollback guarantees."""

    @staticmethod
    def _snapshot_path(path: Path) -> tuple[bytes, int] | None:
        """Capture exact file bytes and permissions for transactional rollback."""
        if not path.exists():
            return None
        if not path.is_file():
            raise IsADirectoryError(f"目标不是普通文件: {path}")
        return path.read_bytes(), path.stat().st_mode & 0o7777

    @staticmethod
    def _rollback_paths(
        paths: list[Path],
        snapshots: dict[Path, tuple[bytes, int] | None],
    ) -> list[str]:
        """Restore written paths in reverse order and report rollback failures."""
        errors: list[str] = []
        for path in reversed(paths):
            try:
                snapshot = snapshots[path]
                if snapshot is None:
                    path.unlink(missing_ok=True)
                else:
                    content, mode = snapshot
                    atomic_write_bytes(path, content, mode=mode)
            except Exception as exc:  # noqa: BLE001 - report every rollback error
                errors.append(f"{path}: {exc}")
        return errors

    def _write_file(self, context: AgentContext) -> dict[str, Any]:
        """将内容写入文件。"""
        file_path = self._resolve_template(self.file_path or "", context)
        content = self._resolve_template(self.content or "", context)
        if not file_path:
            raise ValueError(f"[{self.id}] write_file 需要 file_path")
        if not content and self.output_slot:
            content = context.get(self.output_slot, "")

        path = self._validate_path(file_path, for_write=True)
        content_bytes = len(content.encode(self.encoding))
        if content_bytes > MAX_WRITE_SIZE:
            return {
                "action_type": "write_file",
                "file_path": str(path),
                "bytes_written": 0,
                "success": False,
                "error": f"写入内容过大: {content_bytes} 字节，上限 {MAX_WRITE_SIZE} 字节",
            }
        path.parent.mkdir(parents=True, exist_ok=True)
        logger.info("[%s] %s文件: %s", self.id, "追加" if self.append else "写入", path)

        snapshot = self._snapshot_path(path)
        if self.append and snapshot is not None:
            final_content = snapshot[0].decode(self.encoding) + content
        else:
            final_content = content
        try:
            atomic_write_text(
                path,
                final_content,
                backup=snapshot is not None,
                encoding=self.encoding,
            )
        except Exception as exc:  # noqa: BLE001 - convert to tool result
            return {
                "action_type": "write_file",
                "file_path": str(path),
                "bytes_written": 0,
                "success": False,
                "error": f"原子写入失败: {exc}",
            }

        verify_error = self._verify_write(path, content, self.append)
        if verify_error:
            logger.error("[%s] 写入验证失败: %s", self.id, verify_error)
            rollback_errors = self._rollback_paths([path], {path: snapshot})
            return {
                "action_type": "write_file",
                "file_path": str(path),
                "bytes_written": 0,
                "success": False,
                "error": verify_error,
                "rolled_back": not rollback_errors,
                "rollback_errors": rollback_errors,
            }

        result = {
            "action_type": "write_file",
            "file_path": str(path),
            "bytes_written": len(content.encode(self.encoding)),
            "append": self.append,
            "success": True,
        }
        self._write_output(context, str(path))
        return result

    def _verify_write(
        self,
        path: Path,
        expected_content: str,
        is_append: bool,
    ) -> str | None:
        """验证文件写入是否成功。返回错误信息，成功返回 None。"""
        if not path.exists():
            return f"文件写入后验证失败: {path} 不存在"
        if not path.is_file():
            return f"写入验证失败: {path} 不是文件"
        try:
            file_size = path.stat().st_size
        except OSError:
            return "写入验证失败: 无法获取文件大小"
        if file_size > MAX_VERIFY_SIZE:
            logger.info("文件 %s 大小 %s 字节，跳过内容回读验证", path, file_size)
            return None
        try:
            actual = path.read_text(encoding=self.encoding)
        except UnicodeDecodeError:
            logger.info("文件 %s 为二进制格式，跳过内容验证", path)
            return None
        except Exception as exc:  # noqa: BLE001 - convert to validation text
            return f"写入后读取验证失败: {exc}"

        if is_append:
            if not actual.endswith(expected_content) and expected_content not in actual:
                return "追加验证失败: 写入的内容未在文件中找到"
        elif actual != expected_content:
            return (
                f"内容验证失败: 期望 {len(expected_content)} 字符, "
                f"实际 {len(actual)} 字符"
            )
        return None

    def _edit_file(self, context: AgentContext) -> dict[str, Any]:
        """精确文本替换编辑文件。"""
        file_path = self._resolve_template(self.file_path or "", context)
        old_text = self._resolve_template(self.old_text, context)
        new_text = self._resolve_template(self.new_text, context)
        if not file_path:
            raise ValueError(f"[{self.id}] edit_file 需要 file_path")
        if not old_text:
            raise ValueError(f"[{self.id}] edit_file 需要 old_text")

        path = self._validate_path(file_path, for_write=True)
        if not path.exists():
            return {"error": f"文件不存在: {path}", "success": False}
        content = path.read_text(encoding=self.encoding)
        count = content.count(old_text)
        if count == 0:
            # v0.8.3: 智能匹配回退——LLM 生成的 old_text 常因缩进/换行/空白
            # 与文件实际内容有细微差异导致精确匹配失败。归一化空白后重试。
            normalized_hit = _normalized_match(content, old_text)
            if normalized_hit is None:
                hint = _nearby_context(content, old_text)
                return {
                    "error": "未找到匹配文本" + hint,
                    "success": False,
                }
            # 归一化匹配成功：用实际内容作为替换目标，保留原格式
            old_text = normalized_hit
            count = content.count(old_text)
        if count > 1:
            return {"error": f"找到 {count} 处匹配，请提供更多上下文", "success": False}

        new_content = content.replace(old_text, new_text, 1)
        snapshot = self._snapshot_path(path)
        try:
            atomic_write_text(path, new_content, backup=True, encoding=self.encoding)
        except Exception as exc:  # noqa: BLE001 - convert to tool result
            return {
                "file": str(path),
                "replacements": 0,
                "success": False,
                "error": f"原子编辑失败: {exc}",
            }
        try:
            actual = path.read_text(encoding=self.encoding)
            if actual != new_content:
                rollback_errors = self._rollback_paths([path], {path: snapshot})
                return {
                    "file": str(path),
                    "replacements": 0,
                    "success": False,
                    "error": "编辑验证失败: 文件内容与预期不一致",
                    "rolled_back": not rollback_errors,
                    "rollback_errors": rollback_errors,
                }
        except Exception as exc:  # noqa: BLE001 - rollback and report
            rollback_errors = self._rollback_paths([path], {path: snapshot})
            return {
                "file": str(path),
                "replacements": 0,
                "success": False,
                "error": f"编辑后验证读取失败: {exc}",
                "rolled_back": not rollback_errors,
                "rollback_errors": rollback_errors,
            }

        result = {"file": str(path), "replacements": 1, "success": True}
        self._write_output(context, str(path))
        return result

    def _create_directory(self, context: AgentContext) -> dict[str, Any]:
        """创建目录（含所有父目录）。"""
        dir_path = self._resolve_template(self.file_path or "", context)
        if not dir_path:
            dir_path = self._resolve_template(self.action, context)
        if not dir_path:
            raise ValueError(f"[{self.id}] create_directory 需要 file_path")
        path = self._validate_path(dir_path, for_write=True)
        logger.info("[%s] 创建目录: %s", self.id, path)
        try:
            path.mkdir(parents=True, exist_ok=True)
            if not path.exists() or not path.is_dir():
                return {
                    "action_type": "create_directory",
                    "path": str(path),
                    "success": False,
                    "error": f"目录创建后验证失败: {path} 不存在或不是目录",
                }
            result = {
                "action_type": "create_directory",
                "path": str(path),
                "success": True,
            }
            self._write_output(context, str(path))
            return result
        except Exception as exc:  # noqa: BLE001 - convert to tool result
            return {
                "action_type": "create_directory",
                "path": str(path),
                "success": False,
                "error": f"目录创建失败: {exc}",
            }

    def _batch_write(self, context: AgentContext) -> dict[str, Any]:
        """Atomically write a group of files with all-or-nothing rollback."""
        if not self.files:
            return {
                "action_type": "batch_write",
                "success": False,
                "error": "batch_write 需要 files 参数，格式: [{\"path\": \"...\", \"content\": \"...\"}]",
            }

        prepared: list[tuple[int, Path, str, int]] = []
        results: list[dict[str, Any]] = []
        seen_paths: set[Path] = set()
        for i, file_spec in enumerate(self.files):
            error = ""
            path: Path | None = None
            content = ""
            content_bytes = 0
            if not isinstance(file_spec, dict):
                error = "文件描述必须是对象"
            else:
                path_str = file_spec.get("path") or file_spec.get("file_path", "")
                content = file_spec.get("content", "")
                if not path_str:
                    error = "缺少 path"
                elif not isinstance(content, str):
                    error = "content 必须是字符串"
                else:
                    try:
                        path = self._validate_path(str(path_str), for_write=True)
                        content_bytes = len(content.encode(self.encoding))
                        if path in seen_paths:
                            error = f"同一事务中路径重复: {path}"
                        elif content_bytes > MAX_WRITE_SIZE:
                            error = f"内容过大: {content_bytes} 字节"
                        else:
                            seen_paths.add(path)
                    except Exception as exc:  # noqa: BLE001 - aggregate validation
                        error = str(exc)
            if error or path is None:
                results.append({
                    "index": i,
                    "path": str(path) if path else "",
                    "success": False,
                    "error": error or "无效路径",
                })
            else:
                prepared.append((i, path, content, content_bytes))
                results.append({
                    "index": i,
                    "path": str(path),
                    "success": False,
                    "error": "事务尚未提交",
                })

        if len(prepared) != len(self.files):
            for result in results:
                if result.get("error") == "事务尚未提交":
                    result["error"] = "事务包含无效操作，已整体取消"
            return {
                "action_type": "batch_write",
                "total": len(self.files),
                "success_count": 0,
                "success": False,
                "rolled_back": False,
                "error": "批量写入预检失败，未修改任何文件",
                "results": results,
            }

        snapshots = {path: self._snapshot_path(path) for _, path, _, _ in prepared}
        written_paths: list[Path] = []
        try:
            for i, path, content, content_bytes in prepared:
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(path, content, backup=False, encoding=self.encoding)
                written_paths.append(path)
                verify_error = self._verify_write(path, content, False)
                if verify_error:
                    raise OSError(verify_error)
                results[i] = {
                    "index": i,
                    "path": str(path),
                    "success": True,
                    "bytes": content_bytes,
                }
        except Exception as exc:  # noqa: BLE001 - rollback and report
            rollback_errors = self._rollback_paths(written_paths, snapshots)
            for result in results:
                result["success"] = False
                result["error"] = "事务执行失败，已回滚"
            return {
                "action_type": "batch_write",
                "total": len(self.files),
                "success_count": 0,
                "success": False,
                "rolled_back": not rollback_errors,
                "rollback_errors": rollback_errors,
                "error": f"批量写入失败: {exc}",
                "results": results,
            }
        return {
            "action_type": "batch_write",
            "total": len(self.files),
            "success_count": len(prepared),
            "success": True,
            "results": results,
        }

    def _batch_edit(self, context: AgentContext) -> dict[str, Any]:
        """Apply a group of exact edits as one transactional operation."""
        if not self.edits:
            return {
                "action_type": "batch_edit",
                "success": False,
                "error": "batch_edit 需要 edits 参数，格式: [{\"file_path\": \"...\", \"old_text\": \"...\", \"new_text\": \"...\"}]",
            }

        results: list[dict[str, Any]] = []
        staged_content: dict[Path, str] = {}
        path_order: list[Path] = []
        for i, edit_spec in enumerate(self.edits):
            error = ""
            path: Path | None = None
            if not isinstance(edit_spec, dict):
                error = "编辑描述必须是对象"
                old_text = ""
                new_text = ""
                file_path = ""
            else:
                file_path = edit_spec.get("file_path", "")
                old_text = edit_spec.get("old_text", "")
                new_text = edit_spec.get("new_text", "")
                if not file_path or not old_text:
                    error = "缺少 file_path 或 old_text"
                elif not isinstance(old_text, str) or not isinstance(new_text, str):
                    error = "old_text 和 new_text 必须是字符串"

            if not error:
                try:
                    path = self._validate_path(str(file_path), for_write=True)
                    if not path.exists():
                        error = f"文件不存在: {path}"
                    else:
                        if path not in staged_content:
                            staged_content[path] = path.read_text(encoding=self.encoding)
                            path_order.append(path)
                        count = staged_content[path].count(old_text)
                        if count == 0:
                            error = "未找到匹配文本"
                        elif count > 1:
                            error = f"找到 {count} 处匹配，请提供更多上下文"
                        else:
                            staged_content[path] = staged_content[path].replace(
                                old_text, new_text, 1
                            )
                except Exception as exc:  # noqa: BLE001 - aggregate validation
                    error = f"编辑预检异常: {exc}"
            results.append({
                "index": i,
                "file": str(path) if path else str(file_path),
                "success": False,
                "error": error or "事务尚未提交",
            })

        if any(result["error"] != "事务尚未提交" for result in results):
            for result in results:
                if result["error"] == "事务尚未提交":
                    result["error"] = "事务包含无效操作，已整体取消"
            return {
                "action_type": "batch_edit",
                "total": len(self.edits),
                "success_count": 0,
                "success": False,
                "rolled_back": False,
                "error": "批量编辑预检失败，未修改任何文件",
                "results": results,
            }

        snapshots = {path: self._snapshot_path(path) for path in path_order}
        written_paths: list[Path] = []
        try:
            for path in path_order:
                atomic_write_text(
                    path,
                    staged_content[path],
                    backup=False,
                    encoding=self.encoding,
                )
                written_paths.append(path)
                verify_error = self._verify_write(path, staged_content[path], False)
                if verify_error:
                    raise OSError(verify_error)
        except Exception as exc:  # noqa: BLE001 - rollback and report
            rollback_errors = self._rollback_paths(written_paths, snapshots)
            for result in results:
                result["error"] = "事务执行失败，已回滚"
            return {
                "action_type": "batch_edit",
                "total": len(self.edits),
                "success_count": 0,
                "success": False,
                "rolled_back": not rollback_errors,
                "rollback_errors": rollback_errors,
                "error": f"批量编辑失败: {exc}",
                "results": results,
            }

        for result in results:
            result["success"] = True
            result.pop("error", None)
            result["replacements"] = 1
        return {
            "action_type": "batch_edit",
            "total": len(self.edits),
            "success_count": len(self.edits),
            "success": True,
            "results": results,
        }

