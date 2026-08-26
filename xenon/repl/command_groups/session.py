"""Session and context management slash commands.

This group covers REPL lifecycle (exit, help), conversation state (undo, clear,
compact), session persistence (save, load, sessions, resume), routing history,
context inspection, and configuration inspection.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xenon.repl.command_groups.common import confirm_action
from xenon.repl.command_registry import (
    COMMANDS,
    ExitSignal,
    command_handler,
    register_command,
)

if TYPE_CHECKING:
    from xenon.repl.model_registry import ModelRegistry
    from xenon.repl.context_manager import ContextManager


# /exit /quit /bye ─────────────────────────────────────────

register_command("/exit", "退出 Xenon", "/exit")
register_command("/quit", "退出 Xenon（别名）", "/quit")
register_command("/bye", "退出 Xenon（别名）", "/bye")


@command_handler("/exit")
@command_handler("/quit")
@command_handler("/bye")
def _cmd_exit(**kwargs: Any) -> str:
    raise ExitSignal("bye")


# /help ────────────────────────────────────────────────────

register_command("/help", "显示所有可用命令", "/help [command_name]")


@command_handler("/help")
def _cmd_help(*, args: str, **kwargs: Any) -> str:
    if args:
        cmd = COMMANDS.get(f"/{args}")
        if cmd:
            return f"{cmd['description']}\n用法: {cmd['usage']}"
        return f"未知命令: /{args}"

    lines = ["可用命令:\n"]
    for name, info in COMMANDS.items():
        lines.append(f"  {name:<20} {info['description']}")
    lines.append("\n输入 /help <command> 查看详细用法")
    return "\n".join(lines)


# /history ──────────────────────────────────────────────────

register_command("/history", "查看路由调度历史（v0.4.0）", "/history [N]")


@command_handler("/history")
def _cmd_history(*, args: str, session_state: dict, **kwargs: Any) -> str:
    """v0.4.0: 显示最近的路由决策历史."""
    router = session_state.get("auto_router")
    if not router or not hasattr(router, "history"):
        return "路由历史不可用（自动路由尚未初始化）。"

    n_str = args.strip()
    try:
        n = int(n_str) if n_str else 10
    except ValueError:
        return "用法: /history [N]\nN 是要显示的记录条数（默认 10）。"

    records = router.history.recent(n)
    if not records:
        return "路由历史为空。发送一些任务后再查看。"

    lines = [f"[bold]最近 {len(records)} 条路由记录:[/bold]\n"]
    for i, r in enumerate(records, 1):
        dt = datetime.fromtimestamp(r.timestamp).strftime("%H:%M:%S")
        tier_info = f" 层级={r.task_tier}" if r.task_tier is not None else ""
        lines.append(
            f"  {i}. [{dt}] 意图={r.intent or '?'} "
            f"复杂度={r.complexity:.2f}{tier_info}"
        )
        lines.append(f"     输入: {r.user_input_preview}")
        if r.selected_models:
            model_strs = []
            for m, s in zip(r.selected_models, r.scores or [0.0] * len(r.selected_models)):
                model_strs.append(f"{m}({s:.1f})")
            lines.append(f"     模型: {', '.join(model_strs)}")
        lines.append("")
    return "\n".join(lines)


# /undo ────────────────────────────────────────────────────

register_command("/undo", "回退到上一个对话状态", "/undo")


@command_handler("/undo")
def _cmd_undo(*, ctx_mgr: ContextManager, **kwargs: Any) -> str:
    if ctx_mgr.undo():
        stats = ctx_mgr.stats()
        return f"✅ 已回退。剩余消息: {stats['total_messages']}"
    return "❌ 没有可回退的状态"


# /clear ───────────────────────────────────────────────────

register_command("/clear", "清空对话历史", "/clear")


@command_handler("/clear")
def _cmd_clear(*, ctx_mgr: ContextManager, **kwargs: Any) -> str:
    if not confirm_action("确认清空全部对话历史？", default=True):
        return "已取消"
    ctx_mgr.clear()
    return "✅ 对话历史已清空"


# /save ────────────────────────────────────────────────────

register_command("/save", "保存当前会话", "/save <session_name>")


@command_handler("/save")
def _cmd_save(
    *, args: str, ctx_mgr: ContextManager, session_state: dict,
    registry: ModelRegistry, **kwargs: Any,
) -> str:
    from xenon.repl.session import save_session

    name = args.strip()
    if not name:
        return "用法: /save <session_name>"

    history = ctx_mgr.export_history()
    agent_ctx = session_state.get("agent_context")
    ctx_store = agent_ctx.to_dict() if agent_ctx else {}

    path = save_session(
        name,
        history,
        ctx_store,
        registry.export_config(),
        extra={"working_memory": ctx_mgr.get_working_memory()},
    )
    return f"✅ 会话已保存: {path}"


# /load ────────────────────────────────────────────────────

register_command("/load", "加载已保存的会话", "/load <session_name>")


@command_handler("/load")
def _cmd_load(
    *, args: str, ctx_mgr: ContextManager, session_state: dict,
    registry: ModelRegistry, **kwargs: Any,
) -> str:
    from xenon.repl.session import load_session
    from xenon.engine.context import AgentContext

    name = args.strip()
    if not name:
        return "用法: /load <session_name>"

    try:
        data = load_session(name)
    except FileNotFoundError as exc:
        return f"❌ {exc}"

    if not confirm_action(f"加载会话 '{name}' 将覆盖当前对话历史，确认？", default=False):
        return "已取消"

    history = data.get("history", [])
    if not isinstance(history, list):
        return "❌ 加载失败: 会话 history 格式无效。"
    if not all(
        isinstance(msg, dict)
        and isinstance(msg.get("role"), str)
        and isinstance(msg.get("content"), str)
        for msg in history
    ):
        return "❌ 加载失败: 会话消息格式无效。"
    extra = data.get("extra", {})
    if not isinstance(extra, dict):
        extra = {}
    working_memory = extra.get("working_memory", {})
    if not isinstance(working_memory, dict):
        working_memory = {}
    context_store = data.get("context", {})
    if not isinstance(context_store, dict):
        context_store = {}
    ctx_mgr.save_snapshot()
    ctx_mgr.history.clear()
    for msg in history:
        ctx_mgr.add_message(
            msg["role"],
            msg["content"],
            model_used=msg.get("model_used"),
            node_id=msg.get("node_id"),
            metadata=msg.get("metadata", {}),
            task_tier=msg.get("task_tier", 3),
            turn_type=msg.get("turn_type"),
            semantic_group_id=msg.get("semantic_group_id"),
        )
    ctx_mgr.replace_working_memory(working_memory)

    restored_context = AgentContext(initial=context_store)
    session_state["agent_context"] = restored_context
    repl = session_state.get("_repl")
    recovery_notice = ""
    if repl is not None:
        repl.agent_context = restored_context
        restored_context.set_tool_checkpoint_callback(
            repl._persist_tool_checkpoint
        )
        from xenon.nodes.tool_executor import recover_tool_execution_checkpoint
        recovery_notice = recover_tool_execution_checkpoint(restored_context)

    model_config = data.get("model_config", {})
    if not isinstance(model_config, dict):
        model_config = {}
    if "model_config" in data:
        models = model_config.get("models", {})
        if not isinstance(models, dict):
            models = {}
        for alias, mcfg in models.items():
            if isinstance(mcfg, dict) and mcfg.get("model_id"):
                registry.add_model(mcfg["model_id"], alias)

    result = f"✅ 会话 '{name}' 已加载。消息数: {len(ctx_mgr.history)}"
    if recovery_notice:
        result += f"\n{recovery_notice}"
    return result


# /sessions ────────────────────────────────────────────────

register_command("/sessions", "列出所有已保存的会话", "/sessions")


@command_handler("/sessions")
def _cmd_sessions(**kwargs: Any) -> str:
    from xenon.repl.session import list_sessions

    sessions = list_sessions()
    if not sessions:
        return "暂无已保存的会话。"

    lines = ["已保存的会话:\n"]
    for s in sessions:
        lines.append(f"  {s['name']:<20} {s['saved_at'][:19]}  ({s['messages']} 条消息)")
    return "\n".join(lines)


# /resume ──────────────────────────────────────────────────

register_command("/resume", "列出 / 恢复保存的会话", "/resume [序号或名称]")


@command_handler("/resume")
def _cmd_resume(*, args: str, session_state: dict, **kwargs: Any) -> str:
    """断点恢复：列出所有会话，或按序号/名称加载指定会话。"""
    from xenon.repl.session import (
        list_sessions, load_session, get_session_age, _load_and_migrate,
        touch_session,
    )

    repl = session_state.get("_repl")
    if not repl:
        return "❌ REPL 实例不可用。"

    arg = args.strip()

    if not arg:
        sessions = list_sessions()
        if not sessions:
            return "没有已保存的会话。使用 /save <名称> 手动保存，或退出时自动保存。"

        from rich.table import Table
        from rich.console import Console as RichConsole

        table = Table(
            title="已保存的会话 · 输入 /resume <序号> 恢复",
            border_style="dim #64748b",
        )
        table.add_column("#", style="bold cyan", width=3, justify="right")
        table.add_column("名称", style="#67e8f9", max_width=32)
        table.add_column("时间", style="dim #94a3b8", width=12)
        table.add_column("消息", justify="right", width=6)
        table.add_column("范式", style="dim", width=10)

        for i, s in enumerate(sessions, 1):
            display_name = s["name"]
            if display_name.startswith("_auto"):
                display_name = "[上次自动保存]"
            age = get_session_age(s) or s["saved_at"][:16]
            paradigm = s.get("paradigm", "")
            table.add_row(
                str(i), display_name, age,
                str(s["messages"]), paradigm,
            )

        console_out = RichConsole()
        console_out.print()
        console_out.print(table)
        return ""

    sessions = list_sessions()
    if arg.isdigit():
        idx = int(arg)
        if idx < 1 or idx > len(sessions):
            return f"❌ 序号 {idx} 超出范围（共 {len(sessions)} 个会话）。"
        # Fix #75: 直接从路径加载，避免 name 字段与实际文件名不匹配
        session_path = Path(sessions[idx - 1]["path"])
        try:
            data = _load_and_migrate(session_path)
        except FileNotFoundError:
            return f"❌ 会话文件不存在: {session_path}"
        # 按序号恢复也是一次真实加载。_load_and_migrate 本身不计访问
        # （它被 list_sessions 循环调用，计在那里会让列一次表就把所有会话
        # 计数全加一），所以这里显式补记，与按名称恢复的口径保持一致。
        touch_session(session_path)
    else:
        # 命名模式：通过 load_session(name) 加载
        try:
            data = load_session(arg)
        except FileNotFoundError:
            return f"❌ 会话 '{arg}' 不存在。使用 /resume (无参数) 查看全部。"

    try:
        history = data.get("history", [])
        if not isinstance(history, list):
            return "❌ 恢复失败: 会话 history 格式无效。"
        if not all(
            isinstance(msg, dict)
            and isinstance(msg.get("role"), str)
            and isinstance(msg.get("content"), str)
            for msg in history
        ):
            return "❌ 恢复失败: 会话消息格式无效。"
        extra = data.get("extra", {})
        if not isinstance(extra, dict):
            extra = {}
        working_memory = extra.get("working_memory", {})
        if not isinstance(working_memory, dict):
            working_memory = {}
        model_config = data.get("model_config", {})
        if not isinstance(model_config, dict):
            model_config = {}
        context_store = data.get("context", {})
        if not isinstance(context_store, dict):
            context_store = {}
        repl.ctx_mgr.clear()
        if history:
            for msg in history:
                repl.ctx_mgr.add_message(
                    msg.get("role", "user"),
                    msg.get("content", ""),
                    model_used=msg.get("model_used"),
                    node_id=msg.get("node_id"),
                    metadata=msg.get("metadata", {}),
                    task_tier=msg.get("task_tier", 3),
                    turn_type=msg.get("turn_type"),
                    semantic_group_id=msg.get("semantic_group_id"),
                )
        repl.ctx_mgr.replace_working_memory(working_memory)

        from xenon.engine.context import AgentContext
        from xenon.nodes.tool_executor import recover_tool_execution_checkpoint
        restored_context = AgentContext(initial=context_store)
        repl.agent_context = restored_context
        repl._session_state["agent_context"] = restored_context
        restored_context.set_tool_checkpoint_callback(
            repl._persist_tool_checkpoint
        )
        recovery_notice = recover_tool_execution_checkpoint(restored_context)

        paradigm = extra.get("paradigm")
        if paradigm:
            try:
                repl.registry.set_mode(paradigm)
            except ValueError:
                pass

        mc = model_config
        if mc and repl.model_pool.is_empty():
            repl.model_pool.from_config(mc)

        age = get_session_age(data) or "未知时间"
        msgs = len(history)
        result = f"✅ 已恢复会话 ({age}) · {msgs} 条消息 · 范式: {paradigm or 'direct'}"
        if recovery_notice:
            result += f"\n{recovery_notice}"
        return result

    except Exception as exc:
        return f"❌ 恢复失败: {exc}"


# /context ─────────────────────────────────────────────────

register_command("/context", "显示当前上下文状态", "/context")


@command_handler("/context")
def _cmd_context(
    *, ctx_mgr: ContextManager, session_state: dict, **kwargs: Any,
) -> str:
    stats = ctx_mgr.stats()
    lines = [
        "上下文状态:\n",
        f"  消息总数: {stats['total_messages']}",
        f"  用户消息: {stats['user_messages']}",
        f"  助手消息: {stats['assistant_messages']}",
        f"  估算 Token: {stats['estimated_tokens']:,} / {stats['max_tokens']:,} ({stats['usage_ratio']})",
        f"  可回退次数: {stats['undo_available']}",
        f"  需要压缩: {'⚠️ 是' if stats['needs_compact'] else '否'}",
    ]

    agent_ctx = session_state.get("agent_context")
    if agent_ctx and agent_ctx.to_dict():
        lines.append("\nAgentContext 变量:")
        for k, v in agent_ctx.items():
            preview = str(v)[:100]
            lines.append(f"  {k}: {preview}")

    return "\n".join(lines)


# /compact ─────────────────────────────────────────────────

register_command("/compact", "压缩对话历史，释放 context window", "/compact [自定义摘要]")


@command_handler("/compact")
def _cmd_compact(
    *, args: str, ctx_mgr: ContextManager, registry: ModelRegistry, **kwargs: Any,
) -> str:
    summary = args.strip() if args.strip() else None
    model_ids = registry.get_role_priority("planner") if not summary else None
    result = ctx_mgr.compact(summary, model_priority=model_ids)
    stats = ctx_mgr.stats()
    return f"✅ 对话已压缩。当前 Token: {stats['estimated_tokens']:,} ({stats['usage_ratio']})\n\n摘要:\n{result}"


# /config ──────────────────────────────────────────────────

register_command("/config", "查看或保存当前配置", "/config [save <path>]")


@command_handler("/config")
def _cmd_config(*, args: str, registry: ModelRegistry, **kwargs: Any) -> str:
    parts = args.split()
    if parts and parts[0] == "save":
        path = parts[1] if len(parts) > 1 else "xenon_session.yaml"
        registry.save_to_file(path)
        return f"✅ 配置已保存到: {path}"

    import json
    config = registry.export_config()
    return f"当前配置:\n{json.dumps(config, indent=2, ensure_ascii=False)}"
