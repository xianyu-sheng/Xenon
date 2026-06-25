"""
Slash Commands — 斜杠命令处理器。

每个命令是一个独立的函数，接收 REPL 上下文并返回要显示的文本。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

if TYPE_CHECKING:
    from omniagent.repl.model_registry import ModelRegistry
    from omniagent.repl.context_manager import ContextManager

console = Console()


# ── 命令注册表 ────────────────────────────────────────────

COMMANDS: dict[str, dict[str, Any]] = {}


def register_command(name: str, description: str, usage: str = "") -> None:
    """注册一个斜杠命令。"""
    COMMANDS[name] = {"description": description, "usage": usage}


def dispatch_command(
    name: str,
    args: str,
    *,
    registry: ModelRegistry,
    ctx_mgr: ContextManager,
    session_state: dict[str, Any],
) -> str | None:
    """
    分发并执行一个斜杠命令。

    Returns:
        命令输出文本，None 表示无输出。
    """
    handler = _HANDLERS.get(name)
    if not handler:
        return f"未知命令: {name}。输入 /help 查看可用命令。"
    return handler(args=args, registry=registry, ctx_mgr=ctx_mgr, session_state=session_state)


# ── 命令处理器 ────────────────────────────────────────────

_HANDLERS: dict[str, Any] = {}


def _handler(name: str):
    """装饰器：注册命令处理函数。"""
    def decorator(func):
        _HANDLERS[name] = func
        return func
    return decorator


# /exit /quit /bye ─────────────────────────────────────────

class ExitSignal(Exception):
    """通知 REPL 主循环退出。"""
    pass


register_command("/exit", "退出 OmniAgent-CLI", "/exit")
register_command("/quit", "退出 OmniAgent-CLI（别名）", "/quit")
register_command("/bye", "退出 OmniAgent-CLI（别名）", "/bye")


@_handler("/exit")
@_handler("/quit")
@_handler("/bye")
def _cmd_exit(**kwargs: Any) -> str:
    raise ExitSignal("bye")


# /help ────────────────────────────────────────────────────

register_command("/help", "显示所有可用命令", "/help [command_name]")

@_handler("/help")
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


# /set_model ───────────────────────────────────────────────

# /model — 统一的模型管理：浏览添加 + 切换 + 快速注册（合并原 /set_model）────

register_command(
    "/model",
    "管理模型：浏览添加、切换、快速注册（一步完成）",
    "/model — 交互式管理\n/model <alias> <provider/model_name> — 快速添加并切换",
)

register_command(
    "/set_model",
    "模型管理（/model 别名，保留向后兼容）",
    "/set_model [alias] [provider/model_name]",
)


@_handler("/model")
@_handler("/set_model")
def _cmd_model(*, args: str, registry: ModelRegistry, session_state: dict, **kwargs: Any) -> str:
    """统一模型管理：注册 + 切换一步完成。

    - /model 无参数 + 无已注册模型 → 自动打开厂商模型浏览器，选择后注册并切换
    - /model 无参数 + 有已注册模型 → 显示模型列表，可切换或添加新模型
    - /model <alias> <provider/model> → 快速注册并自动切换
    """
    from rich.table import Table as _Table
    from rich.prompt import IntPrompt as _IntPrompt
    from rich.console import Console as _Console
    console = kwargs.get("console") or _Console()

    parts = args.split() if args.strip() else []
    registered = registry.list_models()

    # ── 有参数：快速注册 + 自动切换 ──
    if len(parts) >= 2:
        alias = parts[0]
        model_id = parts[1]
        extra = {}
        for p in parts[2:]:
            if "=" in p:
                k, v = p.split("=", 1)
                extra[k] = v
        try:
            config = registry.add_model(model_id, alias, **extra)
            # 自动切换
            registry.role_priority["planner"] = [alias]
            return f"✅ 已注册并切换: {alias} → {config.model_id}"
        except Exception as e:
            return f"❌ 注册失败: {e}"

    # ── 无参数 + 有已注册模型 → 显示列表可切换 ──
    if registered:
        current_aliases = registry.role_priority.get("planner", [])
        table = _Table(show_header=True, header_style="bold")
        table.add_column("#", style="cyan", width=4)
        table.add_column("别名", style="bold")
        table.add_column("模型 ID")
        table.add_column("状态")
        for i, m in enumerate(registered, 1):
            status = "[green]✦ 当前[/green]" if m.alias in current_aliases else ""
            table.add_row(str(i), m.alias, m.model_id, status)
        # 添加"注册新模型"选项
        table.add_row(str(len(registered) + 1), "[bold cyan]+ 添加新模型[/bold cyan]", "", "")
        console.print(table)
        console.print()

        try:
            choice = _IntPrompt.ask(
                "选择操作",
                choices=[str(i) for i in range(1, len(registered) + 2)],
                default="1",
            )
        except (KeyboardInterrupt, EOFError, OSError):
            return "已取消"

        if choice <= len(registered):
            selected = registered[choice - 1]
            registry.role_priority["planner"] = [selected.alias]
            return f"✅ 已切换到: {selected.alias} ({selected.model_id})"
        # else: choice == len(registered)+1 → 添加新模型，走厂商浏览器
        console.print("[dim]── 浏览可用模型 ──[/dim]")
        console.print()

    # ── 无参数 + 无已注册模型（或用户选择"添加新模型"）→ 厂商浏览器 ──
    from omniagent.repl.provider_registry import get_configured_providers
    configured = get_configured_providers()
    if not configured:
        return "❌ 尚未配置任何 API Key，请先执行 /setup 配置"

    table = _Table(show_header=True, header_style="bold")
    table.add_column("#", style="cyan", width=4)
    table.add_column("厂商", style="bold")
    table.add_column("模型")
    table.add_column("特点")

    all_models: list[tuple[str, str, str, str]] = []  # (model_id, short_name, provider_key, base_url)
    idx = 1
    for p in configured:
        if not p.models:
            table.add_row("-", p.name, "实时获取失败", "请检查 API Key / 网络 / base_url")
            continue
        for m in p.models:
            model_id = f"{p.key}/{m}"
            hint = _model_hint_local(m)
            table.add_row(str(idx), p.name, m, hint)
            all_models.append((model_id, m, p.key, p.base_url))
            idx += 1

    console.print(table)
    console.print()

    if not all_models:
        return "❌ 未能实时获取任何模型，请检查 API Key、网络或厂商 base_url"

    try:
        choice = _IntPrompt.ask(
            "输入模型编号（注册并立即切换）",
            choices=[str(i) for i in range(1, len(all_models) + 1)],
            default="1",
        )
    except (KeyboardInterrupt, EOFError, OSError):
        return "已取消"

    model_id, short_name, provider, base_url = all_models[choice - 1]
    alias = short_name.replace(".", "-")

    try:
        config = registry.add_model(model_id, alias, base_url=base_url)
        registry.role_priority["planner"] = [alias]
        return f"✅ 已注册并切换: {alias} → {config.model_id}"
    except Exception as e:
        return f"❌ 设置失败: {e}"


def _model_hint_local(model_name: str) -> str:
    hints = {
        "gpt-4o": "旗舰，全能", "gpt-4o-mini": "便宜，快速", "gpt-4-turbo": "上代旗舰",
        "gpt-3.5-turbo": "最便宜", "o1-preview": "推理增强", "o1-mini": "推理，便宜",
        "claude-sonnet-4-20250514": "最新旗舰", "claude-3-5-sonnet-20241022": "旗舰，编程强",
        "claude-3-5-haiku-20241022": "快速，便宜", "claude-3-opus-20240229": "最强推理",
        "deepseek-chat": "通用对话", "deepseek-coder": "编程专用", "deepseek-reasoner": "深度推理",
        "gemini-2.0-flash": "最新，快速", "gemini-1.5-pro": "长上下文",
        "glm-4-plus": "旗舰", "glm-4-flash": "快速，免费",
        "qwen-max": "旗舰", "qwen-plus": "性价比高", "qwen-turbo": "快速，便宜",
        "moonshot-v1-128k": "128K 上下文",
    }
    return hints.get(model_name, "")


# /remove_model ────────────────────────────────────────────

register_command("/remove_model", "移除一个模型", "/remove_model <alias>")

@_handler("/remove_model")
def _cmd_remove_model(*, args: str, registry: ModelRegistry, **kwargs: Any) -> str:
    alias = args.strip()
    if not alias:
        return "用法: /remove_model <alias>"
    if registry.remove_model(alias):
        return f"✅ 模型 '{alias}' 已移除"
    return f"❌ 模型 '{alias}' 不存在"


# /models ──────────────────────────────────────────────────

register_command("/models", "列出所有已注册的模型及其角色分配", "/models")

@_handler("/models")
def _cmd_models(*, registry: ModelRegistry, **kwargs: Any) -> str:
    models = registry.list_models()
    if not models:
        return "暂无已注册模型。使用 /model 浏览并添加模型。"

    lines = ["已注册模型:\n"]
    for m in models:
        lines.append(f"  [{m.alias}] {m.model_id}")
        if m.base_url:
            lines.append(f"           端点: {m.base_url}")

    if registry.role_priority:
        lines.append("\n角色分配:")
        for role, aliases in registry.role_priority.items():
            lines.append(f"  {role}: {' -> '.join(aliases)}")

    return "\n".join(lines)


# /set_role ────────────────────────────────────────────────

register_command(
    "/set_role",
    "为角色设置模型优先级",
    "/set_role <role> <alias1> [alias2] [alias3] ...",
)

@_handler("/set_role")
def _cmd_set_role(*, args: str, registry: ModelRegistry, **kwargs: Any) -> str:
    parts = args.split()
    if len(parts) < 2:
        return "用法: /set_role <role> <alias1> [alias2] ...\n" \
               "示例: /set_role planner claude gpt\n" \
               "       /set_role coder deepseek gpt-mini"

    role = parts[0]
    aliases = parts[1:]
    try:
        registry.assign_role(role, aliases)
        return f"✅ 角色 '{role}' 已设置优先级: {' -> '.join(aliases)}"
    except ValueError as e:
        return f"❌ {e}"


# /mode ────────────────────────────────────────────────────

register_command(
    "/mode",
    "切换或查看当前思考范式",
    "/mode [mode_name]\n可用: direct, plan-execute, react, reflection,\n      plan-react, plan-reflection, react-reflection",
)

@_handler("/mode")
def _cmd_mode(*, args: str, registry: ModelRegistry, **kwargs: Any) -> str:
    if not args:
        current = registry.get_current_mode()
        lines = [f"当前范式: {current.name} — {current.description}\n"]
        lines.append("可用范式:")
        for name, mode in registry.modes.items():
            marker = " <-- 当前" if name == current.name else ""
            lines.append(f"  {name:<16} {mode.description}{marker}")
        return "\n".join(lines)

    try:
        mode = registry.set_mode(args.strip())
        return f"✅ 已切换到范式: {mode.name} — {mode.description}"
    except ValueError as e:
        return f"❌ {e}"


# /context ─────────────────────────────────────────────────

register_command("/context", "显示当前上下文状态", "/context")

@_handler("/context")
def _cmd_context(*, ctx_mgr: ContextManager, session_state: dict, **kwargs: Any) -> str:
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

    # 显示 AgentContext 中的变量
    agent_ctx = session_state.get("agent_context")
    if agent_ctx and hasattr(agent_ctx, "_store") and agent_ctx._store:
        lines.append("\nAgentContext 变量:")
        for k, v in agent_ctx._store.items():
            preview = str(v)[:100]
            lines.append(f"  {k}: {preview}")

    return "\n".join(lines)


# /compact ─────────────────────────────────────────────────

register_command("/compact", "压缩对话历史，释放 context window", "/compact [自定义摘要]")

@_handler("/compact")
def _cmd_compact(*, args: str, ctx_mgr: ContextManager, registry: ModelRegistry, **kwargs: Any) -> str:
    summary = args.strip() if args.strip() else None
    model_ids = registry.get_role_priority("planner") if not summary else None
    result = ctx_mgr.compact(summary, model_priority=model_ids)
    stats = ctx_mgr.stats()
    return f"✅ 对话已压缩。当前 Token: {stats['estimated_tokens']:,} ({stats['usage_ratio']})\n\n摘要:\n{result}"


# /undo ────────────────────────────────────────────────────

register_command("/undo", "回退到上一个对话状态", "/undo")

@_handler("/undo")
def _cmd_undo(*, ctx_mgr: ContextManager, **kwargs: Any) -> str:
    if ctx_mgr.undo():
        stats = ctx_mgr.stats()
        return f"✅ 已回退。剩余消息: {stats['total_messages']}"
    return "❌ 没有可回退的状态"


# /clear ───────────────────────────────────────────────────

register_command("/clear", "清空对话历史", "/clear")

@_handler("/clear")
def _cmd_clear(*, ctx_mgr: ContextManager, **kwargs: Any) -> str:
    ctx_mgr.clear()
    return "✅ 对话历史已清空"


# /save ────────────────────────────────────────────────────

register_command("/save", "保存当前会话", "/save <session_name>")

@_handler("/save")
def _cmd_save(*, args: str, ctx_mgr: ContextManager, session_state: dict, registry: ModelRegistry, **kwargs: Any) -> str:
    from omniagent.repl.session import save_session

    name = args.strip()
    if not name:
        return "用法: /save <session_name>"

    history = [{"role": t.role, "content": t.content, "model_used": t.model_used} for t in ctx_mgr.history]
    agent_ctx = session_state.get("agent_context")
    ctx_store = agent_ctx._store.copy() if agent_ctx and hasattr(agent_ctx, "_store") else {}

    path = save_session(name, history, ctx_store, registry.export_config())
    return f"✅ 会话已保存: {path}"


# /load ────────────────────────────────────────────────────

register_command("/load", "加载已保存的会话", "/load <session_name>")

@_handler("/load")
def _cmd_load(*, args: str, ctx_mgr: ContextManager, session_state: dict, registry: ModelRegistry, **kwargs: Any) -> str:
    from omniagent.repl.session import load_session
    from omniagent.engine.context import AgentContext

    name = args.strip()
    if not name:
        return "用法: /load <session_name>"

    try:
        data = load_session(name)
    except FileNotFoundError as e:
        return f"❌ {e}"

    # 恢复对话历史
    ctx_mgr.save_snapshot()
    ctx_mgr.history.clear()
    for msg in data.get("history", []):
        ctx_mgr.add_message(msg["role"], msg["content"], model_used=msg.get("model_used"))

    # 恢复 AgentContext
    session_state["agent_context"] = AgentContext(initial=data.get("context", {}))

    # 恢复模型配置
    if "model_config" in data:
        for alias, mcfg in data.get("model_config", {}).get("models", {}).items():
            registry.add_model(mcfg["model_id"], alias)

    return f"✅ 会话 '{name}' 已加载。消息数: {len(ctx_mgr.history)}"


# /sessions ────────────────────────────────────────────────

register_command("/sessions", "列出所有已保存的会话", "/sessions")

@_handler("/sessions")
def _cmd_sessions(**kwargs: Any) -> str:
    from omniagent.repl.session import list_sessions

    sessions = list_sessions()
    if not sessions:
        return "暂无已保存的会话。"

    lines = ["已保存的会话:\n"]
    for s in sessions:
        lines.append(f"  {s['name']:<20} {s['saved_at'][:19]}  ({s['messages']} 条消息)")
    return "\n".join(lines)


# /runs ────────────────────────────────────────────────────

register_command("/runs", "列出或查看 Agent run 事件记录", "/runs [run_id]")

@_handler("/runs")
def _cmd_runs(*, args: str, session_state: dict[str, Any], **kwargs: Any) -> str:
    from omniagent.engine.run_recorder import list_runs, load_run_events, summarize_run

    session = session_state.get("_runtime_session")
    session_root = getattr(session, "runs_dir", None)

    run_id = args.strip()
    if run_id:
        roots = [session_root, None] if session_root is not None else [None]
        summary = None
        events = []
        for root in roots:
            summary = summarize_run(run_id, root=root)
            if summary is not None:
                events = load_run_events(run_id, root=root)
                break
        if summary is None:
            return f"未找到 run: {run_id}"

        lines = [
            f"Run: {summary.run_id}",
            f"会话: {summary.session_id or '-'}",
            f"状态: {summary.status}",
            f"范式: {summary.mode or '-'}",
            f"开始: {summary.started_at or '-'}",
            f"结束: {summary.finished_at or '-'}",
            f"事件: {summary.event_count}",
            f"日志: {summary.events_path}",
            "",
            "最近事件:",
        ]
        for event in events[-8:]:
            event_type = event.get("type", "?")
            detail = ""
            if event_type == "tool.call_started":
                detail = f" {event.get('tool_name', '')}"
            elif event_type == "step.started":
                detail = f" step={event.get('step')} {event.get('task', '')}"
            elif event_type == "run.finished":
                detail = f" status={event.get('status')}"
            elif event_type == "review.finished":
                detail = f" score={event.get('score')} passed={event.get('passed')}"
            lines.append(f"  {event.get('seq', '-')}. {event_type}{detail}")
        return "\n".join(lines)

    roots = [session_root, None] if session_root is not None else [None]
    seen: set[str] = set()
    runs = []
    for root in roots:
        for item in list_runs(limit=10, root=root):
            if item.run_id in seen:
                continue
            seen.add(item.run_id)
            runs.append(item)
    runs.sort(key=lambda item: item.started_at, reverse=True)
    runs = runs[:10]

    if not runs:
        return (
            "暂无 run 事件记录。完成一次对话后会写入 "
            ".omniagent/sessions/<session_id>/runs/<run_id>/events.jsonl。"
        )

    lines = ["最近 run 记录:\n"]
    for item in runs:
        goal = item.goal.replace("\n", " ")
        if len(goal) > 54:
            goal = goal[:51] + "..."
        lines.append(
            f"  {item.run_id}  {item.status:<7} {item.mode or '-':<16} "
            f"{item.event_count:>3} events  {goal}"
        )
    lines.append("\n输入 /runs <run_id> 查看事件摘要和日志路径")
    return "\n".join(lines)


# /policy ──────────────────────────────────────────────────

register_command("/policy", "查看工具权限策略", "/policy")

@_handler("/policy")
def _cmd_policy(**kwargs: Any) -> str:
    from omniagent.engine.permissions import DEFAULT_POLICY_PATH, get_permission_manager

    manager = get_permission_manager()
    lines = [
        "工具权限策略:",
        f"  策略文件: {DEFAULT_POLICY_PATH.resolve()}",
        f"  文件存在: {'是' if DEFAULT_POLICY_PATH.exists() else '否'}",
        "",
        "当前工具默认策略:",
    ]
    for name in sorted(manager.policies):
        policy = manager.policies[name]
        label = policy.default
        if policy.deny_patterns:
            label += f", deny={len(policy.deny_patterns)}"
        if policy.allow_patterns:
            label += f", allow={len(policy.allow_patterns)}"
        lines.append(f"  {name:<16} {label}")

    lines.extend([
        "",
        "示例 .omniagent/policy.yaml:",
        "tools:",
        "  command:",
        "    deny_patterns:",
        "      - \"npm publish*\"",
        "  write_file:",
        "    deny_patterns:",
        "      - \"*.secret\"",
    ])
    return "\n".join(lines)


# /session ─────────────────────────────────────────────────

register_command("/session", "查看当前 runtime session 与最近 thread", "/session [thread [n]]")

@_handler("/session")
def _cmd_session(*, args: str, session_state: dict[str, Any], **kwargs: Any) -> str:
    store = session_state.get("_session_store")
    session = session_state.get("_runtime_session")
    if not store or not session:
        return "当前没有 runtime session。"

    parts = args.split()
    if parts and parts[0].lower() == "thread":
        limit = 8
        if len(parts) > 1 and parts[1].isdigit():
            limit = max(1, min(int(parts[1]), 50))
        entries = store.read_thread(session.id, limit=limit)
        if not entries:
            return f"Session {session.id} 暂无 thread 消息。"
        lines = [f"Session thread: {session.id}\n"]
        for entry in entries:
            content = str(entry.get("content", "")).replace("\n", " ")
            if len(content) > 120:
                content = content[:117] + "..."
            run_id = entry.get("run_id") or "-"
            lines.append(f"  {entry.get('role', '?'):<9} {run_id}  {content}")
        return "\n".join(lines)

    entries = store.read_thread(session.id)
    notes = store.read_notes(session.id)
    lines = [
        f"当前 session: {session.id}",
        f"标题: {session.title}",
        f"创建: {session.created_at}",
        f"更新: {session.updated_at}",
        f"Thread: {session.thread_path.resolve()}",
        f"Notes: {session.notes_path.resolve()}",
        f"消息数: {len(entries)}",
        f"Notes 字符数: {len(notes)}",
        "",
        "输入 /session thread 查看最近消息；输入 /notes add <内容> 追加长期 notes。",
    ]
    return "\n".join(lines)


# /notes ───────────────────────────────────────────────────

register_command("/notes", "查看或追加当前 session notes", "/notes [add <content>]")

@_handler("/notes")
def _cmd_notes(*, args: str, session_state: dict[str, Any], **kwargs: Any) -> str:
    store = session_state.get("_session_store")
    session = session_state.get("_runtime_session")
    if not store or not session:
        return "当前没有 runtime session。"

    if args.strip().lower().startswith("add "):
        note = args.strip()[4:].strip()
        if not note:
            return "用法: /notes add <内容>"
        path = store.append_note(session.id, note)
        session_state["_runtime_session"] = store.get(session.id)
        return f"✅ 已追加 notes: {path.resolve()}"

    notes = store.read_notes(session.id).strip()
    if not notes:
        return "当前 notes 为空。输入 /notes add <内容> 追加。"
    if len(notes) > 4000:
        notes = notes[-4000:]
    return notes


# /config ──────────────────────────────────────────────────

register_command("/config", "查看或保存当前配置", "/config [save <path>]")

@_handler("/config")
def _cmd_config(*, args: str, registry: ModelRegistry, **kwargs: Any) -> str:
    parts = args.split()
    if parts and parts[0] == "save":
        path = parts[1] if len(parts) > 1 else "omniagent_session.yaml"
        registry.save_to_file(path)
        return f"✅ 配置已保存到: {path}"

    # 显示当前配置
    config = registry.export_config()
    import json
    return f"当前配置:\n{json.dumps(config, indent=2, ensure_ascii=False)}"


# /run ─────────────────────────────────────────────────────

register_command("/run", "执行当前配置的工作流", "/run [workflow.yaml] [--init key=value]")

@_handler("/run")
def _cmd_run(*, args: str, session_state: dict, registry: ModelRegistry, **kwargs: Any) -> str:
    from omniagent.engine.context import AgentContext
    from omniagent.engine.scheduler import DAGScheduler
    from omniagent.utils.config_parser import load_yaml, parse_workflow

    parts = args.split()
    workflow_path = None
    init_vars = {}

    i = 0
    while i < len(parts):
        if parts[i] == "--init" and i + 1 < len(parts):
            kv = parts[i + 1]
            if "=" in kv:
                k, v = kv.split("=", 1)
                init_vars[k] = v
            i += 2
        elif not workflow_path:
            workflow_path = parts[i]
            i += 1
        else:
            i += 1

    if not workflow_path:
        workflow_path = registry.get_current_mode().workflow_template
        if not workflow_path:
            return "❌ 未指定工作流文件且当前范式无默认模板"

    try:
        config = load_yaml(workflow_path)
        nodes, models = parse_workflow(config)
    except Exception as e:
        return f"❌ 配置解析失败: {e}"

    # 合并 session_state 中的 context 变量
    agent_ctx = session_state.get("agent_context")
    if agent_ctx:
        for k, v in init_vars.items():
            agent_ctx.set(k, v)
    else:
        agent_ctx = AgentContext(initial=init_vars)
        session_state["agent_context"] = agent_ctx

    start_node = config.get("start_node")
    if not start_node:
        for nid in nodes:
            if nodes[nid].__class__.__name__ != "RouterNode":
                start_node = nid
                break

    scheduler = DAGScheduler(nodes, start_node_id=start_node)
    try:
        result = scheduler.run(agent_ctx)
        lines = [f"✅ 工作流完成。状态: {result['status']}, 步数: {result['steps']}"]
        for entry in result.get("log", []):
            lines.append(f"  [{entry['step']}] {entry['node']}: {entry['status']}")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ 工作流执行失败: {e}"


# /shell /open /new_terminal ───────────────────────────────

register_command(
    "/shell",
    "运行终端命令（也可直接输入 !<command>）",
    "/shell <command>\n示例: /shell python -m pytest tests -q",
)

@_handler("/shell")
def _cmd_shell(*, args: str, ctx_mgr: ContextManager, session_state: dict[str, Any], **kwargs: Any) -> str:
    from omniagent.repl.file_links import linkify_file_paths
    from omniagent.repl.shell_runner import format_shell_result, run_shell_command

    command = args.strip()
    if not command:
        return "用法: /shell <command>\n也可以直接输入 !<command>"

    agent_ctx = session_state.get("agent_context")
    result = run_shell_command(command, context=agent_ctx)
    session_state["_last_shell_result"] = result
    if agent_ctx:
        agent_ctx.set("_last_shell_command", result.command)
        agent_ctx.set("_last_shell_output", result.combined_output)
    ctx_mgr.add_user_message(f"[shell]\n$ {result.command}")
    ctx_mgr.add_assistant_message(
        f"Shell command {'succeeded' if result.success else 'failed'}.\n"
        f"Exit code: {result.returncode if result.returncode is not None else '-'}\n"
        f"{result.combined_output}"
    )
    return linkify_file_paths(format_shell_result(result))


register_command(
    "/open",
    "打开文件路径（支持 path:line，点击链接不可用时的兜底）",
    "/open <file_path[:line[:column]]>",
)

@_handler("/open")
def _cmd_open(*, args: str, **kwargs: Any) -> str:
    from omniagent.repl.file_links import format_file_link, open_file_target

    target = args.strip()
    if not target:
        return "用法: /open <file_path[:line[:column]]>"
    try:
        result = open_file_target(target)
    except Exception as e:
        return f"❌ 打开失败: {e}"
    link = format_file_link(result.target)
    cmd = f"\n命令: {' '.join(result.command)}" if result.command else ""
    return f"✅ {result.message}: {link}{cmd}"


register_command(
    "/new_terminal",
    "打开可观测子终端（Windows Terminal 优先分屏）",
    "/new_terminal [cwd]",
)

@_handler("/new_terminal")
def _cmd_new_terminal(*, args: str, session_state: dict[str, Any], **kwargs: Any) -> str:
    bridge = _get_terminal_bridge(session_state)
    cwd = args.strip() or None
    result = bridge.open_terminal(cwd=cwd)
    if result.session:
        session_state["_terminal_session"] = result.session
    return ("✅ " if result.success else "❌ ") + result.message


register_command(
    "/terminal_status",
    "查看子终端最近输出",
    "/terminal_status [lines]",
)

@_handler("/terminal_status")
def _cmd_terminal_status(*, args: str, session_state: dict[str, Any], **kwargs: Any) -> str:
    bridge = _get_terminal_bridge(session_state)
    return bridge.status(lines=_parse_line_count(args, default=40))


register_command(
    "/terminal_quote",
    "把子终端最近输出引用到当前对话上下文",
    "/terminal_quote [lines]",
)

@_handler("/terminal_quote")
def _cmd_terminal_quote(
    *, args: str, ctx_mgr: ContextManager, session_state: dict[str, Any], **kwargs: Any,
) -> str:
    bridge = _get_terminal_bridge(session_state)
    lines = _parse_line_count(args, default=80)
    tail = bridge.read_tail(lines=lines)
    if tail.startswith("暂无子终端") or tail.startswith("子终端日志不存在"):
        return tail

    quoted = f"[子终端引用 - 最近 {lines} 行]\n{tail}"
    ctx_mgr.add_user_message(quoted)
    agent_ctx = session_state.get("agent_context")
    if agent_ctx:
        agent_ctx.set("_last_terminal_quote", tail)
    return f"✅ 已引用子终端最近 {lines} 行到上下文。\n\n{tail}"


def _get_terminal_bridge(session_state: dict[str, Any]):
    from omniagent.repl.terminal_bridge import TerminalBridge

    bridge = session_state.get("_terminal_bridge")
    if bridge is None:
        bridge = TerminalBridge()
        session_state["_terminal_bridge"] = bridge
    return bridge


def _parse_line_count(args: str, *, default: int) -> int:
    text = args.strip()
    if not text:
        return default
    try:
        return max(1, min(500, int(text)))
    except ValueError:
        return default


# /ask ─────────────────────────────────────────────────────

register_command(
    "/ask",
    "向指定模型发送单次提问（不进入多轮对话）",
    "/ask <alias> <question>",
)

@_handler("/ask")
def _cmd_ask(*, args: str, registry: ModelRegistry, ctx_mgr: ContextManager, **kwargs: Any) -> str:
    from omniagent.utils.llm_client import chat_completion

    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        return "用法: /ask <alias> <question>"

    alias, question = parts[0], parts[1]
    model = registry.get_model(alias)
    if not model:
        return f"❌ 模型 '{alias}' 不存在。使用 /models 查看可用模型。"

    try:
        response = chat_completion(model.model_id, [{"role": "user", "content": question}])
        ctx_mgr.add_user_message(f"/ask {alias} {question}")
        ctx_mgr.add_assistant_message(response, model_used=model.model_id)
        return response
    except Exception as e:
        return f"❌ 调用失败: {e}"


# /code ────────────────────────────────────────────────────

register_command(
    "/code",
    "生成代码并写入文件，可选运行",
    "/code <任务描述> [--file path] [--run] [--lang python]",
)

@_handler("/code")
def _cmd_code(*, args: str, registry: ModelRegistry, ctx_mgr: ContextManager, session_state: dict, **kwargs: Any) -> str:
    import re
    import subprocess
    import sys
    from pathlib import Path
    from omniagent.utils.llm_client import chat_completion

    if not args:
        return "用法: /code <任务描述> [--file path] [--run] [--lang python]"

    # 解析参数
    parts = args.split()
    task_parts = []
    file_path = None
    run_code = False
    lang = "python"

    i = 0
    while i < len(parts):
        if parts[i] == "--file" and i + 1 < len(parts):
            file_path = parts[i + 1]
            i += 2
        elif parts[i] == "--run":
            run_code = True
            i += 1
        elif parts[i] == "--lang" and i + 1 < len(parts):
            lang = parts[i + 1]
            i += 2
        else:
            task_parts.append(parts[i])
            i += 1

    task = " ".join(task_parts)
    if not task:
        return "请提供任务描述"

    # 获取模型
    model_ids = registry.get_role_priority("coder") or registry.get_role_priority("planner")
    if not model_ids:
        return "❌ 未配置模型。请先 /set_model"

    # 生成代码
    prompt = f"""请根据以下任务生成 {lang} 代码。只输出代码，不要解释。

