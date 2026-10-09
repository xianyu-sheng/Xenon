"""Engine host (R1-2: extracted from repl.py).

The REPL's engine lifecycle: building engines, running them through the
single ``_run_engine`` flow (publish-gate retries included), direct mode,
steering, log capture, trace persistence and MCP injection. Mixed into
REPL as ``EngineHostMixin`` — methods keep ``self.*`` access and call
sites stay unchanged.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from rich.text import Text

from xenon.engine.registry import ENGINE_REGISTRY
from xenon.engine.registry import EngineSpec
from xenon.repl.turn_helpers import (
    retry_budget_for as _retry_budget_for,
    verify_retries_limit as _verify_retries_limit,
)

logger = logging.getLogger(__name__)

# 与 repl.py 共用同一 console 实例：由 repl.py 在 Console 创建后回填。
console: Any = None


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
            if isinstance(_lg_obj, logging.Logger):
                if _lg_name.startswith("xenon") or _lg_name in (
                    "httpx",
                    "openai",
                    "httpcore",
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
                # 恢复原有 handler
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
        """根据 verbose 状态创建引擎回调。"""
        from xenon.engine.callbacks import ConsoleCallback

        callback = ConsoleCallback(verbose=self.verbose)
        self._active_callback = callback
        return callback


    def _persist_engine_trace(self, engine: object) -> int:
        """Move verified engine tool calls into cross-turn context and memory."""
        if getattr(engine, "_xenon_trace_persisted", False):
            return 0
        setattr(engine, "_xenon_trace_persisted", True)
        tracker = getattr(engine, "_last_tracker", None)
        calls = list(getattr(tracker, "calls", []) or [])
        if not calls:
            return 0

        provider_messages = list(getattr(engine, "_last_provider_messages", []) or [])
        provider_tool_results = sum(
            1
            for message in provider_messages
            if isinstance(message, dict) and message.get("role") == "tool"
        )
        if provider_messages:
            self.ctx_mgr.add_provider_messages(provider_messages)
        protocol_covers_trace = provider_tool_results >= len(calls)

        import os as _os

        created: list[str] = []
        modified: list[str] = []
        recent_activity: list[dict[str, object]] = []
        for call in calls:
            tool_name = str(getattr(call, "tool_name", "unknown"))
            params = getattr(call, "params", {})
            if not isinstance(params, dict):
                params = {}
            success = bool(getattr(call, "success", False))
            result = str(getattr(call, "result_summary", "") or "")
            error = getattr(call, "error", None)
            if not protocol_covers_trace:
                self.ctx_mgr.add_tool_trace(
                    tool_name,
                    params,
                    success,
                    result=result,
                    error=str(error) if error else None,
                )
            recent_activity.append(
                {
                    "tool": tool_name,
                    "success": success,
                    "summary": (result or str(error or ""))[:300],
                }
            )

            if not success:
                continue
            paths: list[str] = []
            for key in ("file_path", "file_paths", "path", "target_directory"):
                value = params.get(key)
                if isinstance(value, str):
                    paths.append(value)
                elif isinstance(value, list):
                    paths.extend(str(v) for v in value if isinstance(v, str))
            if tool_name == "batch_write":
                for item in params.get("files", []):
                    if isinstance(item, dict) and isinstance(item.get("path"), str):
                        paths.append(item["path"])
            for path in paths:
                absolute = path if _os.path.isabs(path) else _os.path.abspath(path)
                target = created if tool_name in self._FILE_CREATE_TOOLS else modified
                if tool_name in self._FILE_CREATE_TOOLS | self._FILE_MODIFY_TOOLS:
                    if absolute not in target:
                        target.append(absolute)

        memory = self.ctx_mgr.get_working_memory()
        if created:
            known = list(memory.get("session_created_files", []))
            self.ctx_mgr.update_working_memory(
                "session_created_files",
                (known + [p for p in created if p not in known])[-100:],
            )
        if modified:
            known = list(memory.get("session_modified_files", []))
            self.ctx_mgr.update_working_memory(
                "session_modified_files",
                (known + [p for p in modified if p not in known])[-100:],
            )
        active_dirs = [
            _os.path.dirname(path)
            for path in created + modified
            if _os.path.dirname(path)
        ]
        if active_dirs:
            known_dirs = list(memory.get("session_active_dirs", []))
            merged_dirs = active_dirs + [d for d in known_dirs if d not in active_dirs]
            self.ctx_mgr.update_working_memory("session_active_dirs", merged_dirs[:10])

        previous_activity = list(memory.get("recent_tool_activity", []))
        self.ctx_mgr.update_working_memory(
            "recent_tool_activity",
            (previous_activity + recent_activity)[-12:],
        )
        return len(calls)


    @staticmethod
    def _engine_model_used(engine: object, model_ids: list[str]) -> str | None:
        """Return the model that actually answered, falling back safely.

        Engine calls can fail over from ``model_ids[0]`` to a later provider.
        ``BaseEngine.last_model_used`` records that successful provider so the
        transcript and fixed bottom toolbar do not advertise the wrong model.
        """
        actual = getattr(engine, "last_model_used", None)
        if isinstance(actual, str) and actual:
            return actual
        return model_ids[0] if model_ids else None


    def _inject_mcp_tools_into_engine(self, engine: object) -> None:
        """v0.5.4: 将可用 MCP 工具（或惰性描述）注入引擎的 system prompt。

        所有引擎（ReAct/PlanExecute/Reflection 等）统一使用此方法，
        让 LLM 知道有哪些 MCP 服务器可用。

        惰性模式下：只显示服务器名列表，不调用 discover_tools()。
        当 LLM 实际决定调用 mcp_call 时，_ensure_mcp_ready() 才触发连接。
        """
        from xenon.engine.execution_policy import walk_engine_graph

        nodes = list(walk_engine_graph(engine))
        registry = getattr(self, "_mcp_registry", None)
        has_mcp = bool(
            registry and (registry.clients or registry.has_pending_servers())
        )
        if not has_mcp:
            for node in nodes:
                if not hasattr(node, "tools"):
                    continue
                # A generic MCP entry with no configured server encourages the
                # model to fabricate names such as ``train_query``.  Hide the
                # impossible action from every executable child in a combined
                # engine, not just from its tool-less wrapper.
                node.tools = dict(node.tools)
                node.tools.pop("mcp_call", None)
        try:
            mcp_tools_list = self._build_mcp_tools_list()
        except Exception as e:
            logger.debug(f"构建 MCP 工具列表失败: {e}")
            return
        for node in nodes:
            if not hasattr(node, "_mcp_tools_list"):
                continue
            try:
                node._mcp_tools_list = mcp_tools_list
                if hasattr(node, "_build_system_prompt"):
                    node.system_prompt = node._build_system_prompt()
            except Exception as e:
                logger.warning(f"注入 MCP 工具列表到引擎失败: {e}")


    def _record_engine_error(self, message: str, model_used: str | None) -> None:
        """记录引擎错误到对话历史，并保证历史不残留 user-only 序列。

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


    def _bind_interactive_tool_runtime(self, engine: Any) -> None:
        """Give the interactive path the same workspace binding as evals.

        ``bind_tool_runtime`` was previously called only for spawned
        sub-agents (``react_engine``) and SWE-bench (``evals/``), leaving
        interactive runs with ``ToolExecutor.runtime is None``.  In that state
        ``_runtime_params`` returns model params untouched, so the ``cwd`` the
        model supplied survives — and ``cwd`` is in ``ToolNode._VALID_PARAMS``.
        Since the fence root is derived from ``cwd``
        (``ToolNode._get_allowed_root``), a model-chosen ``cwd`` moved the
        fence itself rather than being contained by it.

        Binding one runtime makes ``_runtime_params`` overwrite ``cwd`` with
        the trusted workspace root on every tool call, so interactive and
        evaluation runs enforce the same boundary.
        """
        from xenon.engine.tool_runtime import ToolRuntime, bind_tool_runtime

        try:
            root = self.project_ctx.root or self.project_ctx.working_dir or Path.cwd()
            bind_tool_runtime(engine, ToolRuntime(workspace_root=Path(root)))
        except Exception as exc:  # noqa: BLE001 — never block a task on binding
            # A missing/renamed directory must not make the task unrunnable;
            # ToolNode's own fence still applies via its cwd fallback.
            logger.warning(
                "工具运行时绑定失败，本次任务回退到 ToolNode 自身围栏: %s", exc
            )


    def _run_engine(
        self, spec: EngineSpec, user_input: str, model_ids: list[str]
    ) -> None:
        """按 ``EngineSpec`` 运行一种推理范式 —— 全部引擎共用的唯一流程。

        此前六个 ``_run_*_engine`` 方法（约 275 行）逐行同构，只差引擎类、
        一两个调参 kwargs、模式行文案和结果标题。差异现在由 ``EngineSpec``
        承载（见 ``xenon/engine/builtin_engines.py``），公共流程只此一份：

            日志捕获 → 构造引擎 → 注入 MCP 工具 → run() → 落 trace
            → 渲染结果 → 异常收尾 → finally 收尾

        收敛的直接好处：``_record_engine_error`` 之类的收尾调用只有一个调用点，
        不会再出现「五个副本里有五种写法」的漂移。
        """
        self._last_mode_line = spec.mode_line

        self._start_log_capture()
        callback = self._make_callback()
        engine = spec.factory(
            model_priority=model_ids,
            model_pool=self.model_pool,
            auto_router=self.auto_router,
            callback=callback,
            model_configs=dict(self.registry.models),
            permission_gate=self._permission_gate,
        )
        self._inject_mcp_tools_into_engine(engine)
        # 工作区绑定：与评测路径一致（见 _bind_interactive_tool_runtime）。
        # 必须在 run() 之前——绑定后模型提供的 cwd 才会被可信根覆盖。
        self._bind_interactive_tool_runtime(engine)
        # 在线验证链：任务摄入——REPL 先创建任务证据，引擎复用同一条 ledger。
        try:
            self.agent_context.evidence.start_task(
                engine=spec.name,
                user_input=user_input[:500],
            )
        except Exception as exc:  # noqa: BLE001 — 证据记录失败不阻断主流程
            logger.debug("任务证据记录失败（不影响执行）: %s", exc)
        # Mid-task steering：任务运行期间启动输入监听线程，用户补充/修改
        # 要求通过 engine.steer() 注入，引擎在下一个迭代检查点消费并让
        # LLM 自行判断如何调整后续步骤（不打断当前工具调用）。
        steering_thread = self._start_steering_listener(engine)
        try:
            result = engine.run(
                user_input, context=self.agent_context, ctx_mgr=self.ctx_mgr
            )
            if spec.log_result_diagnostics:
                # 诊断日志 — 记录结果实际值，用于排查空白面板根因。
                logger.info(
                    f"_run_engine[{spec.name}]: result type={type(result).__name__}, "
                    f"len={len(result) if isinstance(result, str) else 'N/A'}, "
                    f"strip_len={len(result.strip()) if isinstance(result, str) and result else 0}, "
                    f"head={result[:80] if isinstance(result, str) and result else repr(result)[:80]}"
                )
            self._captured_log = self._stop_log_capture()
            self._persist_engine_trace(engine)
            model_used = self._engine_model_used(engine, model_ids)
            self.ctx_mgr.add_assistant_message(result, model_used=model_used)

            # ── 发布门 v3：校验未通过 → 同回合反馈重试（闭环上限 XENON_VERIFY_RETRIES）──
            panel = callback.get_thinking_panel()
            first_round_steps = len(getattr(panel, "steps", []) or []) if panel else 0
            retries_left = _verify_retries_limit()
            retries_spent = 0
            verification_ok, verification_reasons = True, []
            while panel is not None and retries_left > 0:
                verification_ok, verification_reasons = self._verify_turn(
                    panel, result
                )
                if verification_ok:
                    break
                retries_left -= 1
                retries_spent += 1
                fused = (
                    getattr(self, "_last_gate_verdict", None) is not None
                    and self._last_gate_verdict.outcome == "fuse"
                )
                if fused:
                    # 同因失败熔断：不再盲目重试，按未完成发布。
                    self._record_event(
                        "verification/fused", reasons=verification_reasons
                    )
                    break
                self._record_event(
                    "verification/retry",
                    reasons=verification_reasons,
                    attempts_left=retries_left,
                )
                feedback = (
                    "【任务校验未通过，请修复后重新总结】\n"
                    + "\n".join(f"- {r}" for r in verification_reasons)
                    + "\n要求：1) 重新执行失败的工具调用并修复；"
                    "2) 若无法修复，必须在最终总结中如实列出失败项及其影响，不得宣称成功；"
                    "3) 修复后再给出结论。"
                )
                self.ctx_mgr.add_user_message(feedback)
                # 旧引擎为单次运行语义：重试轮新建引擎，复用同一会话历史。
                callback = self._make_callback()
                engine = spec.factory(
                    model_priority=model_ids,
                    model_pool=self.model_pool,
                    auto_router=self.auto_router,
                    callback=callback,
                    model_configs=dict(self.registry.models),
                    permission_gate=self._permission_gate,
                )
                self._inject_mcp_tools_into_engine(engine)
                self._bind_interactive_tool_runtime(engine)
                # 决策 2：重试轮预算 = 首轮实际步数的一半（clamp[10,40]）。
                for _attr in ("max_iterations", "max_steps", "react_iterations", "max_rounds"):
                    if hasattr(engine, _attr):
                        try:
                            setattr(
                                engine, _attr, _retry_budget_for(first_round_steps)
                            )
                        except Exception:  # noqa: BLE001 — 属性只读则跳过
                            pass
                        break
                self._start_log_capture()
                result = engine.run(
                    feedback, context=self.agent_context, ctx_mgr=self.ctx_mgr
                )
                self._captured_log = self._stop_log_capture()
                self._persist_engine_trace(engine)
                model_used = self._engine_model_used(engine, model_ids) or model_used
                self.ctx_mgr.add_assistant_message(result, model_used=model_used)
                panel = callback.get_thinking_panel()

            self._render_engine_result(callback, result, spec.result_title)
            # 结构层：回合节点状态（TurnGate 单一来源）。
            final_status = "passed" if verification_ok else "failed-retried"
            verdict = getattr(self, "_last_gate_verdict", None)
            if verdict is not None and verdict.outcome == "fuse":
                final_status = "fused"
            self._finish_turn_node(
                final_status,
                engine=spec.name,
                reasons=None if verification_ok else verification_reasons,
                retries_used=retries_spent,
                panel=panel,
            )
            # P1-High 问题1 修复: 引擎模式统一通过 record_model_success 更新状态
            if model_used:
                self.auto_router.record_model_success(model_used)
        except Exception as e:
            self._captured_log = self._stop_log_capture()
            if spec.preserve_thinking_panel:
                # 异常退出时也保留面板。缺了这步，重试/LLM 报错后 Ctrl+O 只有
                # 原始日志可看，详细工具时间线看起来像丢了。
                try:
                    self._last_thinking_panel = callback.get_thinking_panel()
                except Exception:
                    self._last_thinking_panel = None
            self._persist_engine_trace(engine)
            # 结构层：异常 → 回合节点 interrupted（“继续”续接的唯一依据）。
            panel_exc = None
            try:
                panel_exc = callback.get_thinking_panel()
            except Exception:  # noqa: BLE001 — 面板获取失败不影响状态写入
                panel_exc = None
            self._finish_turn_node(
                "interrupted",
                engine=spec.name,
                reasons=[str(e)[:120]],
                panel=panel_exc,
            )
            # 异常时展开日志便于调试
            if self._last_mode_line:
                console.print(f"[dim]{self._last_mode_line}[/dim]")
            if self._captured_log:
                console.print(Text(self._captured_log.rstrip(), style="dim"))
            if spec.log_result_diagnostics:
                import traceback

                logger.error(f"{spec.label} 引擎异常:\n{traceback.format_exc()}")
            console.print(f"[error]❌ {spec.label} 引擎执行失败: {e}[/error]")
            self._record_engine_error(
                f"[错误] {spec.label} 引擎执行失败: {e}", model_ids[0]
            )
        finally:
            # 停掉 steering 监听线程（engine.run 已返回，不再需要输入注入）
            self._stop_steering_listener(steering_thread)
            if hasattr(callback, "finish_activity"):
                callback.finish_activity()
            self._persist_engine_trace(engine)
            if getattr(self, "_log_capture_active", False):
                self._captured_log = self._stop_log_capture()

    # ── Mid-task steering 输入监听 ──────────────────────────
    # 引擎运行期间，用户可以在终端继续输入。这些输入通过 engine.steer()
    # 注入任务（引擎在迭代检查点消费并让 LLM 自行判断如何调整），而不是
    # 排队到引擎结束后的下一轮对话。斜杠命令 / 空行在监听线程内忽略——
    # 它们仍由主循环处理，避免两个线程竞争同一 stdin。


    def _start_steering_listener(self, engine: Any) -> threading.Thread | None:
        """启动 steering 监听线程；非 TTY / 不可用环境静默降级返回 None。"""
        import sys

        if self._pt_session is not None or not sys.stdin.isatty():
            # prompt_toolkit 会话或非交互输入：无法在引擎运行期间安全地
            # 从后台线程读 stdin，静默降级（功能不可用时不影响主流程）。
            return None
        stop = threading.Event()
        thread = threading.Thread(
            target=self._steering_listener_loop,
            args=(engine, stop),
            name="steering-listener",
            daemon=True,
        )
        thread._xenon_stop_event = stop  # type: ignore[attr-defined]
        thread.start()
        return thread


    def _steering_listener_loop(self, engine: Any, stop: threading.Event) -> None:
        """监听线程主体：阻塞读一行，非空且非斜杠命令则注入 engine。"""
        import sys

        while not stop.is_set():
            try:
                line = sys.stdin.readline()
            except Exception:  # noqa: BLE001 — 读取失败即退出监听
                return
            if not line:
                return  # EOF
            text = line.strip()
            if not text or text.startswith("/"):
                continue
            try:
                if engine.steer(text):
                    console.print("[dim]↪ 已收到你的补充，Agent 正在调整计划…[/dim]")
            except Exception:  # noqa: BLE001 — steering 失败不崩溃主流程
                return


    def _stop_steering_listener(self, thread: threading.Thread | None) -> None:
        """停止监听线程并等待其退出（防悬挂线程）。"""
        if thread is None:
            return
        stop = getattr(thread, "_xenon_stop_event", None)
        if stop is not None:
            stop.set()
        # readline() 是阻塞的；主流程已结束，此处仅尽力 join（短超时），
        # 线程本身是 daemon，不会阻止进程退出。
        thread.join(timeout=0.5)

    # ── 兼容层 ─────────────────────────────────────────────
    # 旧的 per-engine 方法名保留为薄封装：外部脚本、测试和 monkeypatch 可能
    # 直接引用它们。实现统一走 _run_engine。


    def _run_react_engine(self, user_input: str, model_ids: list[str]) -> None:
        """ReAct 引擎模式（兼容入口，实现见 _run_engine）。"""
        self._run_engine(ENGINE_REGISTRY.require("react"), user_input, model_ids)


    def _run_plan_execute_engine(self, user_input: str, model_ids: list[str]) -> None:
        """Plan-Execute 引擎模式（兼容入口）。"""
        self._run_engine(ENGINE_REGISTRY.require("plan-execute"), user_input, model_ids)


    def _run_reflection_engine(self, user_input: str, model_ids: list[str]) -> None:
        """Reflection 引擎模式（兼容入口）。"""
        self._run_engine(ENGINE_REGISTRY.require("reflection"), user_input, model_ids)


    def _run_plan_react_engine(self, user_input: str, model_ids: list[str]) -> None:
        """Plan + React 组合引擎模式（兼容入口）。"""
        self._run_engine(ENGINE_REGISTRY.require("plan-react"), user_input, model_ids)


    def _run_plan_reflection_engine(
        self, user_input: str, model_ids: list[str]
    ) -> None:
        """Plan + Reflection 组合引擎模式（兼容入口）。"""
        self._run_engine(
            ENGINE_REGISTRY.require("plan-reflection"), user_input, model_ids
        )


    def _run_react_reflection_engine(
        self, user_input: str, model_ids: list[str]
    ) -> None:
        """ReAct + Reflection 组合引擎模式（兼容入口）。"""
        self._run_engine(
            ENGINE_REGISTRY.require("react-reflection"), user_input, model_ids
        )


