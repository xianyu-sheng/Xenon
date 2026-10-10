"""Turn flow (R1-3: extracted from repl.py).

The per-turn pipeline: context-window sync, chat handling (continuation,
plan mode, routing dispatch), mode selection, intent resolution, turn
contract, pending-action update, goal projection, task-state block and
memory suggestions. Mixed into REPL as ``TurnFlowMixin``.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from rich.prompt import Prompt

from xenon.engine.registry import ENGINE_REGISTRY
from xenon.repl.execution_policy import (
    ExecutionLevel,
    ExecutionPolicy,
    bind_execution_boundary,
)
from xenon.repl.turn_contract import (
    PendingAction,
    TurnContract,
    build_turn_contract,
    continuation_hint,
    detect_pending_action,
    is_continuation_utterance,
    should_consume_pending,
)
from xenon.repl.prompt_optimizer import get_intent_display, optimize_prompt
from xenon.repl.system_config import get_config
from xenon.session.query import needs_recall

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


class TurnFlowMixin:
    def _sync_context_window(self, model_aliases: list[str]) -> None:
        """R4: 按激活模型的实际上下文窗口校准 ContextManager.max_tokens。

        取激活模型中 context_window 的最小值（瓶颈模型），保证最小窗口模型
        也不会超限；均未配置则保持默认。替代原先 128000 硬编码——8k 模型时
        needs_compact 永不触发（实际已超限），1M 模型时过早压缩。
        """
        window = self.registry.context_window_for(model_aliases)
        if window > 0:
            self.ctx_mgr.max_tokens = window


    def _handle_chat(
        self,
        user_input: str,
        *,
        skill_name: str | None = None,
        skill_args: str = "",
    ) -> None:
        """处理多轮对话，支持 prompt 优化和多种思考范式。"""
        # P2-修复2: 空输入防护 — 避免空 user 消息污染 history + 浪费 LLM token
        # run() 主循环 line 165 也有防护，但 _handle_chat 是独立可调用的方法，
        # 直接调（如测试或 API 入口）时无防护会 add_user_message("") 进入完整流程
        if not user_input or not user_input.strip():
            console.print("[dim]· 空输入已忽略[/dim]")
            return
        self._last_user_text = user_input
        self._ensure_turn_tree_rebuilt()

        # 跨回合续接：短确认语 + 未兑现承诺 = 继承上轮意图/级别/范式；
        # 待批 Gate 则接受更宽的确认语（“好写入到桌面”/“请写入”）。
        # 没有承诺时明确澄清，不让模型把“继续”当新话题重新回答。
        bare_continuation = is_continuation_utterance(user_input)
        pending_reply = self._pending_action is not None and should_consume_pending(
            self._pending_action, user_input
        )
        pending = self._pending_action if pending_reply else None
        if bare_continuation and pending is None:
            # 结构层唯一续接通道：树尾节点状态（中断/熔断/校验失败）。
            notice, user_input = self._resolve_bare_continuation(user_input)
            if notice is not None:
                console.print(f"[dim]{notice}[/dim]")
                return
        if pending_reply and self._pending_action is not None:
            if self._pending_action.gate_id:
                self._record_event(
                    "gate/resolved",
                    gate_id=self._pending_action.gate_id,
                    outcome="consumed",
                )
        if self._pending_action is not None and not pending_reply:
            # 用户在承诺未兑现前切换了话题：旧承诺失效，避免过期授权。
            if self._pending_action.gate_id:
                self._record_event(
                    "gate/resolved",
                    gate_id=self._pending_action.gate_id,
                    outcome="stale",
                )
            self._pending_action = None
        # 每轮重置：边界审批的 allowed-once 只对本轮有效；TurnGate 状态机同样清零。
        self._boundary_approved_once.clear()
        self._turn_gate.reset()

        # v0.6.0: 智能路由 - 根据用户输入自动选择推理范式
        if self.intelligent_router.enabled:
            routing_decision = self.intelligent_router.route(
                user_input=user_input,
                context=self.ctx_mgr,
                current_mode=self.registry.current_mode
            )

            if routing_decision is not None:
                # 建议切换范式
                old_mode = self.registry.current_mode
                try:
                    self.registry.set_mode(routing_decision.paradigm)
                    self.status_bar.set_mode_notification(routing_decision.paradigm)

                    if self.intelligent_router.notify_user:
                        console.print(
                            f"\n[dim]┌─ 智能路由: {old_mode} → "
                            f"[bold]{routing_decision.paradigm}[/bold][/dim]"
                        )
                        console.print(
                            f"[dim]│  {routing_decision.reason}[/dim]"
                        )
                        console.print(
                            f"[dim]│  [置信度: {routing_decision.confidence:.0%}, "
                            f"规则: {routing_decision.matched_rule}][/dim]"
                        )
                except ValueError as e:
                    logger.warning(f"智能路由切换范式失败: {e}")
                    console.print(f"\n[yellow]⚠ 范式切换失败: {e}[/yellow]")

        # Side effects are authorized by the original request, never by the
        # optimizer's generated wording or by the selected reasoning mode.
        turn_contract_obj, execution_policy, intent, inherited_intent_source = (
            self._resolve_turn_contract(user_input, pending=pending)
        )
        if pending is not None and not turn_contract_obj.continuation:
            # 契约层判定不能续接（如与约束矛盾）时退化为普通回合。
            pending = None
        if self._plan_mode_active and turn_contract_obj.level >= ExecutionLevel.WRITE:
            # 计划模式是软引导：不剥夺能力，但把写/执行转为待确认。
            turn_contract_obj = self._plan_mode_contract(turn_contract_obj)
            execution_policy = turn_contract_obj.to_execution_policy()
            console.print(
                "[dim cyan]📋 计划模式：本轮只产出方案，写/执行需用户确认[/dim cyan]"
            )
        # 结构层：本回合节点入树（turn/user 事件带同一 turn_id 供重建）。
        self._turn_tree.append_turn(user_input[:200])
        self._record_event(
            "turn/user",
            turn_id=self._turn_tree.tail().turn_id,
            text=user_input[:500],
            intent=turn_contract_obj.intent,
            level=int(execution_policy.level),
            operations=sorted(turn_contract_obj.operations),
            continuation=pending is not None,
            deferred=bool(turn_contract_obj.deferred_write),
        )
        self._update_goal_projection(
            user_input, turn_contract_obj, continuation=pending is not None
        )
        self.agent_context.update(
            {
                "_execution_level": int(execution_policy.level),
                "_execution_reason": execution_policy.reason,
                "_turn_contract": turn_contract_obj,
                # Preserve filter constraints for terse continuations such as
                # "结果呢" that do not repeat the original query.
                "_query_constraint_source": inherited_intent_source or "",
            }
        )

        # Resolve the project before a memory command so the default
        # project-local destination is deterministic and visible in its receipt.
        self._inject_project_context()
        # SKILL.md is trusted installed context, not a fresh user statement.
        # Never interpret phrases inside it as consent to persist memory.
        if skill_name is None and self._handle_explicit_memory_request(user_input):
            return

        # R4: 按激活模型上下文窗口校准 token 阈值（须在 needs_compact 之前）
        self._sync_context_window(
            self.auto_router.route(
                user_input,
                count=3,
                intent=turn_contract_obj.intent,
                requires_tools=turn_contract_obj.requires_tools,
            )
        )
        # 自动 compact 检查：优先用事件日志的结构化回合归档（确定性、零 LLM）。
        if self.ctx_mgr.needs_compact():
            if self._auto_compact():
                console.print(
                    "[dim]· 已自动压缩早期对话（结构化回合归档，完整消息保留最近几轮）[/dim]"
                )
            else:
                console.print(
                    "[dim]· 对话较长，建议 [bold cyan]/compact[/bold cyan] 压缩[/dim]"
                )

        # 保存 undo 快照
        self.ctx_mgr.save_snapshot()

        # ── 项目上下文注入（首次对话时） ──────────────────
        self._inject_project_context()

        # ── 记忆注入 ──────────────────────────────────
        memory_query = skill_args if skill_name is not None else user_input
        self._inject_memories(memory_query or skill_name or "")

        # ── Prompt 优化（按需） ──────────────────────────
        # 单一语义通道：意图来自契约（LLM 或降级正则），这里不再另算。
        if skill_name is not None:
            # 技能流程同样使用契约意图，不再用正则另算。
            intent = turn_contract_obj.intent
            inherited_intent_source = None
        system_hint: str | None = None
        if pending is not None:
            optimized = user_input
            console.print(
                f"[dim cyan]⏩ 延续上轮任务 → 直接继续：{pending.reason}[/dim cyan]"
            )
        elif skill_name is not None:
            optimized = user_input
            console.print(
                f"[dim cyan]🧩 Agent Skill: {skill_name}（正文与资源按需加载）[/dim cyan]"
            )
        elif self.optimize_prompts:
            optimized, system_hint, was_optimized = optimize_prompt(
                user_input,
                intent=intent,
            )
            console.print(f"[dim]🎯 意图: {get_intent_display(intent)}[/dim]")

            if was_optimized:
                # 展示优化后的 prompt，帮助用户学习
                self._render_secondary_text(
                    "📝 优化后的 Prompt（供学习参考）", optimized
                )
            elif intent is not None:
                # 有明确任务意图，但提示词质量已足够好
                console.print("[dim]✅ 提示词质量良好，无需优化[/dim]")
            else:
                # 通用对话，无明确任务意图
                console.print("[dim]💬 通用对话[/dim]")
            # ── 缓存优化提示 ──
            if hasattr(self, "_cache_tracker") and self._cache_tracker:
                cr = self._cache_tracker
                total = cr.cache_hits + cr.cache_misses
                if total > 0 and was_optimized:
                    rate = cr.cache_hit_rate
                    cost = cr.estimated_cost_yuan
                    if rate < 0.70:
                        console.print(
                            f"[dim cyan]💡 提示词已结构化；当前缓存命中率 {rate:.0%}，累计费用 ¥{cost:.4f}[/dim cyan]"
                        )
                    else:
                        console.print(
                            f"[dim cyan]💡 提示词已结构化；缓存命中率 {rate:.0%}，累计费用 ¥{cost:.4f}[/dim cyan]"
                        )
        else:
            optimized = user_input

        # A per-turn hint is volatile.  Keeping it as a system overlay ahead
        # of history invalidates DeepSeek's exact-prefix cache on every intent
        # change.  Bind it to this user turn instead, where it is both scoped
        # correctly and retained as part of the reusable conversation.  Chat
        # greetings intentionally remain byte-for-byte unchanged.
        turn_prompt = optimized
        if system_hint and intent != "chat":
            turn_prompt = f"{optimized}\n\n## 本轮回答指导\n{system_hint}"
        if inherited_intent_source and pending is None:
            turn_prompt = (
                f"{turn_prompt}\n\n"
                "## 延续上轮任务\n"
                "保留上轮查询目标和全部筛选条件，继续取得实际结果；"
                "不要只返回查询 URL。\n"
                f"{inherited_intent_source}"
            )
        if pending is not None:
            turn_prompt = f"{turn_prompt}\n\n{continuation_hint(pending)}"
        if self._plan_mode_active:
            turn_prompt = (
                f"{turn_prompt}\n\n## 计划模式（软引导）\n"
                "先探索与设计，给出可执行的计划；本轮不要写盘或执行命令，"
                "相关动作需等待用户确认。"
            )
        if needs_recall(user_input):
            recall_text = self._recall_block(user_input)
            if recall_text:
                turn_prompt = f"{turn_prompt}\n\n{recall_text}"

        # Freeze authorization with this exact turn. ReAct must not mutate its
        # leading system prompt when the execution level changes.
        turn_prompt = bind_execution_boundary(turn_prompt, execution_policy.level)

        # 添加用户消息
        intent_source = inherited_intent_source or (
            user_input if intent in {"query", "research"} else ""
        )
        turn_prompt = self.ctx_mgr.add_request_message(
            turn_prompt,
            metadata={
                "intent": intent,
                "original_user_input": user_input,
                "intent_source": intent_source,
                "contextual_followup": bool(inherited_intent_source),
                "continuation": pending is not None,
            },
        )

        # 获取模型列表
        # v0.4.0: auto-route based on task difficulty
        if self.auto_router.is_empty():
            # Fallback to static registry for backward compat
            model_ids = self.registry.get_role_priority("planner")
        else:
            route_engine = (
                "react"
                if skill_name is not None or execution_policy.requires_tools
                else self.registry.current_mode.replace("-", "")
            )
            route_phase = {
                "direct": "chat",
                "react": "reason_act",
                "planexecute": "plan",
                "reflection": "execute",
            }.get(route_engine, "request")
            model_ids = self.auto_router.route(
                turn_prompt,
                self.ctx_mgr.get_messages(),
                count=3,
                preferred_models=self._preferred_model_ids or None,
                cache_engine=route_engine,
                cache_phase=route_phase,
                intent=turn_contract_obj.intent,
                requires_tools=turn_contract_obj.requires_tools,
            )
        if not model_ids:
            console.print(
                "[red]· 未配置模型，请先 [bold cyan]/setup[/bold cyan] 配置[/red]"
            )
            return

        # v0.5.2: 过滤本会话已失败的模型（统一入口，覆盖所有引擎模式）
        model_ids = [m for m in model_ids if m not in self._failed_models]
        if not model_ids:
            self._failed_models.clear()
            model_ids = self.registry.get_role_priority("planner")
            if not model_ids:
                console.print("[red]· 所有模型均已失败且无法恢复[/red]")
                return
            console.print("[dim]· 所有模型已重置失败状态，重新尝试[/dim]")

        # ── 上下文桥接（v0.6.1 文档）──
        # 引擎期望 engine/context.py:AgentContext (get/set_conversation_messages)
        # REPL 持有 repl/context_manager.py:ContextManager (get_messages/add_message)
        # 每次引擎调用前手动同步。⚠️ 不可遗漏，否则引擎内部报 AttributeError。
        self.agent_context.set_conversation_messages(self.ctx_mgr.get_messages())

        # 根据当前思考范式选择执行方式
        mode = self._select_turn_mode(
            user_input,
            execution_policy,
            turn_contract_obj.intent,
            turn_contract_obj.requires_tools,
        )
        if pending is not None and pending.engine:
            spec = ENGINE_REGISTRY.get(pending.engine)
            if spec is not None and spec.runs_engine and mode != pending.engine:
                mode = pending.engine
                console.print(
                    f"[dim cyan]⏩ 延续上轮任务 → 继续使用 "
                    f"[bold]{mode}[/bold] 范式[/dim cyan]"
                )

        try:
            if skill_name is not None:
                self._run_react_engine(turn_prompt, model_ids)
            elif (
                execution_policy.level is ExecutionLevel.ANSWER_ONLY
                and intent == "write_code"
            ):
                if mode != "direct":
                    console.print(
                        "[dim cyan]💬 本轮未授权文件或命令操作，"
                        "使用仅回答模式...[/dim cyan]"
                    )
                self._run_direct(
                    turn_prompt,
                    model_ids,
                    intent=intent,
                    execution_policy=execution_policy,
                )
            else:
                # 由 ENGINE_REGISTRY 查表分发，替代原先的 if/elif 链。
                # 未注册的 mode 显式报错，不静默回落到 direct —— 那种回落会让
                # 用户以为在跑新范式，其实在跑 direct，且没有任何提示。
                spec = ENGINE_REGISTRY.get(mode)
                if spec is None:
                    available = ", ".join(sorted(ENGINE_REGISTRY.names()))
                    console.print(
                        f"[error]❌ 未注册的思考范式: {mode}[/error]\n"
                        f"[dim]可用: {available}[/dim]"
                    )
                    logger.error("未注册的思考范式: %s（可用: %s）", mode, available)
                    self._record_engine_error(
                        f"[错误] 未注册的思考范式: {mode}", model_ids[0]
                    )
                elif spec.runs_engine:
                    self._run_engine(spec, turn_prompt, model_ids)
                else:
                    # direct 模式 — 直接调 LLM，不走引擎循环
                    self._run_direct(
                        turn_prompt,
                        model_ids,
                        intent=intent,
                        execution_policy=execution_policy,
                    )
        except KeyboardInterrupt:
            # B2: Ctrl+C 取消当前运行，返回提示符而非退出整个 REPL
            if getattr(self, "_log_capture_active", False):
                self._captured_log = self._stop_log_capture()
            try:
                self.ctx_mgr.add_assistant_message("[已中断] 当前任务已由用户取消。")
            except Exception:
                self.ctx_mgr.trim_last_user()
            console.print("\n[dim]· 已中断，返回提示符[/dim]")

        # ``use_count`` means a memory reached a successfully completed answer,
        # not merely that retrieval considered it. This keeps retention metrics
        # honest and separate from ``retrieval_count``.
        self._ensure_turn_node_finished()
        self._commit_memory_usage()

        # A suggestion is shown after the answer, at most once per turn.  It is
        # deliberately independent from XENON_ASSUME_YES: test/automation flags
        # must never become consent for long-term memory.
        # 本轮一旦以“回复继续即可”收尾，就登记为跨回合承诺；否则清除。
        # 放在记忆建议之前：记忆确认可能与用户交互，不应影响承诺识别。
        self._update_pending_action(
            mode=mode,
            contract=turn_contract_obj,
            policy=execution_policy,
        )

        if skill_name is None:
            self._maybe_suggest_memory(user_input)

    # 自动切换范式的置信度门槛。低于此值保留当前范式——宁可少切，
    # 也不要让用户觉得范式在背后乱跳。
    _ENGINE_SWITCH_THRESHOLD = 0.6


    def _select_turn_mode(
        self,
        user_input: str,
        execution_policy: ExecutionPolicy,
        intent: str | None = None,
        requires_tools: bool | None = None,
    ) -> str:
        """为本轮选择范式：仅在用户停留在默认 direct 时才自动升级。

        这里刻意**只返回本轮使用的范式，不写回 registry.current_mode**：
        自动路由是一次性的判断，用户下一句话可能完全换个任务；把它固化成
        会话状态会让 /mode 显示的内容与实际执行的范式长期不一致。

        用户一旦显式 /mode 或 Shift+Tab 选过范式，就完全尊重该选择——
        自动化不该覆盖人的明确意图。
        """
        current = self.registry.current_mode
        if current != "direct":
            return current
        # 无需工具的轮次没有范式可选：引擎循环的价值全在工具调用上。
        if not execution_policy.requires_tools:
            return current
        if get_config().engine.disable_auto_routing:
            return current

        try:
            profile = self.auto_router.estimator.estimate(
                user_input,
                self.ctx_mgr.get_messages(),
                intent=intent,
                requires_tools=requires_tools,
            )
        except Exception as exc:  # noqa: BLE001 — 推荐失败不该阻断对话
            logger.debug("范式推荐失败，保留当前范式: %s", exc)
            return current

        engine = getattr(profile, "recommended_engine", "direct")
        confidence = float(getattr(profile, "engine_confidence", 0.0) or 0.0)
        if engine == current or confidence < self._ENGINE_SWITCH_THRESHOLD:
            return current
        if ENGINE_REGISTRY.get(engine) is None:
            logger.warning("推荐了未注册的范式 %r，保留 %s", engine, current)
            return current

        reason = getattr(profile, "engine_reason", "") or "任务结构更适合该范式"
        console.print(
            f"[dim cyan]🧭 {reason} → 本轮使用 [bold]{engine}[/bold] 范式"
            f"（/mode 可固定，config.yaml engine.disable_auto_routing 可关闭）[/dim cyan]"
        )
        return engine


    @staticmethod
    def _detect_intent(text: str) -> str | None:
        """检测用户意图。"""
        from xenon.repl.prompt_optimizer import detect_intent

        return detect_intent(text)


    def _resolve_turn_intent(self, text: str) -> tuple[str | None, str | None]:
        """回退意图（仅降级模式）。

        单一语义通道：LLM 分类器可用时，意图与跨轮次绑定全部由它负责
        （配合任务状态块），这里不再用正则猜语义；分类器不可用时才走
        旧的 query/research 继承逻辑作为保守回退。
        """

        classifier = (
            self._get_intent_classifier()
            if getattr(self, "_intent_classifier_checked", False)
            else None
        )
        if classifier is not None and classifier.enabled:
            return None, None

        from xenon.repl.prompt_optimizer import is_contextual_followup

        raw_intent = self._detect_intent(text)
        if not is_contextual_followup(text):
            return raw_intent, None

        history = getattr(self.ctx_mgr, "history", [])
        for turn in reversed(history[-20:]):
            if getattr(turn, "role", "") != "user":
                continue
            metadata = getattr(turn, "metadata", {}) or {}
            previous_intent = metadata.get("intent")
            content = str(getattr(turn, "content", ""))
            if previous_intent is None:
                previous_intent = self._detect_intent(content)
            if previous_intent in {"query", "research"}:
                source = str(
                    metadata.get("intent_source")
                    or metadata.get("original_user_input")
                    or content
                ).strip()
                return str(previous_intent), source[:2000] or None
            # Legacy versions mislabeled retrieval follow-ups as debug.  Skip
            # those, but do not jump across an unrelated substantive task.
            if previous_intent not in {None, "debug", "chat"}:
                break
        return raw_intent, None


    def _task_state_block(self) -> str:
        """确定性生成跨轮次任务状态块（目标/门/产物/计划模式）。"""

        try:
            from xenon.repl.task_state import build_task_state_block
            from xenon.session.projection import project

            view = None
            log = getattr(self, "_session_events", None)
            if log is not None:
                view = project(log.read())
            block = build_task_state_block(
                active_goal=view.active_goal if view else None,
                pending=self._pending_action,
                # R4: 产物来自树尾节点（单一来源）；目标仍由投影提供。
                artifacts=self._turn_tree.tail().artifacts,
                plan_mode=self._plan_mode_active,
            )
            suffix = self._turn_tree.status_suffix()
            if suffix:
                block = (
                    block + "\n- " + suffix
                    if block
                    else "## 任务状态（跨轮次）\n- " + suffix
                )
            return block
        except Exception:  # noqa: BLE001 — 状态块失败不能阻断回合
            logger.debug("任务状态块构建失败（已忽略）", exc_info=True)
            return ""


    def _resolve_turn_contract(
        self, user_input: str, pending: PendingAction | None = None
    ) -> tuple[TurnContract, ExecutionPolicy, str | None, str | None]:
        """本轮唯一一次分类：正则信号 +（可选）LLM 意图 → 不可变契约。

        所有下游（引擎路由、工具 schema、证据门、策略提示）都应消费
        ``self.agent_context["_turn_contract"]``，不得再重新分类。
        续接承诺（``pending`` + 短确认语）时不做 LLM 分类：上一轮已经
        决定了意图与级别，本轮只继承，避免“继续”被当成新闲聊。
        """

        intent, inherited_intent_source = self._resolve_turn_intent(user_input)
        task_state = self._task_state_block()
        if pending is not None:
            inherited = build_turn_contract(
                user_input, pending=pending, task_state=task_state
            )
            if inherited.continuation:
                return (
                    inherited,
                    inherited.to_execution_policy(),
                    inherited.intent or intent,
                    inherited_intent_source,
                )
        contract = build_turn_contract(
            user_input,
            classifier=self._get_intent_classifier(),
            context_messages=self.ctx_mgr.get_messages()[-4:],
            fallback_intent=intent,
            task_state=task_state,
        )
        if contract.ask_required:
            contract = self._confirm_write_escalation(contract)
        return (
            contract,
            contract.to_execution_policy(),
            contract.intent or intent,
            inherited_intent_source,
        )


    def _update_pending_action(
        self,
        *,
        mode: str,
        contract: TurnContract,
        policy: ExecutionPolicy,
    ) -> None:
        """记录本轮结尾的“回复继续”承诺；无承诺则清除，避免过期授权。"""

        # Stop hook（回合尾）：exit 0 = 请求停止，记事实 + 提示。
        if self._hook_runner is not None:
            try:
                outcome = self._hook_runner.run(
                    "Stop", "", {}, extra={"last_user_text": getattr(self, "_last_user_text", "")}
                )
                if outcome is not None and outcome.stop:
                    self._record_event("hook/stop")
                    console.print("[dim]· hook 请求停止本轮[/dim]")
                elif outcome is not None and outcome.message:
                    console.print(
                        f"[dim]· hook 反馈: {outcome.message[:200]}[/dim]"
                    )
            except Exception:  # noqa: BLE001
                pass

        answer = ""
        for message in reversed(self.ctx_mgr.get_messages()[-8:]):
            if message.get("role") == "assistant":
                answer = str(message.get("content") or "")
                break
        self._record_event("turn/assistant", preview=(answer or "")[:300])

        # 条件式写入（“我让你写你再写”）：写入本轮未执行，登记为待批 Gate。
        if contract.deferred_write:
            pending_ops = frozenset(
                op
                for op in contract.operations
                if op in {"write", "create", "delete", "move", "execute"}
            )
            if pending_ops:
                gate_id = (
                    self._record_event(
                        "gate/opened",
                        kind="approval",
                        level=int(contract.proposed_level),
                        operations=sorted(pending_ops),
                        reason="上一轮用户要求确认后再写入",
                    )
                    or ""
                )
                self._pending_action = PendingAction(
                    engine=mode,
                    level=contract.proposed_level,
                    intent=contract.intent,
                    operations=pending_ops,
                    reason="上一轮用户要求确认后再写入",
                    promise="（待确认的写盘提案）",
                    kind="approval",
                    gate_id=gate_id,
                )
                return

        level = max(int(policy.level), int(contract.proposed_level))
        operations = contract.operations
        if not operations:
            # 引擎实际执行了工具、但契约层没记下操作（如分类器给了纯聊
            # 天意图）时，用本轮实际授权级别补出操作，否则承诺续接会降级。
            from xenon.repl.turn_contract import contract_for_level

            operations = contract_for_level(level).operations
        detected = detect_pending_action(
            answer,
            engine=mode,
            level=level,
            intent=contract.intent,
            operations=operations,
            reason="上一轮承诺在用户确认后继续执行",
        )
        if detected is not None:
            gate_id = (
                self._record_event(
                    "gate/opened",
                    kind=detected.kind,
                    level=int(detected.level),
                    operations=sorted(detected.operations),
                    reason=detected.reason,
                )
                or detected.gate_id
            )
            detected = PendingAction(
                engine=detected.engine,
                level=detected.level,
                intent=detected.intent,
                operations=detected.operations,
                reason=detected.reason,
                promise=detected.promise,
                kind=detected.kind,
                gate_id=gate_id,
            )
        self._pending_action = detected


    def _update_goal_projection(
        self,
        user_input: str,
        contract: TurnContract,
        *,
        continuation: bool,
    ) -> None:
        """Open/pause goals from durable turn facts (projection, not authority)."""

        log = getattr(self, "_session_events", None)
        if log is None:
            return
        try:
            from xenon.session.projection import project

            text = (user_input or "").strip()
            substantive = len(text) >= 8 and (
                contract.requires_tools
                or (contract.intent or "") not in {"chat", "explain"}
            )
            if not substantive:
                return
            view = project(log.read())
            active = view.active_goal
            if active is None:
                self._record_event("goal/opened", objective=text[:120])
                return
            if continuation or self._goal_related(text, active.objective):
                return
            self._record_event("goal/paused")
            self._record_event("goal/opened", objective=text[:120])
        except Exception:  # noqa: BLE001 — 投影失败不能影响回合
            logger.debug("目标投影更新失败（已忽略）", exc_info=True)


    def _maybe_suggest_memory(self, user_input: str) -> None:
        """Offer one post-answer candidate; persistence always needs this prompt."""
        try:
            if not getattr(sys.stdin, "isatty", lambda: False)():
                return
            proposal = self._get_memory_detector().propose(user_input)
            if proposal is None:
                return
            if not self._has_active_project() and proposal.scope.value.startswith(
                "project-"
            ):
                from xenon.memory import MemoryScope

                proposal.scope = MemoryScope.USER
                proposal.reason += "；当前为无项目模式"
            service = self._get_memory_service()
            conflicts = service.find_conflicts(
                proposal.content,
                scope=proposal.scope,
                kind=proposal.kind,
            )
            with self._terminal_waiting("等待记忆确认"):
                self._render_memory_proposal(
                    proposal,
                    service.destination_for(proposal.scope, proposal.kind),
                    conflicts,
                )
                choice = Prompt.ask(
                    "[bold cyan]处理这条记忆候选[/bold cyan]",
                    choices=["s", "e", "u", "l", "h", "t", "n"],
                    default="n",
                    show_choices=False,
                )
                if choice == "n":
                    console.print("[dim]· 已忽略，本轮没有写入记忆[/dim]")
                    return
                if choice == "e":
                    edited = Prompt.ask(
                        "编辑记忆内容", default=proposal.content
                    ).strip()
                    if not edited:
                        console.print("[dim]· 内容为空，已取消[/dim]")
                        return
                    proposal.content = edited
                elif choice == "u":
                    from xenon.memory import MemoryScope

                    proposal.scope = MemoryScope.USER
                elif choice == "l":
                    from xenon.memory import MemoryScope

                    proposal.scope = MemoryScope.PROJECT_LOCAL
                elif choice == "h":
                    from xenon.memory import MemoryScope

                    proposal.scope = MemoryScope.PROJECT_SHARED
                elif choice == "t":
                    from xenon.memory import MemoryScope

                    proposal.scope = MemoryScope.SESSION

            if not self._has_active_project() and proposal.scope.value.startswith(
                "project-"
            ):
                console.print(
                    "[error]❌ 当前未检测到项目，不能写入项目记忆；"
                    "请先进入具体项目目录[/error]"
                )
                return

            receipt = service.remember(
                proposal.content,
                scope=proposal.scope,
                kind=proposal.kind,
                source="user-confirmed-candidate",
                confidence=proposal.confidence,
            )
            self._render_memory_receipt(receipt)
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]· 已取消，本轮没有写入记忆[/dim]")
        except ValueError as exc:
            console.print(f"[error]❌ 未写入记忆：{exc}[/error]")
        except Exception as exc:
            # Memory UX must never hide or invalidate the answer that preceded it.
            logger.debug(f"记忆候选处理失败: {exc}", exc_info=True)

