"""Session operations (R1-4: extracted from repl.py).

Tree navigation and archival: bare-continuation resolution, turn-node
finishing, auto-compaction, rewind and artifact listing, session-file
tracking. Mixed into REPL as ``SessionOpsMixin``.
"""

from __future__ import annotations

import logging
from typing import Any

from xenon.repl.turn_helpers import resume_prompt as _resume_prompt_fn

logger = logging.getLogger(__name__)

# 与 repl.py 共用同一 console 实例：由 repl.py 回填。
class _ConsoleProxy:
    """动态转发到 repl.console：测试 monkeypatch repl.console 时同步生效。"""

    def __getattr__(self, name):
        from xenon.repl.repl import console

        return getattr(console, name)

    def __enter__(self):
        from xenon.repl.repl import console

        return console.__enter__()

    def __exit__(self, *exc):
        from xenon.repl.repl import console

        return console.__exit__(*exc)


console: Any = _ConsoleProxy()


class SessionOpsMixin:
    def _track_session_files(self, panel) -> None:
        """从 ThinkingPanel 中提取文件路径，更新 ContextManager 工作记忆。"""
        import os as _os

        created: list[str] = []
        modified: list[str] = []

        for step in panel.steps:
            if not step.action:
                continue
            action = step.action
            ai = step.action_input if isinstance(step.action_input, dict) else {}

            # 提取 file_path / file_paths / path / target_directory
            paths: list[str] = []
            for key in ("file_path", "file_paths", "path", "target_directory"):
                val = ai.get(key)
                if isinstance(val, str):
                    paths.append(val)
                elif isinstance(val, list):
                    paths.extend([str(v) for v in val if isinstance(v, str)])

            # 特殊处理 batch_write: files 是 [{path: ..., content: ...}, ...]
            if action == "batch_write" and "files" in ai:
                files = ai["files"]
                if isinstance(files, list):
                    for f in files:
                        if isinstance(f, dict) and "path" in f:
                            paths.append(str(f["path"]))

            # 失败的工具也要进事实日志（回合归档需要失败日志）；
            # 工作记忆仍只吸收成功结果。
            self._record_event(
                "tool/result",
                tool=str(action),
                success=not step.is_error,
                paths=[str(p) for p in paths[:20]],
                error=(
                    str(getattr(step, "observation", "") or "")[:120]
                    if step.is_error
                    else ""
                ),
            )

            # PostToolUse hook：exit 2 的 stderr 回传给用户/模型。
            if self._hook_runner is not None:
                try:
                    outcome = self._hook_runner.run(
                        "PostToolUse",
                        str(action),
                        dict(ai),
                        extra={
                            "success": not step.is_error,
                            "observation": str(
                                getattr(step, "observation", "") or ""
                            )[:200],
                        },
                    )
                    if outcome is not None and outcome.message:
                        console.print(
                            f"[dim]· hook 反馈: {outcome.message[:200]}[/dim]"
                        )
                except Exception:  # noqa: BLE001
                    pass

            if step.is_error or not paths:
                continue

            for p in paths:
                # 标准化为绝对路径
                abs_path = p if _os.path.isabs(p) else _os.path.abspath(p)

                if action in self._FILE_CREATE_TOOLS:
                    if abs_path not in created:
                        created.append(abs_path)
                elif action in self._FILE_MODIFY_TOOLS:
                    if abs_path not in modified:
                        modified.append(abs_path)

        if created or modified:
            # 合并到工作记忆中（保留历史记录）
            prev = self.ctx_mgr.get_working_memory()
            all_created = list(prev.get("session_created_files", []))
            all_modified = list(prev.get("session_modified_files", []))

            for p in created:
                if p not in all_created:
                    all_created.append(p)
            for p in modified:
                if p not in all_modified:
                    all_modified.append(p)

            self.ctx_mgr.update_working_memory("session_created_files", all_created)
            self.ctx_mgr.update_working_memory("session_modified_files", all_modified)

            # 同时跟踪最近一次操作的关键目录
            dirs = set()
            for p in created:
                d = _os.path.dirname(p)
                if d:
                    dirs.add(d)
            if dirs:
                prev_dirs = list(prev.get("session_active_dirs", []))
                for d in dirs:
                    if d not in prev_dirs:
                        prev_dirs.insert(0, d)  # 最近的在前
                self.ctx_mgr.update_working_memory("session_active_dirs", prev_dirs[:5])


    def _resolve_bare_continuation(self, user_input: str) -> tuple[str | None, str]:
        """裸“继续”的确定性判定：树尾节点可续接 → (None, 续接输入)；否则 (提示, 原输入)。"""

        tail = self._turn_tree.tail()
        if tail is not self._turn_tree.root and tail.is_resumable:
            resumed = self._resume_prompt(tail) + user_input
            self._record_event(
                "turn/resumed", turn_id=tail.turn_id, status=tail.status
            )
            return None, resumed
        return (
            "· 没有可继续的未完成任务。请直接说明你想让我继续做什么。",
            user_input,
        )


    @staticmethod
    def _resume_prompt(node: Any) -> str:
        """续接提示（实现见 xenon.repl.turn_helpers）。"""

        return _resume_prompt_fn(node)


    def _ensure_turn_tree_rebuilt(self) -> None:
        """首次访问时从事件日志重建树（best effort，失败保留新树）。"""

        if getattr(self, "_turn_tree_rebuilt", False):
            return
        self._turn_tree_rebuilt = True
        try:
            log = getattr(self, "_session_events", None)
            if log is not None:
                from xenon.session.tree import TurnTree

                self._turn_tree = TurnTree.rebuild_from_events(log.read())
        except Exception:  # noqa: BLE001 — 重建失败不阻断会话
            logger.debug("TurnTree 重建失败（已忽略）", exc_info=True)


    def _finish_turn_node(
        self,
        status: str,
        *,
        engine: str = "",
        reasons: list[str] | None = None,
        retries_used: int = 0,
        panel: Any = None,
    ) -> None:
        """回合收尾：写树节点状态 + turn/verdict 事实（状态单一来源）。"""

        try:
            node = self._turn_tree.tail()
            fields: dict[str, Any] = {"engine": engine, "retries_used": retries_used}
            if panel is not None:
                fields["steps"] = len(getattr(panel, "steps", []) or [])
                fields["tools"] = int(getattr(panel, "tool_call_count", 0) or 0)
                fields["errors"] = sum(
                    1 for s in (getattr(panel, "steps", []) or []) if s.is_error
                ) + len(getattr(panel, "errors", []) or [])
                # R4: 产物单一来源——树节点 artifacts（回合内成功写入路径）。
                from xenon.repl.turn_helpers import paths_from_panel

                fields["artifacts"] = paths_from_panel(panel)[-10:]
            if reasons:
                fields["verdict_reasons"] = list(reasons)
            # 同因中断快速熔断（402 类基础设施错误）：连续两轮同因 → fused + 指引。
            if status == "interrupted" and reasons:
                parent = node.parent
                if parent is not None and parent is not self._turn_tree.root:
                    from xenon.session.tree import RESUMABLE_STATUSES

                    if (
                        parent.status in RESUMABLE_STATUSES
                        and parent.verdict_reasons[:1] == list(reasons)[:1]
                    ):
                        status = "fused"
                        fields["verdict_reasons"] = list(reasons) + [
                            "连续两轮同因中断（可能是 API 余额/配置问题），请检查后再继续"
                        ]
                        reasons = fields["verdict_reasons"]
            self._turn_tree.finish(node, status, **fields)
            self._record_event(
                "turn/verdict",
                turn_id=node.turn_id,
                status=status,
                reasons=list(reasons or []),
                engine=engine,
                steps=fields.get("steps", 0),
                tools=fields.get("tools", 0),
                errors=fields.get("errors", 0),
            )
        except Exception:  # noqa: BLE001 — 状态写入失败不阻断回合
            logger.debug("回合节点状态写入失败（已忽略）", exc_info=True)


    def _ensure_turn_node_finished(self) -> None:
        """纯对话路径（direct 无引擎）收尾：running 节点 → passed。"""

        try:
            node = self._turn_tree.tail()
            if node is not self._turn_tree.root and node.status == "running":
                self._turn_tree.finish(node, "passed", engine="direct")
        except Exception:  # noqa: BLE001
            pass


    def _auto_compact(self) -> bool:
        """用事件日志做确定性回合归档，替代 LLM 摘要压缩。"""

        log = getattr(self, "_session_events", None)
        if log is None:
            return False
        try:
            from xenon.session.archive import (
                build_turn_archives,
                render_archive_block,
            )

            events = log.read()
            archives = build_turn_archives(events)
            if not archives:
                return False
            block = render_archive_block(archives)
            if not block:
                return False
            self.ctx_mgr.compact(summary=block)
            self._record_event("compaction", turns_archived=len(archives))
            return True
        except Exception:  # noqa: BLE001 — 压缩失败不能阻断回合
            logger.debug("自动压缩失败（已忽略）", exc_info=True)
            return False


    def _rewind_to_turn(self, n: int) -> tuple[bool, list[str]]:
        """截断上下文到第 n 个用户回合；返回其后的文件产物清单。"""

        history = list(getattr(self.ctx_mgr, "history", []))
        user_turns = [t for t in history if getattr(t, "role", "") == "user"]
        if n < 1 or n > len(user_turns):
            return False, []
        target = user_turns[n - 1]
        kept = history[: history.index(target) + 1]
        self.ctx_mgr.clear()
        for turn in kept:
            self.ctx_mgr.add_message(
                str(getattr(turn, "role", "user")),
                str(getattr(turn, "content", "")),
                model_used=getattr(turn, "model_used", None),
                node_id=getattr(turn, "node_id", None),
                metadata=getattr(turn, "metadata", {}) or {},
                task_tier=int(getattr(turn, "task_tier", 3) or 3),
                turn_type=getattr(turn, "turn_type", "general") or "general",
                semantic_group_id=getattr(turn, "semantic_group_id", None),
            )
        self._pending_action = None
        self._boundary_approved_once.clear()
        # 结构层：指针回退到第 n 个回合节点（原枝保留，供 /fork 复用）。
        node = self._turn_tree.ancestor_at(n)
        if node is not None:
            self._turn_tree.move_to(node)
        self._record_event("rewind", turn=n)
        return True, self._artifacts_since_turn(n)


    def _artifacts_since_turn(self, n: int) -> list[str]:
        """事件日志里第 n 个用户回合之后的成功产物（只读提示）。"""

        log = getattr(self, "_session_events", None)
        if log is None:
            return []
        out: list[str] = []
        user_count = 0
        for event in log.read():
            if event.get("type") == "turn/user":
                user_count += 1
                continue
            if (
                event.get("type") == "tool/result"
                and event.get("success")
                and user_count > n
            ):
                for path in event.get("paths") or []:
                    if path and str(path) not in out:
                        out.append(str(path))
        return out

