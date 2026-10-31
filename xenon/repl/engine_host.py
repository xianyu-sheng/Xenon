"""Harness host for the REPL: log capture, callbacks, kernel escalation.

This module used to host the seven engines (``_run_engine`` and its per-engine
shims, trace persistence, MCP injection, plan display, steering).  All of that
became dead code when the turn path moved to the kernel loop (#202), so what is
left is the part the REPL still uses:

* log capture around a turn,
* the ``ConsoleCallback`` factory,
* the "history must stay paired" error recorder,
* ``_run_react_engine`` — direct-chat escalation, which now runs the kernel.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from xenon.repl.execution_policy import rebind_execution_boundary

logger = logging.getLogger(__name__)


# 与 repl.py 共用同一 console 实例：由 repl.py 在 Console 创建后回填。
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


class EngineHostMixin:
    def _start_log_capture(self) -> None:
        """v0.5.3: 拦截引擎执行期间的日志输出。

        保存并移除目标 logger 的所有现有 handler，替换为写入内存缓冲区的
        StringIO handler。这样日志不会输出到 stderr，而是被收集供折叠/展开使用。
        """
        import io as _io

        if getattr(self, "_log_capture_active", False):
            # Defensive cleanup if a previous run was interrupted outside the
            # normal Exception path.
            self._captured_log = self._stop_log_capture()
        self._log_buffer = _io.StringIO()
        self._log_handler = logging.StreamHandler(self._log_buffer)
        self._log_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        self._log_handler.setLevel(logging.INFO)
        # 保存原有 handler 并清空，确保日志只进缓冲区
        # 关键：同时捕获 root logger（日志传播终点）+ 所有已注册子 logger
        self._saved_handlers: dict[str, list[logging.Handler]] = {}
        self._saved_propagate: dict[str, bool] = {}
        _capture_names: list[str] = []
        # 遍历所有已注册的 logger，找到 xenon.* / httpx / openai / httpcore 及 root
        for _lg_name, _lg_obj in logging.root.manager.loggerDict.items():
            if isinstance(_lg_obj, logging.Logger) and (
                _lg_name.startswith("xenon")
                or _lg_name in ("httpx", "openai", "httpcore")
            ):
                _capture_names.append(_lg_name)
        for _name in _capture_names:
            _lg = logging.getLogger(_name)
            self._saved_handlers[_name] = list(_lg.handlers)
            self._saved_propagate[_name] = _lg.propagate
            _lg.handlers.clear()
            # 子 logger 统一传播到 root，避免同一个 handler 在子级和 root
            # 各执行一次造成重复日志。
            _lg.propagate = True

        root_logger = logging.getLogger()
        self._saved_handlers[""] = list(root_logger.handlers)
        root_logger.handlers.clear()
        root_logger.addHandler(self._log_handler)
        self._log_capture_active = True

    def _stop_log_capture(self) -> str:
        """v0.5.3: 恢复原有 handler，返回捕获的日志文本。"""
        if not getattr(self, "_log_capture_active", False):
            return ""
        _captured = ""
        try:
            for _name in self._saved_handlers:
                _lg = logging.getLogger(_name)
                try:
                    _lg.removeHandler(self._log_handler)
                except Exception as exc:
                    logger.debug("移除临时日志 handler 失败 (%s): %s", _name, exc)
                for _h in self._saved_handlers.get(_name, []):
                    try:
                        _lg.addHandler(_h)
                    except Exception as exc:
                        # 恢复失败意味着该 logger 之后永久失去输出，必须留痕。
                        logger.warning(
                            "恢复原日志 handler 失败 (%s)，该 logger 的输出可能丢失: %s",
                            _name,
                            exc,
                        )
                if _name in self._saved_propagate:
                    _lg.propagate = self._saved_propagate[_name]
        finally:
            self._log_capture_active = False
            try:
                self._log_handler.close()
            except Exception as exc:
                logger.debug("关闭临时日志 handler 失败: %s", exc)
            try:
                _captured = self._log_buffer.getvalue()
            except Exception as exc:
                logger.debug("读取捕获日志缓冲失败（本次执行详情将为空）: %s", exc)
        return _captured

    def _make_callback(self):
        """根据 verbose 状态创建回调。"""
        from xenon.engine.callbacks import ConsoleCallback

        callback = ConsoleCallback(verbose=self.verbose)
        self._active_callback = callback
        return callback

    def _record_engine_error(self, message: str, model_used: str | None) -> None:
        """记录一轮错误到对话历史，并保证历史不残留 user-only 序列。

        引擎抛异常时 user 消息已入 history 但没有对应的 assistant 响应。这种
        user-only 序列会污染下一轮的上下文与缓存前缀，所以必须二选一收尾：

        1. 优先写入 assistant 占位错误消息（保留失败信息，用户可见）；
        2. 写入失败则回退 ``trim_last_user()`` 清掉孤立 user 消息。

        两步都失败时记日志——此前这里是静默 ``pass``，历史已损坏却无任何线索，
        表现为「下一轮忽然行为异常」且无法排查。
        """
        try:
            self.ctx_mgr.add_assistant_message(message, model_used=model_used)
            return
        except Exception as exc:
            logger.warning(
                "写入 assistant 错误占位失败，回退清理孤立 user 消息: %s", exc
            )
        try:
            self.ctx_mgr.trim_last_user()
        except Exception as exc:
            logger.error(
                "清理孤立 user 消息也失败，对话历史可能残留 user-only 序列"
                "（建议 /clear 或 /resume 重建上下文）: %s",
                exc,
            )

    def _run_react_engine(self, user_input: str, model_ids: list[str]) -> None:
        """Escalation entry: run the tool turn on the kernel loop.

        Direct chat can escalate mid-turn (the model asked for a tool, or
        claimed work it did not do).  That escalation is just a tool turn, so it
        runs on the same loop as every other tool turn instead of waking a
        legacy engine.
        """

        from xenon.repl.execution_policy import ExecutionLevel
        from xenon.repl.runtime_adapter import run_kernel_turn
        from xenon.repl.turn_contract import build_turn_contract

        # Escalation raises the level in the agent context; rebuild the turn
        # contract from that level so the kernel sees the escalated boundary
        # instead of the original answer-only one.
        contract = build_turn_contract(user_input)
        level = self.agent_context.get("_execution_level")
        if level is not None:
            contract = replace(
                contract,
                level=ExecutionLevel(int(level)),
                proposed_level=ExecutionLevel(int(level)),
            )
        try:
            run_kernel_turn(
                self,
                objective=rebind_execution_boundary(
                    user_input, int(contract.level)
                ),
                user_input=user_input,
                model_ids=model_ids,
                execution_policy=contract.to_execution_policy(),
            )
        except Exception as exc:  # noqa: BLE001 — history must stay paired
            # The user message is already recorded.  Leave a placeholder so the
            # turn history is not left with an orphan user message; if even that
            # fails, drop the user message instead.
            try:
                self.ctx_mgr.add_assistant_message(
                    f"[错误] {exc}",
                    model_used=model_ids[0] if model_ids else None,
                )
            except Exception:  # noqa: BLE001
                self.ctx_mgr.trim_last_user()