任务: {task}

要求:
1. 只输出代码，不要 markdown 代码块标记
2. 代码必须完整可运行
3. 包含必要的 import 和注释"""

    try:
        code = chat_completion(model_ids[0], [{"role": "user", "content": prompt}])
    except Exception as e:
        return f"❌ 代码生成失败: {e}"

    # 清理代码（移除可能的 markdown 标记）
    code = re.sub(r'^```\w*\n?', '', code, flags=re.MULTILINE)
    code = re.sub(r'\n?```$', '', code, flags=re.MULTILINE)
    code = code.strip()

    # 确定文件路径
    if not file_path:
        ext = {"python": ".py", "javascript": ".js", "typescript": ".ts", "bash": ".sh"}.get(lang, ".txt")
        file_path = f"generated_code{ext}"

    # 写入文件
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code, encoding="utf-8")

    result_lines = [f"✅ 代码已写入: {path.absolute()}"]
    result_lines.append(f"  语言: {lang}")
    result_lines.append(f"  行数: {len(code.splitlines())}")

    # 记录到 context
    ctx_mgr.add_user_message(f"/code {task}")
    ctx_mgr.add_assistant_message(f"生成代码并写入 {path}", model_used=model_ids[0])

    # 可选运行
    if run_code and lang == "python":
        result_lines.append("\n▶️  运行代码...")
        try:
            proc = subprocess.run(
                [sys.executable, str(path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode == 0:
                result_lines.append(f"✅ 运行成功:")
                if proc.stdout:
                    result_lines.append(proc.stdout)
            else:
                result_lines.append(f"❌ 运行失败 (返回码 {proc.returncode}):")
                if proc.stderr:
                    result_lines.append(proc.stderr)
                if proc.stdout:
                    result_lines.append(proc.stdout)
        except subprocess.TimeoutExpired:
            result_lines.append("⏰ 运行超时 (30s)")
        except Exception as e:
            result_lines.append(f"❌ 运行异常: {e}")

    return "\n".join(result_lines)


# /stream ──────────────────────────────────────────────────

register_command("/stream", "切换流式输出模式", "/stream [on|off]")

@_handler("/stream")
def _cmd_stream(*, args: str, session_state: dict, **kwargs: Any) -> str:
    repl = session_state.get("_repl")
    if repl:
        if args.strip().lower() == "on":
            repl.streaming = True
            repl.status_bar.set_streaming(True)
            return "✅ 流式输出已开启"
        elif args.strip().lower() == "off":
            repl.streaming = False
            repl.status_bar.set_streaming(False)
            return "✅ 流式输出已关闭"
        else:
            status = "开启" if repl.streaming else "关闭"
            return f"当前流式输出: {status}\n用法: /stream on 或 /stream off"
    return "❌ 无法获取 REPL 状态"


# /optimize ────────────────────────────────────────────────

register_command("/optimize", "切换输入指令自动优化", "/optimize [on|off]")

@_handler("/optimize")
def _cmd_optimize(*, args: str, session_state: dict, **kwargs: Any) -> str:
    repl = session_state.get("_repl")
    if repl:
        if args.strip().lower() == "on":
            repl.optimize_prompts = True
            return "✅ 输入优化已开启\n口语化输入将自动重构为结构化 prompt"
        elif args.strip().lower() == "off":
            repl.optimize_prompts = False
            return "✅ 输入优化已关闭\n输入将原样发送给模型"
        else:
            status = "开启" if repl.optimize_prompts else "关闭"
            return (
                f"当前输入优化: {status}\n\n"
                "开启后，口语化输入会自动重构为结构化 prompt，例如：\n"
                '  输入: "帮我写个快排"\n'
                '  优化: "## 任务\\n帮我写个快排\\n## 要求\\n代码完整可运行..."\n\n'
                "用法: /optimize on 或 /optimize off"
            )
    return "❌ 无法获取 REPL 状态"


# /verbose ──────────────────────────────────────────────────

register_command("/verbose", "切换详细输出模式（显示思考过程和工具调用）", "/verbose [on|off]")

@_handler("/verbose")
def _cmd_verbose(*, args: str, session_state: dict, **kwargs: Any) -> str:
    repl = session_state.get("_repl")
    if repl:
        if args.strip().lower() == "on":
            repl.verbose = True
            return "✅ 详细模式已开启\n引擎执行时将显示思考过程、工具调用和观察结果"
        elif args.strip().lower() == "off":
            repl.verbose = False
            return "✅ 详细模式已关闭"
        else:
            status = "开启" if repl.verbose else "关闭"
            return f"当前详细模式: {status}\n用法: /verbose on 或 /verbose off"
    return "❌ 无法获取 REPL 状态"


# /mcp ──────────────────────────────────────────────────

register_command("/mcp", "管理 MCP 服务器连接", "/mcp [add|list|tools|remove] [args]")

@_handler("/mcp")
def _cmd_mcp(*, args: str, session_state: dict, **kwargs: Any) -> str:
    repl = session_state.get("_repl")
    if not repl:
        return "❌ 无法获取 REPL 状态"

    from omniagent.mcp.registry import MCPRegistry

    parts = args.strip().split()
    sub = parts[0] if parts else "list"

    # 确保注册表存在
    if not hasattr(repl, '_mcp_registry') or repl._mcp_registry is None:
        repl._mcp_registry = MCPRegistry()
        repl.agent_context.set("_mcp_registry", repl._mcp_registry)

    registry = repl._mcp_registry

    if sub == "add":
        if len(parts) < 3:
            return "用法: /mcp add <name> <command_or_url> [args...]\n示例:\n  /mcp add fs npx -y @modelcontextprotocol/server-filesystem .\n  /mcp add web http://localhost:3000/sse"
        name = parts[1]
        target = parts[2]
        extra_args = parts[3:] if len(parts) > 3 else []

        try:
            if target.startswith("http"):
                registry.add_server(name, url=target)
            else:
                registry.add_server(name, command=target, args=extra_args)

            # 发现工具
            tools = registry.discover_tools()
            tool_count = len(tools.get(name, []))
            return f"✅ MCP 服务器 '{name}' 已连接\n发现 {tool_count} 个工具"
        except Exception as e:
            return f"❌ 连接失败: {e}"

    elif sub == "list":
        if not registry.clients:
            return "当前无 MCP 服务器。使用 /mcp add 添加。"
        lines = ["═══ MCP 服务器 ═══\n"]
        for name, client in registry.clients.items():
            info = client.server_info
            tool_count = len(client.tools)
            lines.append(f"  {name}: {info.get('name', 'unknown')} v{info.get('version', '?')} ({tool_count} 工具)")
        return "\n".join(lines)

    elif sub == "tools":
        if not registry.tool_map:
            registry.discover_tools()
        if not registry.tool_map:
            return "无可用 MCP 工具"
        lines = ["═══ MCP 工具 ═══\n"]
        for global_name, (server, tool) in sorted(registry.tool_map.items()):
            if ":" in global_name:
                desc = tool.get("description", "")[:60]
                lines.append(f"  {global_name}: {desc}")
        return "\n".join(lines)

    elif sub == "remove":
        if len(parts) < 2:
            return "用法: /mcp remove <name>"
        name = parts[1]
        if name in registry.clients:
            registry.clients[name].close()
            del registry.clients[name]
            # 重建工具映射
            registry.tool_map.clear()
            registry.discover_tools()
            return f"✅ MCP 服务器 '{name}' 已移除"
        return f"❌ 未找到 MCP 服务器 '{name}'"

    else:
        return "用法: /mcp [add|list|tools|remove] [args]"


# /status ──────────────────────────────────────────────────

register_command("/status", "显示详细状态信息", "/status")

@_handler("/status")
def _cmd_status(*, ctx_mgr: ContextManager, registry: ModelRegistry, session_state: dict, **kwargs: Any) -> str:
    from omniagent.repl.prompt_optimizer import get_intent_display

    stats = ctx_mgr.stats()
    mode = registry.get_current_mode()
    repl = session_state.get("_repl")

    lines = [
        "═══ 系统状态 ═══\n",
        f"  范式: {mode.name} — {mode.description}",
        f"  流式输出: {'开启' if repl and repl.streaming else '关闭'}",
        f"  输入优化: {'开启' if repl and repl.optimize_prompts else '关闭'}",
        f"  详细模式: {'开启' if repl and repl.verbose else '关闭'}",
        "",
        "═══ 上下文 ═══\n",
        f"  消息总数: {stats['total_messages']}",
        f"  用户消息: {stats['user_messages']}",
        f"  助手消息: {stats['assistant_messages']}",
        f"  Token 用量: {stats['estimated_tokens']:,} / {stats['max_tokens']:,} ({stats['usage_ratio']})",
        f"  可回退次数: {stats['undo_available']}",
        f"  需要压缩: {'⚠️ 是' if stats['needs_compact'] else '否'}",
        "",
        "═══ 模型 ═══\n",
    ]

    models = registry.list_models()
    if models:
        for m in models:
            lines.append(f"  [{m.alias}] {m.model_id}")
    else:
        lines.append("  (无)")

    if registry.role_priority:
        lines.append("\n═══ 角色分配 ═══\n")
        for role, aliases in registry.role_priority.items():
            lines.append(f"  {role}: {' -> '.join(aliases)}")

    return "\n".join(lines)


# /setup /set_up ───────────────────────────────────────────

register_command("/setup", "首次配置向导（配置 Key、选模型、选范式）", "/setup")
register_command("/set_up", "系统设置（/setup 别名）", "/set_up")

@_handler("/set_up")
@_handler("/setup")
def _cmd_setup(*, session_state: dict, **kwargs: Any) -> str:
    repl = session_state.get("_repl")
    if repl:
        from omniagent.repl.setup_wizard import interactive_setup

        interactive_setup(repl.registry)
        return ""
    return "❌ 无法获取 REPL 状态"


# /provider ────────────────────────────────────────────────

register_command("/provider", "查看已配置的厂商和可用模型", "/provider")

@_handler("/provider")
def _cmd_provider(**kwargs: Any) -> str:
    from omniagent.repl.provider_registry import get_configured_providers, PROVIDERS

    configured = get_configured_providers()
    lines = ["已配置的厂商:\n"]

    if configured:
        for p in configured:
            key_mask = p.api_key[:8] + "****" if len(p.api_key) > 8 else "****"
            lines.append(f"  {p.name} ({p.key})")
            lines.append(f"    Key: {key_mask}")
            lines.append(f"    模型: {', '.join(p.models)}")
            lines.append("")
    else:
        lines.append("  (无)")
        lines.append("\n输入 /setup 配置 API Key")

    unconfigured = [p for p in PROVIDERS.values() if p.key not in {c.key for c in configured}]
    if unconfigured:
        lines.append("\n可用但未配置的厂商:")
        for p in unconfigured:
            lines.append(f"  {p.name} — {', '.join(p.models[:3])}...")

    return "\n".join(lines)


# /tools ───────────────────────────────────────────────────

register_command("/tools", "查看所有可用工具类型", "/tools")

@_handler("/tools")
def _cmd_tools(**kwargs: Any) -> str:
    tools_info = [
        ("command", "执行终端命令", "action='dir'"),
        ("write_file", "写入文件", "file_path, content"),
        ("read_file", "读取文件", "file_path"),
        ("edit_file", "精确编辑文件（查找替换）", "file_path, old_text, new_text"),
        ("create_directory", "创建目录", "file_path"),
        ("list_files", "目录遍历（glob 模式）", "file_path, pattern, max_depth"),
        ("search_files", "文件内容搜索", "file_path, search_pattern, file_filter"),
        ("git", "Git 操作", "git_command='status|diff|log|add|commit'"),
        ("web_fetch", "抓取网页内容", "url"),
        ("batch_write", "批量写入多个文件", "files=[{path, content}, ...]"),
        ("batch_edit", "批量编辑多个文件", "edits=[{file_path, old_text, new_text}, ...]"),
        ("code_index", "代码符号搜索（AST 索引）", "search_pattern, file_path"),
        ("ast_analyze", "Python 代码结构分析", "file_path"),
        ("refactor", "重构：重命名/清理导入/分析", "refactor_action, old_name, new_name"),
        ("diff_preview", "预览文件修改 diff", "file_path, old_text, new_text"),
        ("mcp_call", "调用 MCP 外部工具", "tool_name, tool_args"),
        ("github_fetch", "GitHub 仓库操作（列出文件/获取内容/README）", "repo, github_action, github_path, branch"),
    ]

    lines = ["可用工具类型:\n"]
    for name, desc, params in tools_info:
        lines.append(f"  [bold]{name}[/bold] — {desc}")
        lines.append(f"    参数: {params}")
        lines.append("")
    lines.append("工具可在 YAML 工作流中通过 action_type 字段使用。")
    return "\n".join(lines)


# /memory ──────────────────────────────────────────────────

register_command(
    "/memory",
    "管理跨会话记忆",
    "/memory list|search|add|clear [参数]",
)

@_handler("/memory")
def _cmd_memory(*, args: str, session_state: dict[str, Any], **kwargs: Any) -> str:
    from omniagent.repl.memory import MemoryStore

    store = MemoryStore()
    parts = args.split(maxsplit=1) if args.strip() else []
    sub = parts[0].lower() if parts else "list"
    sub_args = parts[1] if len(parts) > 1 else ""

    if sub == "list":
        type_filter = sub_args.strip() if sub_args else None
        memories = store.list_all(type_filter)
        if not memories:
            return "暂无记忆。使用 /memory add <内容> 添加。"

        lines = [f"共 {len(memories)} 条记忆:\n"]
        for m in memories:
            emoji = {"fact": "📌", "project": "📁", "error": "⚠️", "preference": "⭐"}.get(m.type, "📝")
            lines.append(f"  {emoji} [{m.id}] [{m.type}] {m.content[:80]}")
            if m.tags:
                lines.append(f"     标签: {', '.join(m.tags)}")
            lines.append(f"     访问: {m.access_count} 次 | 创建: {m.created_at[:10]}")
        return "\n".join(lines)

    elif sub == "search":
        if not sub_args:
            return "用法: /memory search <关键词>"
        results = store.search(sub_args.strip())
        if not results:
            return f"未找到与 '{sub_args}' 相关的记忆。"

        lines = [f"搜索 '{sub_args}' 找到 {len(results)} 条:\n"]
        for m in results:
            emoji = {"fact": "📌", "project": "📁", "error": "⚠️", "preference": "⭐"}.get(m.type, "📝")
            lines.append(f"  {emoji} [{m.id}] {m.content[:80]}")
        return "\n".join(lines)

    elif sub == "add":
        if not sub_args:
            return "用法: /memory add <记忆内容> [--type fact|project|error|preference] [--tags tag1,tag2]"

        # 解析参数
        text = sub_args
        mem_type = "fact"
        tags = []

        if "--type" in text:
            idx = text.index("--type")
            before = text[:idx].strip()
            after = text[idx + 6:].strip()
            parts2 = after.split(maxsplit=1)
            mem_type = parts2[0] if parts2 else "fact"
            text = before or (parts2[1] if len(parts2) > 1 else "")

        if "--tags" in text:
            idx = text.index("--tags")
            before = text[:idx].strip()
            after = text[idx + 6:].strip()
            parts2 = after.split(maxsplit=1)
            tags = [t.strip() for t in parts2[0].split(",")] if parts2 else []
            text = before or (parts2[1] if len(parts2) > 1 else "")

        if not text.strip():
            return "用法: /memory add <记忆内容>"

        memory = store.add(text.strip(), type=mem_type, tags=tags)
        return f"✅ 已添加记忆 [{memory.id}]: {memory.content[:60]}"

    elif sub == "clear":
        count = store.clear()
        return f"已清空 {count} 条记忆。"

    else:
        return "用法: /memory list|search|add|clear [参数]"


# /shortcut ────────────────────────────────────────────────

register_command(
    "/shortcut",
    "管理自定义快捷指令",
    "/shortcut create|list|run|delete [参数]",
)

# /skill ───────────────────────────────────────────────────

register_command(
    "/skill",
    "管理自定义技能（支持 LLM + 工具组合）",
    "/skill create|list|run|delete [参数]",
)

# ── 从 shortcuts 子模块导入实现并注册 ──
from omniagent.repl.commands.shortcuts import cmd_shortcut as _cmd_shortcut, cmd_skill as _cmd_skill

_HANDLERS["/shortcut"] = _cmd_shortcut
_HANDLERS["/skill"] = _cmd_skill


# /project ──────────────────────────────────────────────────

register_command("/project", "查看/刷新项目上下文", "/project [refresh]")

@_handler("/project")
def _cmd_project(*, args: str, session_state: dict[str, Any], **kwargs: Any) -> str:
    from omniagent.repl.file_links import linkify_file_paths

    repl = session_state.get("_repl")
    if not repl:
        return "❌ 无法访问 REPL 实例。"

    pc = repl.project_ctx

    if args.strip().lower() == "refresh":
        pc.refresh()
        repl._project_injected = False
        return f"✅ 项目上下文已刷新。\n\n{pc.get_summary()}"

    if not pc._initialized:
        pc.detect()

    summary = pc.get_summary()

    tree_preview = ""
    if pc.file_tree:
        tree_lines = pc.file_tree.splitlines()[:30]
        tree_preview = "\n\n[文件树预览]\n" + "\n".join(tree_lines)
        if len(pc.file_tree.splitlines()) > 30:
            tree_preview += f"\n... (共 {len(pc.file_tree.splitlines())} 项)"

    return linkify_file_paths(f"{summary}{tree_preview}")


# /edit ─────────────────────────────────────────────────────

register_command("/edit", "编辑代码文件（支持 LLM 辅助）", "/edit <file_path> [指令]")

@_handler("/edit")
def _cmd_edit(*, args: str, registry: ModelRegistry, **kwargs: Any) -> str:
    from omniagent.repl.code_editor import CodeEditor
    from omniagent.repl.file_links import format_file_link

    parts = args.strip().split(maxsplit=1)
    if not parts:
        return "用法: /edit <file_path> [修改指令]\n\n  /edit app.py  — 交互式查看文件\n  /edit app.py 把所有函数名改为驼峰 — LLM 辅助修改"

    file_path = parts[0]
    instruction = parts[1] if len(parts) > 1 else ""

    if not instruction:
        try:
            content, line_count = CodeEditor.read_file(file_path)
            from rich.syntax import Syntax
            ext = Path(file_path).suffix.lstrip(".")
            console.print(f"\n[bold]{format_file_link(file_path)}[/bold] ({line_count} 行)\n")
            console.print(Syntax(content, ext or "text", theme="monokai", line_numbers=False))
            return ""
        except FileNotFoundError as e:
            return str(e)

    model_ids = registry.get_role_priority("planner")
    if not model_ids:
        return "❌ 未配置模型，无法使用 LLM 辅助编辑。请先 /set_model。"

    return CodeEditor.edit_with_llm(file_path, instruction, model_ids, confirm=True)


# /novel ─────────────────────────────────────────────────────

register_command("/novel", "多小说项目管理", "/novel [init <名称> [类型]|list|switch <名称>|status|delete <名称>]")


@_handler("/novel")
def _cmd_novel(
    *, args: str, registry: ModelRegistry, ctx_mgr: ContextManager,
    session_state: dict[str, Any], **kwargs: Any,
) -> str:
    """多小说项目管理命令。"""
    from omniagent.engine.novel_manager import NovelManager

    parts = args.strip().split(maxsplit=2)
    subcmd = parts[0].lower() if parts else "status"
    subargs = parts[1] if len(parts) > 1 else ""
    subargs2 = parts[2] if len(parts) > 2 else ""

    # 确保 NovelManager 存在
    if not hasattr(session_state.get("_repl", None) or object(), '_novel_manager'):
        pass  # Manager 在 repl 中初始化
    manager = session_state.get("_novel_manager")
    if not manager:
        manager = NovelManager()
        session_state["_novel_manager"] = manager

    if subcmd == "init":
        # /novel init <名称> [类型]
        if not subargs:
            return "用法: /novel init <名称> [类型]\n示例: /novel init 星际迷途 科幻"
        title = subargs
        genre = subargs2
        project = manager.create_novel(title, genre)
        return (
            f"✅ 小说「{title}」已创建\n\n"
            f"  📁 项目目录: .novel/projects/{project.slug}/\n"
            f"  📂 类型: {genre or '未分类'}\n\n"
            f"现在可以在 Novel 模式下直接说「帮我写第一章」来开始创作。\n"
            f"使用 /novel list 查看所有小说。"
        )

    elif subcmd == "list":
        # 列出所有小说
        novels = manager.list_novels()
        if not novels:
            return "还没有创建任何小说。使用 /novel init <名称> 创建。"
        lines = ["📚 小说列表:\n"]
        for n in novels:
            marker = " ← 当前" if n["is_active"] else ""
            lines.append(
                f"  {'▸' if n['is_active'] else '•'} **{n['title']}** "
                f"({n['genre'] or '未分类'}) — "
                f"{n['chapters']} 章, 约 {n['words']} 字{marker}"
            )
        lines.append(f"\n使用 /novel switch <名称> 切换小说")
        return "\n".join(lines)

    elif subcmd == "switch":
        # 切换当前小说
        if not subargs:
            return "用法: /novel switch <名称>"
        # 按标题或 slug 查找
        novels = manager.list_novels()
        target = None
        for n in novels:
            if subargs in n["title"] or subargs == n["slug"]:
                target = n["slug"]
                break
        if not target:
            return f"找不到小说「{subargs}」。使用 /novel list 查看所有小说。"
        project = manager.switch_novel(target)
        if project:
            return f"✅ 已切换到「{project.title}」\n现在可以继续创作这本小说了。"
        return f"切换失败: {subargs}"

    elif subcmd == "status":
        # 显示当前小说状态
        current = manager.get_current()
        if not current:
            novels = manager.list_novels()
            if not novels:
                return "还没有创建任何小说。使用 /novel init <名称> 创建。"
            return "没有活跃的小说。使用 /novel switch <名称> 切换。"

        lines = [f"📖 当前小说: **{current.title}**\n"]
        lines.append(f"  类型: {current.genre or '未分类'}")
        lines.append(f"  创建: {current.created_at}")
        lines.append(f"  更新: {current.updated_at}")
        lines.append(f"  章节: {current.chapter_count()} 个")
        lines.append(f"  字数: 约 {current.total_words()} 字")

        # 角色统计
        if current.characters_path().exists():
            try:
                import json
                chars = json.loads(current.characters_path().read_text(encoding="utf-8"))
                if isinstance(chars, list):
                    lines.append(f"  角色: {len(chars)} 个")
            except Exception:
                pass

        lines.append(f"\n  📁 {current.base_dir}")
        return "\n".join(lines)

    elif subcmd == "delete":
        # 删除小说
        if not subargs:
            return "用法: /novel delete <名称>"
        novels = manager.list_novels()
        target = None
        for n in novels:
            if subargs in n["title"] or subargs == n["slug"]:
                target = n["slug"]
                break
        if not target:
            return f"找不到小说「{subargs}」。"
        title = next((n["title"] for n in novels if n["slug"] == target), target)
        manager.delete_novel(target)
        return f"🗑️ 已删除「{title}」"

    else:
        return (
            "用法: /novel <子命令>\n\n"
            "子命令:\n"
            "  init <名称> [类型] — 创建新小说\n"
            "  list               — 列出所有小说\n"
            "  switch <名称>      — 切换当前小说\n"
            "  status             — 查看当前小说状态\n"
            "  delete <名称>      — 删除小说"
        )


# /cleanup ───────────────────────────────────────────────────

register_command(
    "/cleanup",
    "清理过期会话、运行记录和 checkpoint 备份",
    "/cleanup [stats|dry-run]",
)


@_handler("/cleanup")
def _cmd_cleanup(*, args: str, **kwargs: Any) -> str:
    from omniagent.engine.cleanup import SessionCleaner

    cleaner = SessionCleaner()
    parts = args.strip().split()
    sub = parts[0].lower() if parts else "run"

    if sub == "stats":
        s = cleaner.stats()
        return (
            f"═══ .omniagent 存储统计 ═══\n\n"
            f"  会话: {s['sessions']['count']} 个 ({s['sessions']['size']})\n"
            f"  运行记录: {s['runs']['count']} 个 ({s['runs']['size']})\n"
            f"  Checkpoint: {s['checkpoints']['count']} 个 ({s['checkpoints']['size']})\n"
            f"  总计: {s['total_size']}\n\n"
            f"  保留策略:\n"
            f"    会话: {s['retention']['sessions_days']} 天\n"
            f"    运行记录: {s['retention']['runs_days']} 天\n"
            f"    Checkpoint: {s['retention']['checkpoints_days']} 天"
        )

    if sub == "dry-run":
        stats = cleaner.cleanup(dry_run=True)
        return (
            f"═══ 清理预览 (dry-run) ═══\n\n"
            f"  将删除:\n"
            f"    会话: {stats.sessions_deleted} 个 → 释放 {SessionCleaner._format_bytes(stats.bytes_freed)}\n"
            f"    运行记录: {stats.runs_deleted} 个\n"
            f"    Checkpoint: {stats.checkpoints_deleted} 个\n"
            f"  将保留:\n"
            f"    会话: {stats.sessions_kept} 个"
        )

    # 执行清理
    stats = cleaner.cleanup()
    if stats.sessions_deleted or stats.runs_deleted or stats.checkpoints_deleted:
        return (
            f"✅ 清理完成\n\n"
            f"  删除会话: {stats.sessions_deleted} 个\n"
            f"  删除运行记录: {stats.runs_deleted} 个\n"
            f"  删除 checkpoint: {stats.checkpoints_deleted} 个\n"
            f"  释放空间: {SessionCleaner._format_bytes(stats.bytes_freed)}"
        )
    return "✅ 无需清理，所有数据都在保留期内。"


# /prompt ────────────────────────────────────────────────────

register_command(
    "/prompt",
    "管理系统提示词（主版本、领域提示词、长期记忆）",
    "/prompt status|domains|memories|versions|reload",
)


@_handler("/prompt")
def _cmd_prompt(*, args: str, session_state: dict[str, Any], **kwargs: Any) -> str:
    """管理系统提示词存储。"""
    repl = session_state.get("_repl")
    if not repl or not hasattr(repl, "prompt_store"):
        return "❌ PromptStore 未初始化"

    store = repl.prompt_store
    sub = args.strip().lower()

    if sub == "status":
        store._ensure_loaded()
        master_ver = store._master.metadata.version if store._master else 0
        domain_count = sum(1 for e in store._entries.values() if e.category == "domain")
        memory_count = sum(1 for e in store._entries.values() if e.category == "memory")
        memory_tokens = sum(e.token_estimate for e in store._entries.values() if e.category == "memory")

        return (
            f"═══ 系统提示词状态 ═══\n\n"
            f"  主版本: v{master_ver}\n"
            f"  领域提示词: {domain_count} 个\n"
            f"  长期记忆: {memory_count} 个 ({memory_tokens} tokens)\n"
            f"  项目目录: {store._project_dir}\n"
            f"  用户目录: {store._user_dir}\n\n"
            f"使用 /prompt domains|memories|versions 查看详情"
        )

    elif sub == "domains":
        store._ensure_loaded()
        domains = [e for e in store._entries.values() if e.category == "domain"]
        if not domains:
            return "暂无领域提示词。"
        lines = [f"领域提示词 ({len(domains)} 个):\n"]
        for e in sorted(domains, key=lambda x: x.metadata.domain):
            lines.append(
                f"  [{e.metadata.priority}] {e.metadata.domain} "
                f"({e.token_estimate} tokens) "
                f"tags: {', '.join(e.metadata.tags)}"
            )
        return "\n".join(lines)

    elif sub == "memories":
        memories = store.list_memories()
        if not memories:
            return "暂无长期记忆。Agent 会在发现值得持久化的模式时自动写入。"
        lines = [f"长期记忆 ({len(memories)} 个):\n"]
        for e in memories:
            preview = e.content[:80].replace("\n", " ")
            lines.append(
                f"  [{e.metadata.priority}] {e.metadata.domain} "
                f"(v{e.metadata.version}, {e.token_estimate} tokens)\n"
                f"    {preview}..."
            )
            if e.metadata.tags:
                lines.append(f"    tags: {', '.join(e.metadata.tags)}")
        return "\n".join(lines)

    elif sub == "versions":
        versions = store.list_versions()
        if not versions:
            return "暂无归档版本。使用 /prompt 相关功能修改主提示词后会自动归档。"
        lines = [f"主提示词历史版本 ({len(versions)} 个):\n"]
        for v in versions:
            lines.append(f"  {Path(v['path']).name}: {v['size']} bytes, {v['modified'][:19]}")
        return "\n".join(lines)

    elif sub == "reload":
        store._loaded = False
        store._load_all()
        return (
            f"✅ 已重新加载提示词。\n"
            f"  Master: {'已加载' if store._master else '未找到'}\n"
            f"  Domains: {sum(1 for e in store._entries.values() if e.category == 'domain')} 个\n"
            f"  Memories: {sum(1 for e in store._entries.values() if e.category == 'memory')} 个"
        )

    else:
        return (
            "用法: /prompt <子命令>\n\n"
            "子命令:\n"
            "  status   — 查看 PromptStore 状态（主版本、数量、token 使用）\n"
            "  domains  — 列出所有领域提示词\n"
            "  memories — 列出所有 Agent 长期记忆\n"
            "  versions — 查看主提示词历史版本\n"
            "  reload   — 重新加载提示词文件夹（手动编辑后使用）"
        )
