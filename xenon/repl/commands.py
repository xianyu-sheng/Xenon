"""
Slash Commands — 斜杠命令处理器。

每个命令是一个独立的函数，接收 REPL 上下文并返回要显示的文本。
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.panel import Panel

from xenon.repl.model_registry import BUILTIN_MODES

if TYPE_CHECKING:
    from xenon.repl.model_registry import ModelRegistry
    from xenon.repl.context_manager import ContextManager

console = Console()


def _confirm(prompt: str, default: bool = False) -> bool:
    """破坏性操作确认对话框（P3-Q8 / §8.20.9）。

    - 脚本/测试可设 ``XENON_ASSUME_YES=1`` 跳过确认（非交互 seam）；
    - 非交互环境无 stdin（``EOFError``）时保守取 ``default``（通常取消），
      避免 hang 或崩；
    - 交互环境走 ``rich.prompt.Confirm.ask``。
    """
    if os.environ.get("XENON_ASSUME_YES"):
        return True
    from rich.prompt import Confirm as _Confirm
    try:
        return _Confirm.ask(prompt, default=default)
    except EOFError:
        return default


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
    # P3-Q8 / §8.20.8：dispatch 兜底——任一 handler 抛异常（/code subprocess、
    # /run scheduler、/mcp 网络、/edit LLM）不再冒泡崩 REPL，转为友好错误。
    # ExitSignal 是正常退出意图，必须放行不能吞。
    try:
        return handler(args=args, registry=registry, ctx_mgr=ctx_mgr, session_state=session_state)
    except ExitSignal:
        raise
    except Exception as e:
        return f"❌ 命令执行失败 ({name}): {e}"


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


register_command("/exit", "退出 Xenon", "/exit")
register_command("/quit", "退出 Xenon（别名）", "/quit")
register_command("/bye", "退出 Xenon（别名）", "/bye")


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

register_command(
    "/set_model",
    "交互式选择或配置模型",
    "/set_model [alias] [provider/model_name] [api_key=xxx] [base_url=xxx]",
)

@_handler("/set_model")
def _cmd_set_model(*, args: str, registry: ModelRegistry, **kwargs: Any) -> str:
    from rich.table import Table as _Table
    from rich.prompt import IntPrompt as _IntPrompt

    parts = args.split() if args.strip() else []

    # 有参数 → 旧逻辑
    if len(parts) >= 2:
        alias = parts[0]
        model_id = parts[1]
        extra = {}
        for p in parts[2:]:
            if "=" in p:
                k, v = p.split("=", 1)
                extra[k] = v
        # A11: api_key 不进 argv — 命令行明文 key 已忽略，改走掩码输入（防 ps/历史泄露）
        if "api_key" in extra:
            from rich.prompt import Prompt as _Prompt
            from rich.console import Console as _Console
            console = kwargs.get("console") or _Console()
            if extra.get("api_key"):
                console.print("[dim]检测到命令行明文 api_key，已忽略并改用掩码输入（建议今后用 api_key= 空值触发掩码输入）[/dim]")
            extra["api_key"] = _Prompt.ask("API Key", password=True)
            if not extra["api_key"]:
                return "❌ 未输入 API Key"
        try:
            config = registry.add_model(model_id, alias, **extra)
            return f"✅ 模型已注册: {alias} -> {config.model_id}"
        except Exception as e:
            return f"❌ 注册失败: {e}"

    # 无参数 → 交互式选择
    from xenon.repl.provider_registry import get_configured_providers
    configured = get_configured_providers()
    if not configured:
        return "❌ 尚未配置任何 API Key，请先执行 /setup 配置"

    table = _Table(show_header=True, header_style="bold")
    table.add_column("#", style="cyan", width=4)
    table.add_column("厂商", style="bold")
    table.add_column("模型")
    table.add_column("特点")

    all_models: list[tuple[str, str, str]] = []  # (model_id, short_name, provider_key)
    idx = 1
    for p in configured:
        if not p.models:
            table.add_row("-", p.name, "实时获取失败", p.model_error or "请检查 API Key / 网络 / base_url")
            continue
        for m in p.models:
            model_id = f"{p.key}/{m}"
            hint = _model_hint_local(m)
            table.add_row(str(idx), p.name, m, hint)
            all_models.append((model_id, m, p.key))
            idx += 1

    from rich.console import Console as _Console
    console = kwargs.get("console") or _Console()
    console.print(table)
    console.print()

    if not all_models:
        errors = [f"{p.name}: {p.model_error}" for p in configured if p.model_error]
        detail = "\n".join(errors) if errors else "请检查 API Key、网络或厂商 base_url"
        return f"❌ 未能实时获取任何模型\n{detail}"

    try:
        choice = _IntPrompt.ask(
            "输入模型编号",
            choices=[str(i) for i in range(1, len(all_models) + 1)],
            default="1",
        )
    except (KeyboardInterrupt, EOFError, OSError):
        return "已取消"

    model_id, short_name, provider = all_models[choice - 1]
    alias = short_name.replace(".", "-")

    try:
        config = registry.add_model(model_id, alias)
        return f"✅ 模型已设置: {alias} -> {config.model_id}"
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
    # v0.5.2: 也支持按 model_id 查找（如 custom/glm-5-2-260617）
    for a, m in list(registry.models.items()):
        if m.model_id == alias:
            registry.remove_model(a)
            return f"✅ 模型 '{alias}' 已移除"
    return f"❌ 模型 '{alias}' 不存在"


# /models ──────────────────────────────────────────────────

register_command("/models", "列出所有已注册的模型及其角色分配", "/models")

@_handler("/models")
def _cmd_models(*, registry: ModelRegistry, **kwargs: Any) -> str:
    models = registry.list_models()
    if not models:
        return "暂无已注册模型。使用 /set_model 添加模型。"

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


# /pool ────────────────────────────────────────────────────

register_command("/pool", "查看模型调用池（v0.4.0）", "/pool")

@_handler("/pool")
def _cmd_pool(*, session_state: dict, **kwargs: Any) -> str:
    """v0.4.0: 显示 ModelPool 状态（含 tier 队列分布）."""
    pool = session_state.get("model_pool")
    if not pool or pool.is_empty():
        return "调用池为空。请先通过 /setup 配置模型。"

    lines = ["[bold]模型调用池:[/bold]\n"]

    # Step 10: 按 tier 分组展示
    tier_queues = pool.get_tier_queues() if hasattr(pool, "get_tier_queues") else {}
    tier_names = {5: "旗舰 Q5", 4: "高级 Q4", 3: "标准 Q3", 2: "轻量 Q2", 1: "基础 Q1"}

    for tier in range(5, 0, -1):
        aliases = tier_queues.get(tier, [])
        if not aliases:
            lines.append(f"[dim]  {tier_names[tier]}: (空)[/dim]")
            continue
        lines.append(f"[bold cyan]  {tier_names[tier]}:[/bold cyan]")
        for alias in aliases:
            e = pool.get(alias)
            if not e:
                continue
            h = e.health
            status = "[green]●[/green]" if h.consecutive_failures == 0 else (
                "[red]✕[/red]" if h.circuit_open_until > 0 else "[yellow]◐[/yellow]"
            )
            health_str = f"调用{h.total_calls}次"
            if h.avg_latency > 0:
                health_str += f" 延迟{h.avg_latency:.1f}s"
            lines.append(
                f"    {status} {e.alias} → {e.model_id}  "
                f"(权重={e.weight:.1f} {health_str})"
            )

    return "\n".join(lines)


# /import_models ───────────────────────────────────────────

register_command(
    "/import_models",
    "批量导入模型配置文件(YAML/JSON)到注册表与调用池",
    "/import_models <file> [--no-probe] [--dry-run]",
)


@_handler("/import_models")
def _cmd_import_models(*, args: str, registry: ModelRegistry, session_state: dict, **kwargs: Any) -> str:
    """P1-A: 批量注册模型(discover+probe+事务注册),注册后持久化到 ~/.xenon/models.yaml。"""
    from xenon.repl.batch_register import batch_register

    parts = args.split()
    if not parts or not parts[0]:
        return "用法: /import_models <file> [--no-probe] [--dry-run]"
    path = parts[0]
    no_probe = "--no-probe" in parts
    dry_run = "--dry-run" in parts

    pool = session_state.get("model_pool")
    if pool is None:
        return "❌ 调用池不可用"

    result = batch_register(path, registry, pool, probe=not no_probe, dry_run=dry_run)

    summary = result.summary()
    if not dry_run and (result.registered or result.updated):
        try:
            persist = Path.home() / ".xenon" / "models.yaml"
            registry.save_to_file(persist)
            summary += f"\n💾 已持久化到 {persist}"
        except Exception as e:
            summary += f"\n⚠️  持久化失败: {e}"
    return summary


# /reload_models ───────────────────────────────────────────

register_command(
    "/reload_models",
    "从文件重载模型到调用池(默认 ~/.xenon/models.yaml)",
    "/reload_models [file]",
)


@_handler("/reload_models")
def _cmd_reload_models(*, args: str, registry: ModelRegistry, session_state: dict, **kwargs: Any) -> str:
    """P1-A: 显式热重载(替代文件 watcher,避免 REPL 内竞态)。"""
    path = args.strip() or str(Path.home() / ".xenon" / "models.yaml")
    if not Path(path).exists():
        return f"❌ 文件不存在: {path}"

    pool = session_state.get("model_pool")
    if pool is None:
        return "❌ 调用池不可用"

    registry.load_from_file(path)
    models_cfg = registry.export_config().get("models", {})
    pool.from_config(models_cfg)
    return f"✅ 已从 {path} 重载 {len(models_cfg)} 个模型到调用池"


# /set_profile ─────────────────────────────────────────────

register_command(
    "/set_profile",
    "设置性能偏好(fast|cost|balanced),影响模型调度权重",
    "/set_profile [fast|cost|balanced]",
)


@_handler("/set_profile")
def _cmd_set_profile(*, args: str, session_state: dict, **kwargs: Any) -> str:
    """P2: 切换 _score 权重向量(极速响应/成本优先/均衡)。"""
    profile = args.strip().lower()
    pool = session_state.get("model_pool")
    if pool is None:
        return "❌ 调用池不可用"
    if not profile:
        return (f"当前性能偏好: [bold]{pool.perf_profile}[/bold]\n"
                f"可选: fast(极速) | cost(成本优先) | balanced(均衡)")
    if pool.set_perf_profile(profile):
        return f"✅ 性能偏好已设为: {profile}"
    return f"❌ 无效的偏好 '{profile}',可选: fast | cost | balanced"


# /resume ──────────────────────────────────────────────────

register_command("/resume", "列出 / 恢复保存的会话", "/resume [序号或名称]")

@_handler("/resume")
def _cmd_resume(*, args: str, session_state: dict, **kwargs: Any) -> str:
    """断点恢复：列出所有会话，或按序号/名称加载指定会话。

    用法:
      /resume         列出所有已保存的会话（含自动保存）
      /resume 1       加载第 1 个会话
      /resume my-sess  加载名为 my-sess 的会话
    """
    from xenon.repl.session import list_sessions, load_session, get_session_age

    repl = session_state.get("_repl")
    if not repl:
        return "❌ REPL 实例不可用。"

    arg = args.strip()

    # ── 无参数：列出所有会话 ──
    if not arg:
        sessions = list_sessions()
        if not sessions:
            return "没有已保存的会话。使用 /save <名称> 手动保存，或退出时自动保存。"

        from rich.table import Table
        from rich.console import Console as RichConsole

        table = Table(title="已保存的会话 · 输入 /resume <序号> 恢复",
                      border_style="dim #64748b")
        table.add_column("#", style="bold cyan", width=3, justify="right")
        table.add_column("名称", style="#67e8f9", max_width=32)
        table.add_column("时间", style="dim #94a3b8", width=12)
        table.add_column("消息", justify="right", width=6)
        table.add_column("范式", style="dim", width=10)

        for i, s in enumerate(sessions, 1):
            display_name = s["name"]
            if display_name.startswith("_auto"):
                display_name = "[上次自动保存]"
            # 时间显示为相对时间，回退到绝对时间
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

    # ── 按序号或名称加载 ──
    sessions = list_sessions()
    if arg.isdigit():
        idx = int(arg)
        if idx < 1 or idx > len(sessions):
            return f"❌ 序号 {idx} 超出范围（共 {len(sessions)} 个会话）。"
        name = sessions[idx - 1]["name"]
    else:
        name = arg

    try:
        data = load_session(name)
    except FileNotFoundError:
        return f"❌ 会话 '{name}' 不存在。使用 /resume (无参数) 查看全部。"

    try:
        history = data.get("history", [])
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
        repl.ctx_mgr.replace_working_memory(
            data.get("extra", {}).get("working_memory", {})
        )

        from xenon.engine.context import AgentContext
        restored_context = AgentContext(initial=data.get("context", {}))
        repl.agent_context = restored_context
        repl._session_state["agent_context"] = restored_context

        # 恢复范式
        paradigm = data.get("extra", {}).get("paradigm")
        if paradigm:
            try:
                repl.registry.set_mode(paradigm)
            except ValueError:
                pass

        # 恢复模型池配置
        mc = data.get("model_config", {})
        if mc and repl.model_pool.is_empty():
            repl.model_pool.from_config(mc)

        age = get_session_age(data) or "未知时间"
        msgs = len(history)
        return f"✅ 已恢复会话 ({age}) · {msgs} 条消息 · 范式: {paradigm or 'direct'}"

    except Exception as e:
        return f"❌ 恢复失败: {e}"


# /history ──────────────────────────────────────────────────

register_command("/history", "查看路由调度历史（v0.4.0）", "/history [N]")

@_handler("/history")
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
    "/mode [mode_name]\n可用: " + ", ".join(BUILTIN_MODES.keys()),
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
    if agent_ctx and agent_ctx.to_dict():
        lines.append("\nAgentContext 变量:")
        for k, v in agent_ctx.items():
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
    # P3-Q8 / §8.20.9：清空历史不可逆，加确认（默认 Yes，低摩擦——误触仍可在此取消）。
    if not _confirm("确认清空全部对话历史？", default=True):
        return "已取消"
    ctx_mgr.clear()
    return "✅ 对话历史已清空"


# /save ────────────────────────────────────────────────────

register_command("/save", "保存当前会话", "/save <session_name>")

@_handler("/save")
def _cmd_save(*, args: str, ctx_mgr: ContextManager, session_state: dict, registry: ModelRegistry, **kwargs: Any) -> str:
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

@_handler("/load")
def _cmd_load(*, args: str, ctx_mgr: ContextManager, session_state: dict, registry: ModelRegistry, **kwargs: Any) -> str:
    from xenon.repl.session import load_session
    from xenon.engine.context import AgentContext

    name = args.strip()
    if not name:
        return "用法: /load <session_name>"

    try:
        data = load_session(name)
    except FileNotFoundError as e:
        return f"❌ {e}"

    # P3-Q8 / §8.20.9：加载会话会覆盖当前对话历史（未保存则丢失），加确认。
    if not _confirm(f"加载会话 '{name}' 将覆盖当前对话历史，确认？", default=False):
        return "已取消"

    # 恢复对话历史
    ctx_mgr.save_snapshot()
    ctx_mgr.history.clear()
    for msg in data.get("history", []):
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
    ctx_mgr.replace_working_memory(
        data.get("extra", {}).get("working_memory", {})
    )

    # 恢复 AgentContext
    restored_context = AgentContext(initial=data.get("context", {}))
    session_state["agent_context"] = restored_context
    repl = session_state.get("_repl")
    if repl is not None:
        repl.agent_context = restored_context

    # 恢复模型配置
    if "model_config" in data:
        for alias, mcfg in data.get("model_config", {}).get("models", {}).items():
            registry.add_model(mcfg["model_id"], alias)

    return f"✅ 会话 '{name}' 已加载。消息数: {len(ctx_mgr.history)}"


# /sessions ────────────────────────────────────────────────

register_command("/sessions", "列出所有已保存的会话", "/sessions")

@_handler("/sessions")
def _cmd_sessions(**kwargs: Any) -> str:
    from xenon.repl.session import list_sessions

    sessions = list_sessions()
    if not sessions:
        return "暂无已保存的会话。"

    lines = ["已保存的会话:\n"]
    for s in sessions:
        lines.append(f"  {s['name']:<20} {s['saved_at'][:19]}  ({s['messages']} 条消息)")
    return "\n".join(lines)


# /config ──────────────────────────────────────────────────

register_command("/config", "查看或保存当前配置", "/config [save <path>]")

@_handler("/config")
def _cmd_config(*, args: str, registry: ModelRegistry, **kwargs: Any) -> str:
    parts = args.split()
    if parts and parts[0] == "save":
        path = parts[1] if len(parts) > 1 else "xenon_session.yaml"
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
    from xenon.engine.context import AgentContext
    from xenon.engine.scheduler import DAGScheduler
    from xenon.utils.config_parser import load_yaml, parse_workflow

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


# /ask ─────────────────────────────────────────────────────

register_command(
    "/ask",
    "向指定模型发送单次提问（不进入多轮对话）",
    "/ask <alias> <question>",
)

@_handler("/ask")
def _cmd_ask(*, args: str, registry: ModelRegistry, ctx_mgr: ContextManager, **kwargs: Any) -> str:
    from xenon.utils.llm_client import chat_completion

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
    from xenon.utils.llm_client import chat_completion

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
        # A11: 执行 LLM 生成代码前人机确认，显示完整代码
        from rich.console import Console as _Console
        from rich.syntax import Syntax as _Syntax
        console = kwargs.get("console") or _Console()
        console.print("\n[bold]⚠️ 即将执行 LLM 生成的代码:[/bold]")
        console.print(_Syntax(code, "python", theme="monokai", line_numbers=True))
        if not _confirm("确认执行以上代码？", default=False):
            result_lines.append("⏭️ 已取消执行")
            return "\n".join(result_lines)
        result_lines.append("\n▶️  运行代码...")
        try:
            proc = subprocess.run(
                [sys.executable, str(path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode == 0:
                result_lines.append("✅ 运行成功:")
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


# /thinking ────────────────────────────────────────────────
# v0.5.3: 折叠/展开工具调用推理过程

register_command("/thinking", "切换推理过程显示（折叠/展开）", "/thinking [on|off]")

@_handler("/thinking")
def _cmd_thinking(*, args: str, session_state: dict, **kwargs: Any) -> str:
    repl = session_state.get("_repl")
    if repl:
        if args.strip().lower() == "on":
            repl._show_thinking = True
            return "✅ 推理过程显示已开启（每次都会展示工具调用明细）"
        elif args.strip().lower() == "off":
            repl._show_thinking = False
            return "✅ 推理过程显示已关闭（默认折叠，Ctrl+O 可随时展开）"
        else:
            status = "展开" if repl._show_thinking else "折叠（Ctrl+O 展开）"
            return f"当前推理过程: {status}\n用法: /thinking on 或 /thinking off"
    return "❌ 无法获取 REPL 状态"


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


# /sub-agent ──────────────────────────────────────────────

register_command(
    "/sub-agent",
    "委派子 Agent 执行任务（支持多引擎和并行）",
    "/sub-agent <task> [--engine react|plan_execute|reflection|novel|plan_react|plan_reflection|react_reflection|direct] [--timeout N] [--parallel task1|task2|...]",
)

@_handler("/sub-agent")
def _cmd_sub_agent(*, args: str, session_state: dict, repl=None, **kwargs: Any) -> str:
    """v0.6.1: 显式委派子 Agent 执行任务。

    用法:
      /sub-agent <task>                         # 默认 ReAct 引擎
      /sub-agent <task> --engine plan_execute   # 指定引擎
      /sub-agent <task> --timeout 30            # 30 秒超时
      /sub-agent --parallel taskA|taskB|taskC   # 并行 3 个子任务
    """
    from xenon.engine.react_engine import ReActEngine
    from xenon.engine.context import AgentContext

    if not args or not args.strip():
        return (
            "📋 /sub-agent — 委派子 Agent 执行任务\n\n"
            "用法:\n"
            "  /sub-agent <task>                        默认 ReAct 引擎\n"
            "  /sub-agent <task> --engine plan_execute  指定引擎类型\n"
            "  /sub-agent <task> --timeout 30           设置超时（秒）\n"
            "  /sub-agent --parallel taskA|taskB|taskC  并行执行（最多 10 个）\n\n"
            "引擎类型:\n"
            "  react              思考-行动循环（默认，适合复杂多步任务）\n"
            "  plan_execute       规划-执行（适合多步骤结构化任务）\n"
            "  reflection         反思-修正（适合需要自我审查的任务）\n"
            "  novel              小说创作（适合创意写作）\n"
            "  plan_react         规划+ReAct 组合（先规划再逐步执行）\n"
            "  plan_reflection    规划+反思组合（规划执行后自我审查）\n"
            "  react_reflection   ReAct+反思组合（探索后自我审查）\n"
            "  direct             直答（无工具，适合简单问答）\n\n"
            "示例:\n"
            "  /sub-agent 分析 xenon/nodes/tool_node.py 的代码质量\n"
            "  /sub-agent 给 lsp_provider.py 写单元测试 --engine plan_execute\n"
            '  /sub-agent --parallel "审查repl.py"|"审查commands.py"|"审查react_engine.py"\n'
        )

    # 解析参数
    import shlex
    parts = shlex.split(args)

    engine_type = "react"
    timeout = None
    parallel_tasks = None

    i = 0
    task_parts = []
    while i < len(parts):
        if parts[i] == "--engine" and i + 1 < len(parts):
            engine_type = parts[i + 1].lower()
            i += 2
        elif parts[i] == "--timeout" and i + 1 < len(parts):
            try:
                timeout = int(parts[i + 1])
            except ValueError:
                return f"❌ --timeout 必须为整数，收到: {parts[i + 1]}"
            i += 2
        elif parts[i] == "--parallel":
            if i + 1 < len(parts):
                parallel_tasks = [t.strip() for t in parts[i + 1].split("|") if t.strip()]
            else:
                return "❌ --parallel 需要任务列表（用 | 分隔）"
            i += 2
        else:
            task_parts.append(parts[i])
            i += 1

    task = " ".join(task_parts).strip()

    # 获取模型配置
    if repl is None:
        return "❌ 无法获取 REPL 实例"

    model_ids = [e.model_id for e in (repl.model_pool.get_healthy() or repl.model_pool.list_all())]
    if not model_ids:
        return "❌ 模型池为空，请先运行 /setup 配置模型。"
    model_configs = getattr(repl, '_model_configs', None) or {}

    # 构建引擎
    engine = ReActEngine(
        model_ids,
        max_iterations=15,
        callback=getattr(repl, '_engine_callback', None),
        model_configs=model_configs,
        subagent_timeout=timeout,
    )

    # 构建上下文
    ctx = AgentContext()
    # 复制当前对话历史（最近 10 条）
    try:
        history = repl.ctx_mgr.get_messages()[-10:]
        ctx.set_conversation_messages(list(history))
    except Exception:
        pass

    # 构建 action_input
    if parallel_tasks:
        action_input: dict[str, Any] = {
            "task_list": [
                {"task": t, "engine": "react"} for t in parallel_tasks
            ]
        }
        display_task = f"并行 {len(parallel_tasks)} 个子任务"
    else:
        if not task:
            return "❌ 请提供任务描述"
        action_input = {"task": task, "engine": engine_type}
        if timeout:
            action_input["timeout"] = timeout
        display_task = task

    import logging
    logger = logging.getLogger(__name__)
    logger.info("用户 /sub-agent 委派: %s (引擎=%s)", display_task[:80], engine_type)

    try:
        result = engine._spawn_subagent(action_input, ctx, None)
        return result
    except Exception as e:
        logger.exception("/sub-agent 执行失败")
        return f"❌ 子 Agent 执行失败: {e}"


# /mcp ──────────────────────────────────────────────────

register_command("/mcp", "管理 MCP 服务器连接", "/mcp [add|list|tools|remove|discover|install] [args]")

@_handler("/mcp")
def _cmd_mcp(*, args: str, session_state: dict, **kwargs: Any) -> str:
    repl = session_state.get("_repl")
    if not repl:
        return "❌ 无法获取 REPL 状态"

    from xenon.mcp.registry import MCPRegistry

    parts = args.strip().split()
    sub = parts[0] if parts else ""

    # 无子命令 → 显示使用指南
    if not sub:
        return _MCP_USAGE

    # 确保注册表存在
    if not hasattr(repl, '_mcp_registry') or repl._mcp_registry is None:
        repl._mcp_registry = MCPRegistry()
        repl.agent_context.set("_mcp_registry", repl._mcp_registry)

    registry = repl._mcp_registry

    if sub == "add":
        # v0.5.3: 过滤掉 '--' 分隔符（兼容 claude mcp add ... -- ... 写法）
        clean_parts = [p for p in parts if p != "--"]
        if len(clean_parts) < 3:
            resp = "用法: /mcp add <name> <command_or_url> [args...]"
            if any(p == "--" for p in parts):
                resp += "\n💡 提示: -- 分隔符不是必需的，直接 /mcp add <name> <command> [args...] 即可"
            return resp + "\n示例:\n  /mcp add fs npx -y @modelcontextprotocol/server-filesystem .\n  /mcp add web http://localhost:3000/sse"
        name = clean_parts[1]
        target = clean_parts[2]
        extra_args = clean_parts[3:] if len(clean_parts) > 3 else []

        try:
            if target.startswith("http"):
                registry.add_server(name, url=target)
                # v0.5.3: 持久化
                from xenon.repl.provider_registry import save_mcp_server
                save_mcp_server(name, url=target)
            else:
                registry.add_server(name, command=target, args=extra_args)
                # v0.5.3: 持久化
                from xenon.repl.provider_registry import save_mcp_server
                save_mcp_server(name, command=target, args=extra_args)

            # 发现工具
            tools = registry.discover_tools()
            tool_count = len(tools.get(name, []))
            return f"✅ MCP 服务器 '{name}' 已连接\n发现 {tool_count} 个工具"
        except Exception as e:
            return f"❌ 连接失败: {e}"

    elif sub == "list":
        has_connected = bool(registry.clients)
        has_pending = registry.has_pending_servers()
        if not has_connected and not has_pending:
            return "当前无 MCP 服务器。使用 /mcp add 添加。"
        lines = ["═══ MCP 服务器 ═══\n"]
        for name, client in registry.clients.items():
            info = client.server_info
            tool_count = len(client.tools)
            lines.append(f"  {name}: {info.get('name', 'unknown')} v{info.get('version', '?')} ({tool_count} 工具)")
        if has_pending:
            for name in registry.get_pending_server_names():
                lines.append(f"  {name}: [dim]惰性（首次调用时连接）[/dim]")
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
            # P3-Q8 / §8.20.9：移除 MCP 服务器会断开连接并重建工具映射，加确认。
            if not _confirm(f"移除 MCP 服务器 '{name}'？", default=False):
                return "已取消"
            registry.clients[name].close()
            del registry.clients[name]
            # 重建工具映射
            registry.tool_map.clear()
            registry.discover_tools()
            # v0.5.3: 从持久化配置中移除
            from xenon.repl.provider_registry import remove_mcp_server
            remove_mcp_server(name)
            return f"✅ MCP 服务器 '{name}' 已移除"
        # v0.5.4: 也处理惰性服务器
        if name in registry.get_pending_server_names():
            if not _confirm(f"移除惰性 MCP 服务器 '{name}'（尚未连接）？", default=False):
                return "已取消"
            # 从 pending_configs 中删除
            registry._pending_configs.pop(name, None)
            from xenon.repl.provider_registry import remove_mcp_server
            remove_mcp_server(name)
            return f"✅ MCP 服务器 '{name}' 已移除（惰性）"
        return f"❌ 未找到 MCP 服务器 '{name}'"

    elif sub == "discover":
        keyword = " ".join(parts[1:]) if len(parts) > 1 else ""
        from xenon.repl.library import get_mcp_library
        lib = get_mcp_library()
        results = lib.discover(keyword)
        if not results:
            return f"未找到匹配 '{keyword}' 的 MCP 服务器。\n输入 /mcp discover 浏览全部"
        lines = [f"═══ MCP 库{' — 搜索: ' + keyword if keyword else ''} ═══ [dim]{lib.source_label}[/dim]\n"]
        for s in results:
            env_hint = ""
            if s.env:
                env_vars = ", ".join(s.env.keys())
                env_hint = f"  [dim]需要环境变量: {env_vars}[/dim]"
            cat = f"[{s.category}]" if s.category else ""
            src = ""
            if s.source == "smithery":
                src = " [dim]🔗 远程[/dim]" if s.url else ""
            elif s.source == "github":
                src = " [dim]📦 本地[/dim]" if s.command else ""
            lines.append(f"  {s.name} {cat}{src}")
            lines.append(f"    {s.description[:100]}")
            if s.note:
                lines.append(f"    [dim]💡 {s.note}[/dim]")
            if s.homepage:
                lines.append(f"    [dim]{s.homepage}[/dim]")
            if env_hint:
                lines.append(f"    {env_hint}")
            lines.append(f"    安装: /mcp install {s.name}")
        return "\n".join(lines)

    elif sub == "install":
        if len(parts) < 2:
            return "用法: /mcp install <name>\n\n提示: 先用 /mcp discover 浏览可用 MCP 服务器"
        name = parts[1]
        from xenon.repl.library import get_mcp_library
        lib = get_mcp_library()
        entry = lib.get(name)
        if not entry:
            similar = lib.discover(name)
            hint = ""
            if similar:
                names = ", ".join(s.name for s in similar[:5])
                hint = f"\n\n相似的: {names}"
            return f"❌ 未在库中找到 '{name}'。输入 /mcp discover 浏览全部{hint}"

        # 检查环境变量
        env_warnings = []
        for env_key, env_val in entry.env.items():
            if "<" in env_val or "你的" in env_val or "Token" in env_val:
                import os as _os
                if not _os.environ.get(env_key):
                    env_warnings.append(f"  ⚠️ {env_key} 未设置；设置环境变量后使用 /mcp remove {entry.name} 再重新安装")

        try:
            # 如果条目来自 Smithery 且没有 command/url，查详情接口获取连接信息
            if entry.source == "smithery" and not entry.command and not entry.url:
                from xenon.repl.library import fetch_smithery_detail
                ok, detail = fetch_smithery_detail(entry.name)
                if ok and isinstance(detail, dict):
                    conns = detail.get("connections", [])
                    if conns:
                        first = conns[0]
                        entry.url = first.get("deploymentUrl", "")
                        schema = first.get("configSchema", {})
                        for prop_name, prop in schema.get("properties", {}).items():
                            if prop_name not in entry.env:
                                entry.env[prop_name] = prop.get("description", "")

            # v0.5.4: 惰性连接 — 仅持久化配置，首次调用时再启动子进程
            if entry.command:
                registry.add_server_pending(entry.name, command=entry.command, args=entry.args)
                from xenon.repl.provider_registry import save_mcp_server
                save_mcp_server(entry.name, command=entry.command, args=entry.args)
            elif entry.url:
                registry.add_server_pending(entry.name, url=entry.url)
                from xenon.repl.provider_registry import save_mcp_server
                save_mcp_server(entry.name, url=entry.url)
            else:
                return f"❌ '{entry.name}' 没有可执行的命令配置"

            msg = f"✅ MCP 服务器 '{entry.name}' 已登记（按需连接）\n"
            msg += f"   {entry.description[:80]}\n"
            msg += "   下次启动或首次调用时自动连接"
            if env_warnings:
                msg += "\n\n" + "\n".join(env_warnings)
            return msg
        except Exception as e:
            return f"❌ 安装失败: {e}"

    else:
        # 无子命令或无效子命令 → 显示完整使用指南
        return _MCP_USAGE


_MCP_USAGE = """\
═══ MCP 使用指南 ═══

📡 浏览云端 MCP 库（7000+ 服务器）：
  /mcp discover              浏览全部
  /mcp discover <关键词>      搜索（如: 搜索 / 数据库 / github）

📥 安装 MCP 服务器：
  /mcp install <名称>         从库安装（惰性，按需连接）
  /mcp add <名称> <命令>      手动安装本地 MCP

📋 管理已安装的 MCP：
  /mcp list                   查看已安装列表
  /mcp tools                  查看已发现工具
  /mcp remove <名称>          移除

🔄 其他：
  /library refresh            强制刷新库缓存

示例：
  /mcp discover 浏览器        → 搜索浏览器相关 MCP
  /mcp install playwright     → 安装 Playwright 浏览器自动化
  /mcp install vercel/grep    → 安装 Smithery 远程服务器"""


# /library ───────────────────────────────────────────────

register_command("/library", "刷新 MCP/Skill 库缓存", "/library refresh")

@_handler("/library")
def _cmd_library(*, args: str, **kwargs: Any) -> str:
    """强制刷新库缓存，从 GitHub 重新拉取。"""
    parts = args.strip().split()
    sub = parts[0] if parts else "refresh"

    if sub in ("refresh", "update"):
        from xenon.repl.library import get_mcp_library, get_skill_library

        lines = ["📚 库刷新结果:\n"]

        # 删除缓存，强制重新拉取
        try:
            from xenon.repl.library import _CACHE_MCP, _CACHE_SKILL
            for p in [_CACHE_MCP, _CACHE_SKILL]:
                if p.exists():
                    p.unlink()
        except Exception:
            pass

        mcp_lib = get_mcp_library(force_refresh=True)
        count_mcp = len(mcp_lib.discover())
        lines.append(f"  MCP:  {count_mcp} 个服务器  [dim]{mcp_lib.source_label}[/dim]")
        if mcp_lib._error:
            lines.append(f"    [dim]⚠️ {mcp_lib._error}[/dim]")

        skill_lib = get_skill_library(force_refresh=True)
        count_skill = len(skill_lib.discover())
        lines.append(f"  Skill: {count_skill} 个  [dim]{skill_lib.source_label}[/dim]")
        if skill_lib._error:
            lines.append(f"    [dim]⚠️ {skill_lib._error}[/dim]")

        return "\n".join(lines)
    else:
        return "用法: /library refresh （清除缓存并从 GitHub 拉取最新库）"


# /skill discover / install ──────────────────────────────

register_command("/skill-discover", "浏览/搜索 Skill 库", "/skill-discover [keyword]")
register_command("/skill-install", "安装 Skill", "/skill-install <name>")


@_handler("/skill-discover")
def _cmd_skill_discover(*, args: str, **kwargs: Any) -> str:
    keyword = args.strip()
    from xenon.repl.library import get_skill_library
    lib = get_skill_library()
    results = lib.discover(keyword)
    if not results:
        return f"未找到匹配 '{keyword}' 的 Skill。\n输入 /skill-discover 浏览全部"
    lines = [f"═══ Skill 库{' — 搜索: ' + keyword if keyword else ''} ═══ [dim]{lib.source_label}[/dim]\n"]
    for s in results:
        cat = f"[{s.category}]" if s.category else ""
        step_count = len(s.steps) if s.steps else 0
        lines.append(f"  {s.name} {cat} ({step_count} 步)")
        lines.append(f"    {s.description[:120]}")
        lines.append(f"    安装: /skill-install {s.name}")
    if not keyword:
        lines.append("\n[dim]💡 想贡献你的 Skill？欢迎 PR → https://github.com/xianyu-sheng/Xenon[/dim]")
    return "\n".join(lines)


@_handler("/skill-install")
def _cmd_skill_install(*, args: str, **kwargs: Any) -> str:
    name = args.strip()
    if not name:
        return "用法: /skill-install <name>\n\n提示: 先用 /skill-discover 浏览可用 Skill"
    from xenon.repl.library import get_skill_library
    lib = get_skill_library()
    ok, msg = lib.install(name)
    if not ok:
        similar = lib.discover(name)
        hint = ""
        if similar:
            names = ", ".join(s.name for s in similar[:5])
            hint = f"\n\n相似的: {names}"
        return f"❌ {msg}{hint}"
    # 刷新 REPL 已缓存的 skill 列表
    lib.refresh_repl_skills()
    return msg + f"\n输入 /{name} 使用"


# /status ──────────────────────────────────────────────────

register_command("/status", "显示详细状态信息", "/status")

@_handler("/status")
def _cmd_status(*, ctx_mgr: ContextManager, registry: ModelRegistry, session_state: dict, **kwargs: Any) -> str:

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


# /setup ───────────────────────────────────────────────────

register_command("/setup", "首次配置向导（配置 Key、选模型、选范式）", "/setup")

@_handler("/setup")
def _cmd_setup(*, session_state: dict, **kwargs: Any) -> str:
    from xenon.repl.setup_wizard import interactive_setup

    repl = session_state.get("_repl")
    if repl:
        interactive_setup(repl.registry, model_pool=repl.model_pool)
        return ""
    return "❌ 无法获取 REPL 状态"


# /model ───────────────────────────────────────────────────

# v0.4.0 removed: register_command("/model", "交互式切换模型", "/model")

@_handler("/model")
def _cmd_model(*, session_state: dict, registry: ModelRegistry, **kwargs: Any) -> str:
    from rich.table import Table as _Table
    from rich.prompt import IntPrompt as _IntPrompt
    from rich.console import Console as _Console

    models = registry.list_models()
    if not models:
        return "暂无已注册模型。请先执行 /set_model 注册模型。"

    console = kwargs.get("console") or _Console()
    current_aliases = registry.role_priority.get("planner", [])

    table = _Table(show_header=True, header_style="bold")
    table.add_column("#", style="cyan", width=4)
    table.add_column("别名", style="bold")
    table.add_column("模型 ID")
    table.add_column("状态")

    for i, m in enumerate(models, 1):
        status = "[green]当前[/green]" if m.alias in current_aliases else ""
        table.add_row(str(i), m.alias, m.model_id, status)

    console.print(table)
    console.print()

    try:
        choice = int(_IntPrompt.ask(
            "输入编号切换模型",
            choices=[str(i) for i in range(1, len(models) + 1)],
            default="1",
        ))
    except (KeyboardInterrupt, EOFError, OSError):
        return "已取消"

    selected = models[choice - 1]
    registry.role_priority["planner"] = [selected.alias]
    # v0.5.2: 清除该模型的失败标记，允许重新调用
    repl = session_state.get("_repl")
    if repl and hasattr(repl, "_failed_models"):
        repl._failed_models.discard(selected.model_id)
    return f"✅ 已切换到: {selected.alias} ({selected.model_id})"


# /provider ────────────────────────────────────────────────

register_command("/provider", "查看已配置的厂商和可用模型", "/provider")

@_handler("/provider")
def _cmd_provider(**kwargs: Any) -> str:
    from xenon.repl.provider_registry import get_configured_providers, PROVIDERS

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
        ("clone_repo", "克隆 GitHub 仓库到本地并分析代码结构", "repo, branch"),
        ("lsp_goto_def", "跳转到 Python 符号定义（跨文件）", "file_path, line, column"),
        ("lsp_find_refs", "查找 Python 符号的所有引用", "file_path, line, column"),
        ("lsp_hover", "获取 Python 符号的类型和文档", "file_path, line, column"),
        ("lsp_diagnostics", "检查 Python 文件语法错误", "file_path"),
        ("lsp_symbols", "列出 Python 文件所有符号", "file_path"),
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
    from xenon.repl.memory import MemoryStore

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

@_handler("/shortcut")
def _cmd_shortcut(*, args: str, registry: ModelRegistry, session_state: dict[str, Any], **kwargs: Any) -> str:
    from xenon.repl.shortcut_manager import ShortcutManager

    manager = ShortcutManager()
    parts = args.split(maxsplit=1) if args.strip() else []
    sub = parts[0].lower() if parts else "list"
    sub_args = parts[1] if len(parts) > 1 else ""

    if sub == "list":
        shortcuts = manager.list_all()
        if not shortcuts:
            return "暂无快捷指令。使用 /shortcut create 创建。"

        lines = [f"共 {len(shortcuts)} 个快捷指令:\n"]
        for s in shortcuts:
            lines.append(f"  /{s.name} — {s.description}")
            for i, step in enumerate(s.steps, 1):
                lines.append(f"    {i}. {step}")
        return "\n".join(lines)

    elif sub == "create":
        return _shortcut_create_interactive(manager, registry=registry)

    elif sub == "run":
        if not sub_args:
            return "用法: /shortcut run <name> [参数]"
        parts2 = sub_args.split(maxsplit=1)
        name = parts2[0]
        run_args = parts2[1] if len(parts2) > 1 else ""
        # P3-Q8 / §8.20.2/9：快捷指令可能含 LLM 生成的 shell 命令，运行前展示步骤并确认。
        sc = manager.get(name)
        if sc is None:
            return f"❌ 未找到快捷指令 '{name}'"
        steps_preview = "\n".join(f"  {i}. {s}" for i, s in enumerate(sc.steps, 1))
        console.print(Panel(steps_preview or "  (无步骤)", title=f"快捷指令 '{name}' 将执行"))
        if not _confirm(
            f"运行快捷指令 '{name}'（将执行以上 {len(sc.steps)} 步命令）？", default=False
        ):
            return "已取消"
        return manager.execute(name, run_args)

    elif sub == "delete":
        if not sub_args:
            return "用法: /shortcut delete <name>"
        if manager.remove(sub_args.strip()):
            return f"✅ 已删除快捷指令: {sub_args.strip()}"
        return f"❌ 快捷指令 /{sub_args.strip()} 不存在"

    else:
        return "用法: /shortcut create|list|run|delete [参数]"


def _shortcut_create_interactive(manager, registry=None) -> str:
    """交互式创建快捷指令。支持智能生成和手动配置。"""
    from rich.prompt import Prompt as _Prompt

    console.print("\n[bold cyan]创建快捷指令[/bold cyan]\n")

    name = _Prompt.ask("指令名称（不含 /）")
    description = _Prompt.ask("指令描述（一句话说明用途）")

    # 选择创建模式
    console.print("\n[dim]创建模式:[/dim]")
    console.print("  [bold]1[/bold]. 🤖 智能生成 — 只需描述，Agent 自动生成命令（推荐）")
    console.print("  [bold]2[/bold]. ✏️  手动配置 — 逐行输入命令")

    mode = _Prompt.ask("选择模式", choices=["1", "2"], default="1")

    if mode == "1":
        return _shortcut_auto_generate(name, description, manager, registry)
    else:
        return _shortcut_manual_create(name, description, manager)


def _shortcut_auto_generate(name: str, description: str, manager, registry=None) -> str:
    """智能生成快捷指令命令。"""
    from rich.panel import Panel
    from rich.prompt import Prompt as _Prompt

    console.print("\n[dim]🤖 正在根据你的描述生成命令...[/dim]\n")

    steps = _generate_shortcut_steps(description, registry)

    if not steps:
        console.print("[yellow]⚠️  自动生成失败，切换到手动模式。[/yellow]")
        return _shortcut_manual_create(name, description, manager)

    # 展示预览
    preview_lines = []
    for i, step in enumerate(steps, 1):
        preview_lines.append(f"  [bold]{i}.[/bold] [cyan]{step}[/cyan]")
    preview = "\n".join(preview_lines)

    console.print(Panel(
        preview,
        title="[bold green]✅ 自动生成的快捷指令[/bold green]",
        border_style="green",
        padding=(1, 2),
    ))

    console.print("\n[dim]👆 以上是 Agent 根据你的描述自动生成的命令。[/dim]\n")

    action = _Prompt.ask("操作", choices=["ok", "edit", "cancel"], default="ok")

    if action == "cancel":
        return "❌ 已取消创建。"

    if action == "edit":
        return _shortcut_manual_create(name, description, manager, pre_steps=steps)

    shortcut = manager.create(name, description, steps)
    return f"✅ 快捷指令 /{shortcut.name} 已创建！使用 /{shortcut.name} 执行。"


def _generate_shortcut_steps(description: str, registry=None) -> list[str]:
    """用 LLM 根据描述生成快捷指令命令。"""
    try:
        from xenon.utils.llm_client import chat_completion

        model_ids = registry.get_role_priority("planner") if registry else []
        if not model_ids:
            return []

        import sys
        if sys.platform == "win32":
            shell_hint = "Windows PowerShell"
            example = '["Write-Host \'hello\'", "Get-ChildItem"]'
        else:
            shell_hint = "Linux bash / macOS zsh"
            example = '["echo \'hello\'", "ls -la"]'

        prompt = f"""根据以下描述，生成一组 shell 命令（{shell_hint} 兼容）。

描述: {description}

要求:
- 返回 JSON 数组，每个元素是一条 shell 命令
- 命令要实用、安全
- 只返回 JSON 数组，不要其他内容

示例: {example}"""

        messages = [
            {"role": "system", "content": "你是一个命令生成器。根据用户描述生成 shell 命令数组。只返回 JSON 数组。"},
            {"role": "user", "content": prompt},
        ]

        for model_id in model_ids:
            try:
                response = chat_completion(model_id, messages, max_tokens=500, temperature=0.3)
                return _parse_shortcut_steps(response)
            except Exception:
                continue

        return []

    except Exception:
        return []


def _parse_shortcut_steps(response: str) -> list[str]:
    """解析 LLM 返回的命令数组。"""
    import json

    text = response.strip()

    if "```json" in text:
        start = text.find("```json") + 7
        end = text.find("```", start)
        if end != -1:
            text = text[start:end].strip()
    elif "```" in text:
        start = text.find("```") + 3
        end = text.find("```", start)
        if end != -1:
            text = text[start:end].strip()

    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [str(cmd) for cmd in data if cmd]
    except json.JSONDecodeError:
        pass

    return []


def _shortcut_manual_create(name: str, description: str, manager, pre_steps=None) -> str:
    """手动配置快捷指令。"""
    from rich.prompt import Prompt as _Prompt

    if pre_steps:
        console.print(f"\n[dim]已有 {len(pre_steps)} 条生成的命令，继续添加。[/dim]")
        steps = list(pre_steps)
    else:
        steps_str = _Prompt.ask("执行步骤（每行一个命令，输入 END 结束）")
        steps = []
        if steps_str.strip().upper() != "END":
            steps.append(steps_str)
            while True:
                line = _Prompt.ask("下一步", default="END")
                if line.strip().upper() == "END":
                    break
                steps.append(line)

    if not steps:
        return "❌ 至少需要一个步骤"

    shortcut = manager.create(name, description, steps)
    return f"✅ 快捷指令 /{shortcut.name} 已创建！使用 /{shortcut.name} 执行。"


# /skill ───────────────────────────────────────────────────

register_command(
    "/skill",
    "管理自定义技能（支持 LLM + 工具组合）",
    "/skill create|list|run|delete|import [参数]",
)

# v0.5.4: 模糊子命令匹配 —— "creat"/"lst"/"del" 等 typo 自动纠正
_SKILL_FUZZY: dict[str, str] = {}
_FUZZY_ALIASES = {
    "create": ["creat", "crate", "creaet", "add", "new", "mk"],
    "list": ["ls", "lst", "show", "all"],
    "delete": ["del", "rm", "remove", "delet"],
    "run": ["exec", "execute", "start"],
    "import": ["install", "get", "fetch", "clone", "load"],
    "reload": ["refresh", "rescan"],
}
for _canonical, _aliases in _FUZZY_ALIASES.items():
    for _a in _aliases:
        _SKILL_FUZZY[_a] = _canonical


def _fuzzy_match_subcommand(sub: str) -> str | None:
    """模糊匹配子命令名，返回规范名或 None。"""
    import difflib
    canonical = list(_FUZZY_ALIASES.keys())
    # 精确别名匹配
    if sub in _SKILL_FUZZY:
        return _SKILL_FUZZY[sub]
    # difflib 模糊匹配（截断到 1 的阈值）
    matches = difflib.get_close_matches(sub, canonical, n=1, cutoff=0.6)
    return matches[0] if matches else None


@_handler("/skill")
def _cmd_skill(*, args: str, registry: ModelRegistry, session_state: dict[str, Any], **kwargs: Any) -> str:
    from xenon.repl.skill_manager import SkillManager

    manager = SkillManager()
    parts = args.split(maxsplit=1) if args.strip() else []
    sub = parts[0].lower() if parts else "list"
    sub_args = parts[1] if len(parts) > 1 else ""

    # v0.5.4: 模糊匹配纠正 typo
    canonical = sub
    if sub not in {"list", "create", "run", "delete", "import", "reload"}:
        matched = _fuzzy_match_subcommand(sub)
        if matched:
            canonical = matched

    if canonical == "list":
        skills = manager.list_all()

        # 已安装的技能
        installed = ""
        if skills:
            lines = [f"═══ 已安装技能（{len(skills)} 个）═══\n"]
            for s in skills:
                type_counts: dict[str, int] = {}
                for st in s.steps:
                    type_counts[st.type] = type_counts.get(st.type, 0) + 1
                step_summary = ", ".join(f"{n}×{t}" for t, n in sorted(type_counts.items()))
                lines.append(f"  /{s.name} — {s.description}")
                lines.append(f"    {len(s.steps)} 步 ({step_summary})")
            installed = "\n".join(lines) + "\n"
        else:
            installed = "暂无已安装技能。\n"

        # 库浏览指引
        library_guide = """\
📡 浏览云端 Skill 库：
  /skill-discover              浏览全部
  /skill-discover <关键词>      搜索

📥 安装 Skill：
  /skill-install <名称>         一键安装
  /skill import <GitHub URL>   从 URL 导入

🛠 其他：
  /skill create                交互式创建
  /skill delete <名称>          删除
  /skill reload                从磁盘重新加载
"""
        return installed + library_guide

    elif canonical == "create":
        return _skill_create_interactive(manager, registry=registry)

    elif canonical == "run":
        if not sub_args:
            return "用法: /skill run <name> [参数]"
        parts2 = sub_args.split(maxsplit=1)
        name = parts2[0]
        run_args = parts2[1] if len(parts2) > 1 else ""
        model_ids = registry.get_role_priority("planner")
        return manager.execute(name, run_args, model_priority=model_ids)

    elif canonical == "delete":
        if not sub_args:
            return "用法: /skill delete <name>"
        if manager.remove(sub_args.strip()):
            return f"✅ 已删除技能: {sub_args.strip()}"
        return f"❌ 技能 /{sub_args.strip()} 不存在"

    elif canonical == "import":
        if not sub_args:
            return "用法: /skill import <github-url>"
        return _skill_import_from_url(manager, sub_args.strip())

    elif canonical == "reload":
        manager.load()
        skills = manager.list_all()
        return f"✅ 已从磁盘重新加载 {len(skills)} 个技能"

    else:
        # v0.5.4: 自然语言技能创建 —— 仅当 args 包含实质性描述时才触发。
        # 单个 typo 词（如 /skill xyz）显示帮助而非静默创建 skill。
        # 阈值：args 总长度 > 15 字符或包含中文（说明用户在描述需求）。
        full_args = args.strip()
        has_chinese = any('一' <= c <= '鿿' for c in full_args)
        if len(full_args) > 15 or has_chinese:
            name = _extract_skill_name(sub, sub_args)
            return _skill_auto_generate(name, args, manager, registry, interactive=False)
        else:
            # sub 可能是 typo — 显示帮助并给出模糊匹配建议
            hint = ""
            matched = _fuzzy_match_subcommand(sub)
            if matched:
                hint = f"\n\n💡 你是不是想用 [bold]/skill {matched}[/bold]？"
            return (
                f"无法识别的子命令: [bold]{sub}[/bold]{hint}\n\n"
                f"用法: /skill [list|create|run|delete|import|reload]\n\n"
                f"📡 浏览云端库: /skill-discover | /skill-install <名称>\n"
                f"💡 自然语言创建: /skill 帮我设计前端页面的技能"
            )


def _skill_create_interactive(manager, registry=None) -> str:
    """交互式创建技能。支持智能生成和手动配置两种模式。"""
    from rich.prompt import Prompt as _Prompt

    console.print("\n[bold cyan]创建技能[/bold cyan]\n")

    name = _Prompt.ask("技能名称（不含 /）")
    description = _Prompt.ask("技能描述（用一句话说明这个技能做什么）")

    # 选择创建模式
    console.print("\n[dim]创建模式:[/dim]")
    console.print("  [bold]1[/bold]. 🤖 智能生成 — 只需描述，Agent 自动生成步骤（推荐）")
    console.print("  [bold]2[/bold]. ✏️  手动配置 — 逐步骤手动添加")

    mode = _Prompt.ask("选择模式", choices=["1", "2"], default="1")

    if mode == "1":
        return _skill_auto_generate(name, description, manager, registry)
    else:
        return _skill_manual_create(name, description, manager)


def _skill_auto_generate(name: str, description: str, manager, registry=None, *, interactive: bool = True) -> str:
    """智能生成技能步骤。

    Args:
        name: 技能名称
        description: 技能描述
        manager: SkillManager 实例
        registry: ModelRegistry 实例
        interactive: True 时展示生成结果并让用户确认/编辑/取消；
                     False 时直接保存（自然语言快速创建）。
    """
    from rich.panel import Panel
    from rich.prompt import Prompt as _Prompt

    console.print("\n[dim]🤖 正在根据你的描述生成技能步骤...[/dim]\n")

    # 用 LLM 生成步骤
    steps, system_prompt = _generate_skill_steps(description, registry)

    if not steps:
        if interactive:
            console.print("[yellow]⚠️  自动生成失败，切换到手动模式。[/yellow]")
            return _skill_manual_create(name, description, manager)
        else:
            console.print("[yellow]⚠️  自动生成失败，使用默认步骤。[/yellow]")
            steps = _fallback_skill_steps(description)
            system_prompt = ""

    # 展示生成结果供用户学习
    console.print(Panel(
        _format_skill_preview(steps, system_prompt),
        title="[bold green]✅ 自动生成的技能[/bold green]",
        border_style="green",
        padding=(1, 2),
    ))

    if interactive:
        console.print("\n[dim]👆 以上是 Agent 根据你的描述自动生成的步骤。[/dim]")
        console.print("[dim]   你可以直接使用，也可以在此基础上修改。[/dim]\n")

        action = _Prompt.ask(
            "操作",
            choices=["ok", "edit", "cancel"],
            default="ok",
        )

        if action == "cancel":
            return "❌ 已取消创建。"

        if action == "edit":
            return _skill_manual_create(name, description, manager, pre_steps=steps, pre_system_prompt=system_prompt)

    # 直接保存
    skill = manager.create(name, description, steps, system_prompt=system_prompt)
    _register_skill_handler(skill, manager)
    return f"✅ 技能 /{skill.name} 已创建！使用 /{skill.name} 或 /skill run {skill.name} 执行。"


def _generate_skill_steps(description: str, registry=None) -> tuple[list[dict], str]:
    """用 LLM 根据描述生成技能步骤。"""
    try:
        from xenon.utils.llm_client import chat_completion

        model_ids = registry.get_role_priority("planner") if registry else []
        if not model_ids:
            return _fallback_skill_steps(description), ""

        prompt = f"""根据以下技能描述，生成对应的执行步骤。

技能描述: {description}

请返回 JSON 格式，包含两个字段:
1. "system_prompt": 系统提示词（字符串，可为空字符串）
2. "steps": 步骤数组，每个步骤是一个对象，包含:
   - "type": "llm" | "command" | "echo" | "write_file" | "read_file"
   - 对于 llm: "prompt" (提示词，可用 {{变量名}} 引用输入)
   - 对于 command: "action" (shell 命令)
   - 对于 echo: "prompt" (输出内容)
   - 对于 write_file: "file_path", "content"
   - 对于 read_file: "file_path"
   - 可选 "output_var": 输出变量名（用于步骤间传递数据）

注意:
- 如果用户输入是 {{input}}，在需要用户输入的地方使用 {{input}}
- 步骤要实用、可执行
- 只返回 JSON，不要其他内容

示例:
{{"system_prompt": "你是一个代码审查专家", "steps": [{{"type": "llm", "prompt": "请审查以下代码:\\n{{input}}", "output_var": "review"}}]}}"""

        messages = [
            {"role": "system", "content": "你是一个技能配置生成器。根据用户描述生成可执行的技能步骤配置。只返回 JSON。"},
            {"role": "user", "content": prompt},
        ]

        for model_id in model_ids:
            try:
                response = chat_completion(model_id, messages, max_tokens=1000, temperature=0.3)
                return _parse_skill_steps(response)
            except Exception:
                continue

        return _fallback_skill_steps(description), ""

    except Exception:
        return _fallback_skill_steps(description), ""


def _parse_skill_steps(response: str) -> tuple[list[dict], str]:
    """解析 LLM 返回的技能步骤 JSON。"""
    import json

    text = response.strip()

    # 提取 JSON
    if "```json" in text:
        start = text.find("```json") + 7
        end = text.find("```", start)
        if end != -1:
            text = text[start:end].strip()
    elif "```" in text:
        start = text.find("```") + 3
        end = text.find("```", start)
        if end != -1:
            text = text[start:end].strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 尝试找 JSON 对象
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start != -1 and brace_end != -1:
            try:
                data = json.loads(text[brace_start:brace_end + 1])
            except json.JSONDecodeError:
                return [], ""
        else:
            return [], ""

    steps = data.get("steps", [])
    system_prompt = data.get("system_prompt", "")

    # 验证步骤格式
    valid_steps = []
    for step in steps:
        if not isinstance(step, dict) or "type" not in step:
            continue
        if step["type"] not in ("llm", "command", "echo", "write_file", "read_file"):
            continue
        valid_steps.append(step)

    return valid_steps, system_prompt


def _fallback_skill_steps(description: str) -> list[dict]:
    """LLM 不可用时的默认步骤。"""
    return [
        {"type": "llm", "prompt": f"根据以下需求执行操作:\n{{input}}\n\n需求: {description}", "output_var": "result"},
    ]


def _format_skill_preview(steps: list[dict], system_prompt: str) -> str:
    """格式化技能预览。"""
    lines = []
    if system_prompt:
        lines.append(f"[bold]系统提示词:[/bold] {system_prompt}\n")

    for i, step in enumerate(steps, 1):
        stype = step.get("type", "?")
        icons = {"llm": "🧠", "command": "⚡", "echo": "📢", "write_file": "📝", "read_file": "📖"}
        icon = icons.get(stype, "❓")

        if stype == "llm":
            prompt_preview = step.get("prompt", "")[:80]
            lines.append(f"  {icon} 步骤 {i} [cyan]LLM[/cyan]: {prompt_preview}")
        elif stype == "command":
            lines.append(f"  {icon} 步骤 {i} [yellow]命令[/yellow]: {step.get('action', '')}")
        elif stype == "echo":
            lines.append(f"  {icon} 步骤 {i} [green]输出[/green]: {step.get('prompt', '')[:60]}")
        elif stype == "write_file":
            lines.append(f"  {icon} 步骤 {i} [magenta]写文件[/magenta]: {step.get('file_path', '')}")
        elif stype == "read_file":
            lines.append(f"  {icon} 步骤 {i} [blue]读文件[/blue]: {step.get('file_path', '')}")

        if step.get("output_var"):
            lines.append(f"       → 输出到: [dim]{step['output_var']}[/dim]")

    return "\n".join(lines)


def _skill_manual_create(name: str, description: str, manager, pre_steps=None, pre_system_prompt="") -> str:
    """手动配置技能步骤。"""
    from rich.prompt import Prompt as _Prompt

    system_prompt = _Prompt.ask("系统提示词（可选）", default=pre_system_prompt or "")

    if pre_steps:
        console.print(f"\n[dim]已有 {len(pre_steps)} 个生成的步骤，继续添加更多步骤。[/dim]")
        steps = list(pre_steps)
    else:
        console.print("\n添加步骤（支持类型: llm, command, echo, write_file, read_file）")
        steps = []

    while True:
        console.print(f"\n[dim]步骤 {len(steps) + 1}[/dim]")
        step_type = _Prompt.ask("  类型", choices=["llm", "command", "echo", "write_file", "read_file", "done"])
        if step_type == "done":
            break

        step: dict[str, str] = {"type": step_type}

        if step_type == "llm":
            step["prompt"] = _Prompt.ask("  提示词（可用 {变量名}）")
        elif step_type == "command":
            step["action"] = _Prompt.ask("  命令")
        elif step_type == "echo":
            step["prompt"] = _Prompt.ask("  输出内容")
        elif step_type == "write_file":
            step["file_path"] = _Prompt.ask("  文件路径")
            step["content"] = _Prompt.ask("  文件内容")
        elif step_type == "read_file":
            step["file_path"] = _Prompt.ask("  文件路径")

        output_var = _Prompt.ask("  输出变量名（可选）", default="")
        if output_var:
            step["output_var"] = output_var

        steps.append(step)

    if not steps:
        return "❌ 至少需要一个步骤"

    skill = manager.create(name, description, steps, system_prompt=system_prompt)
    _register_skill_handler(skill, manager)
    return f"✅ 技能 /{skill.name} 已创建！使用 /{skill.name} 或 /skill run {skill.name} 执行。"


# ── skill 辅助函数 ─────────────────────────────────────────


def _register_skill_handler(skill, manager) -> None:
    """v0.5.4: 动态注册 skill 为命令处理器，无需重启即可用 /<name> 调用。"""
    cmd_name = f"/{skill.name}"
    if cmd_name not in _HANDLERS:
        def make_handler(sk_name):
            def handler(*, args: str, registry, **kw: Any) -> str:
                model_ids = registry.get_role_priority("planner")
                return manager.execute(sk_name, args, model_priority=model_ids)
            return handler
        _HANDLERS[cmd_name] = make_handler(skill.name)
        register_command(cmd_name, f"[技能] {skill.description}", cmd_name)


def _extract_skill_name(sub: str, sub_args: str) -> str:
    """v0.5.4: 从自然语言输入中提取 skill 名称。

    优先级: 1) sub_args 中的英文标识符  2) "创建xxx skill" 模式
    3) sub 本身（如果是有效英文名）  4) 自动生成
    失败时返回 'my-skill'。
    """
    import re

    combined = f"{sub} {sub_args}".strip() if sub_args else sub

    # 1) 尝试从 sub_args 中提取英文标识符（优先 sub_args 因为 sub 可能是 typo）
    if sub_args:
        # "创建/设计 xxx skill" → xxx
        m = re.search(r"(?:创建|设计|一个|叫|名为)\s*[\"']?([a-zA-Z][a-zA-Z0-9_-]*)", sub_args)
        if m:
            return m.group(1).strip("-").lower()

    # 2) 从完整输入中提取英文标识符
    # 优先匹配含连字符的完整标识符（如 frontend-design），但排除已知 typo
    _KNOWN_TYPOS = {"creat", "crate", "creaet", "lst", "ls", "del", "rm", "exec"}
    m = re.search(r"([a-zA-Z][a-zA-Z0-9]+(?:[_-][a-zA-Z][a-zA-Z0-9]+)+)", combined)
    if m:
        name = m.group(1).lower()
        if name not in _KNOWN_TYPOS:
            return name
    # 再匹配单标识符（排除已知 typo 和太短的名字）
    for m in re.finditer(r"([a-zA-Z][a-zA-Z0-9_-]{2,})", combined):
        name = m.group(1).lower()
        if name not in _KNOWN_TYPOS and len(name) >= 3:
            return name

    # 3) sub 本身可能是英文名（但不包括已知的 typo）
    if sub not in _KNOWN_TYPOS and re.match(r"^[a-zA-Z][a-zA-Z0-9_-]{2,}$", sub):
        return sub.lower()

    # 4) 无法提取英文名——用描述内容的稳定哈希生成唯一名（比时间戳更稳定）
    import hashlib
    content_hash = hashlib.md5(combined.encode()).hexdigest()[:6]
    return f"skill-{content_hash}"


def _skill_import_from_url(manager, url: str) -> str:
    """v0.5.4: 从 URL 导入 skill。

    支持 GitHub URL 格式:
    - https://github.com/owner/repo
    - https://github.com/owner/repo/tree/main/skills/my-skill
    - owner/repo
    """
    import re
    import subprocess

    # 解析 URL
    url = url.strip()
    if not url.startswith(("http://", "https://", "github.com/")) and "/" not in url:
        return f"❌ 无法识别的 URL 格式: {url}\n   支持: https://github.com/owner/repo 或 owner/repo"

    if not url.startswith("http"):
        url = f"https://github.com/{url}"

    # 提取 owner/repo
    m = re.search(r"github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/|$)", url)
    if not m:
        return f"❌ 无法解析 GitHub URL: {url}"

    owner, repo = m.group(1), m.group(2)
    console.print(f"[dim]· 正在从 GitHub 获取: {owner}/{repo}...[/dim]")

    try:
        # 使用 gh CLI 或 curl 获取仓库内容
        # 方法 1: 尝试 gh CLI
        gh_available = subprocess.run(
            ["which", "gh"], capture_output=True, text=True
        ).returncode == 0

        if gh_available:
            # 用 gh 获取仓库文件树
            result = subprocess.run(
                ["gh", "repo", "view", f"{owner}/{repo}", "--json", "name,description"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                import json
                try:
                    info = json.loads(result.stdout)
                    repo_desc = info.get('description', '无描述')
                    console.print(f"[dim]  仓库: {repo_desc}[/dim]")
                except json.JSONDecodeError:
                    pass

        # 方法 2: 直接用 curl 获取文件列表
        api_url = f"https://api.github.com/repos/{owner}/{repo}/contents"
        result = subprocess.run(
            ["curl", "-sL", api_url],
            capture_output=True, text=True, timeout=30,
        )

        if result.returncode != 0 or not result.stdout.strip():
            return f"❌ 无法获取仓库内容: {api_url}"

        import json as _json
        try:
            contents = _json.loads(result.stdout)
        except _json.JSONDecodeError:
            return "❌ 无法解析仓库内容 (可能触发了 GitHub API 限流)"

        if not isinstance(contents, list):
            return "❌ 仓库内容格式异常"

        # 查找 YAML 文件（优先 .xenon/skills/ 目录下的）
        yaml_files = []
        for item in contents:
            name = item.get("name", "")
            if name.endswith((".yaml", ".yml")):
                yaml_files.append(item)
            if name == ".xenon" and item.get("type") == "dir":
                # 递归获取 .xenon/skills/ 目录
                sub_url = item.get("url", "")
                sub_result = subprocess.run(
                    ["curl", "-sL", sub_url],
                    capture_output=True, text=True, timeout=30,
                )
                try:
                    sub_contents = _json.loads(sub_result.stdout)
                    if isinstance(sub_contents, list):
                        for si in sub_contents:
                            if si.get("name") == "skills" and si.get("type") == "dir":
                                skills_url = si.get("url", "")
                                skills_result = subprocess.run(
                                    ["curl", "-sL", skills_url],
                                    capture_output=True, text=True, timeout=30,
                                )
                                try:
                                    skills_contents = _json.loads(skills_result.stdout)
                                    if isinstance(skills_contents, list):
                                        for ski in skills_contents:
                                            if ski.get("name", "").endswith((".yaml", ".yml")):
                                                yaml_files.append(ski)
                                except _json.JSONDecodeError:
                                    pass
                except _json.JSONDecodeError:
                    pass

        if not yaml_files:
            return (
                f"❌ 未在仓库 {owner}/{repo} 中找到 skill YAML 文件。\n"
                f"   请确保仓库包含有效的 skill 配置（.yaml 文件）。\n"
                f"   Skill 文件应包含: name, description, steps 字段。"
            )

        # 下载并导入每个 YAML 文件
        imported = []
        for yf in yaml_files:
            download_url = yf.get("download_url", "")
            if not download_url:
                continue

            yaml_result = subprocess.run(
                ["curl", "-sL", download_url],
                capture_output=True, text=True, timeout=30,
            )
            if yaml_result.returncode != 0:
                continue

            try:
                import yaml as _yaml
                data = _yaml.safe_load(yaml_result.stdout)
                if not data or "name" not in data:
                    continue

                # 导入 skill
                skill = manager.create(
                    name=data["name"],
                    description=data.get("description", f"从 {owner}/{repo} 导入"),
                    steps=data.get("steps", []),
                    system_prompt=data.get("system_prompt", ""),
                    params=data.get("params", []),
                )
                _register_skill_handler(skill, manager)
                imported.append(skill.name)

            except Exception as e:
                console.print(f"[yellow]⚠️  导入 {yf.get('name', '?')} 失败: {e}[/yellow]")

        if imported:
            names = ", ".join(f"/{n}" for n in imported)
            return f"✅ 已从 {owner}/{repo} 导入 {len(imported)} 个技能: {names}"
        return "❌ 未能成功导入任何技能，请检查仓库中的 YAML 文件格式。"

    except subprocess.TimeoutExpired:
        return "❌ GitHub API 请求超时，请稍后重试。"
    except Exception as e:
        return f"❌ 导入失败: {e}"


# /project ──────────────────────────────────────────────────

register_command("/project", "查看/刷新项目上下文", "/project [refresh]")

@_handler("/project")
def _cmd_project(*, args: str, session_state: dict[str, Any], **kwargs: Any) -> str:
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

    return f"{summary}{tree_preview}"


# /edit ─────────────────────────────────────────────────────

register_command("/edit", "编辑代码文件（支持 LLM 辅助）", "/edit <file_path> [指令]")

@_handler("/edit")
def _cmd_edit(*, args: str, registry: ModelRegistry, **kwargs: Any) -> str:
    from xenon.repl.code_editor import CodeEditor

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
            console.print(f"\n[bold]{file_path}[/bold] ({line_count} 行)\n")
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
    from xenon.engine.novel_manager import NovelManager

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
        lines.append("\n使用 /novel switch <名称> 切换小说")
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


# ── /permissions — v0.5.0 权限模式管理 ─────────────────────

register_command("/permissions", "查看/切换工具执行权限模式", "/permissions [default|accept_edits|bypass|plan]")

@_handler("/permissions")
def _cmd_permissions(*, args: str, session_state: dict[str, Any] | None = None, **kwargs: Any) -> str:
    repl = session_state.get("_repl") if session_state else None
    if not repl or not hasattr(repl, '_permission_gate'):
        return "权限系统未初始化"

    gate = repl._permission_gate
    current_mode = gate.mode.value

    if not args:
        # 显示当前模式和可用模式
        lines = [
            f"当前权限模式: [bold cyan]{current_mode}[/bold cyan]",
            "",
            "可用模式:",
            "  [bold]default[/bold]      — 写入/Shell 操作前确认",
            "  [bold]accept_edits[/bold] — 自动批准编辑，Shell 仍需确认",
            "  [bold]bypass[/bold]       — 跳过所有确认（CI/自动化场景）",
            "  [bold]plan[/bold]         — 只读模式，拒绝所有写入",
            "",
            "用法: /permissions <模式名>",
            "已记忆允许的工具: " + (", ".join(sorted(gate._session_allow)) if gate._session_allow else "(无)"),
        ]
        return "\n".join(lines)

    mode_map = {
        "default": "DEFAULT",
        "accept_edits": "ACCEPT_EDITS",
        "bypass": "BYPASS",
        "plan": "PLAN",
    }
    mode_key = mode_map.get(args.strip().lower())
    if not mode_key:
        return f"未知模式: {args}。可用: default, accept_edits, bypass, plan"

    from xenon.repl.permissions import PermissionMode
    new_mode = getattr(PermissionMode, mode_key)
    gate.set_mode(new_mode)
    gate.reset_session()  # 切换模式时清除记忆
    return f"✅ 权限模式已切换为: [bold cyan]{new_mode.value}[/bold cyan]"


# ══════════════════════════════════════════════════════════════
# /cost — DeepSeek 缓存命中率 + 费用面板
# ══════════════════════════════════════════════════════════════

register_command(
    "/cost",
    "显示缓存命中率 + 费用明细（本地计算，不消耗 API）",
    "/cost [模型名]",
)


@_handler("/cost")
def _cmd_cost(*, args: str = "", session_state: dict = None, **kwargs: Any) -> str:
    """显示 DeepSeek 缓存命中率 + 费用明细面板。

    所有数据来自 API 响应的 usage.*_tokens 字段，配合本地定价表
    纯本地计算。不调任何 LLM API（零额外消费）。
    """
    repl = session_state.get("_repl") if session_state else None
    tracker = getattr(repl, "_cache_tracker", None) if repl else None

    if not tracker:
        return "[dim]CacheTracker 未初始化。仅在 DeepSeek 模型调用后可用。[/dim]"

    total_cache = tracker.cache_hits + tracker.cache_misses
    if total_cache == 0:
        return "[dim]暂无缓存数据。进行 DeepSeek API 调用后自动统计。[/dim]"

    lines: list[str] = []
    model_filter = args.strip() if args else ""

    models = tracker.all_models
    if model_filter:
        models = [m for m in models if model_filter.lower() in m.lower()]

    for model_id in sorted(models):
        snap = tracker.model_snapshot(model_id)
        if not snap:
            continue

        hr = snap["cache_hit_rate"]
        hr_color = "green" if hr >= 0.70 else ("yellow" if hr >= 0.40 else "red")

        lines.append(f"\n[bold cyan]模型:[/bold cyan] {model_id}")
        lines.append(f"  [dim]调用次数:[/dim] {snap['calls']}")
        lines.append(f"  [dim]Input:[/dim] {snap['prompt_tokens']:,} tokens"
                     f"  [dim]Output:[/dim] {snap['completion_tokens']:,} tokens")
        lines.append(f"  [bold cyan]缓存命中:[/bold cyan] [{hr_color}]{snap['cache_hit_tokens']:,}[/{hr_color}]"
                     f"  ([{hr_color}]{hr:.1%}[/{hr_color}])")
        lines.append(f"  [dim]缓存未命中:[/dim] {snap['cache_miss_tokens']:,}"
                     f"  ([dim]{1 - hr:.1%}[/dim])")
        lines.append(f"  [bold yellow]预估费用:[/bold yellow] ¥{snap['cost_yuan']:.4f}")
        if snap['saved_yuan'] > 0.0001:
            saved_pct = int(snap['saved_yuan'] / (snap['cost_yuan'] + snap['saved_yuan']) * 100)
            lines.append(f"  [bold green]节省:[/bold green] ¥{snap['saved_yuan']:.4f} ({saved_pct}%)"
                         f"  [dim]vs 全未命中[/dim]")
        lines.append("")

    # 汇总
    if len(models) > 1:
        lines.append("[bold]─── 汇总 ───[/bold]")
        lines.append(f"  [dim]总缓存命中率:[/dim] [bold]{tracker.cache_hit_rate_pct}[/bold]")
        lines.append(f"  [dim]总费用:[/dim] [bold yellow]{tracker.estimated_cost_display}[/bold yellow]")
        if tracker.savings_pct > 0:
            lines.append(f"  [dim]总节省:[/dim] [bold green]¥{tracker.savings_yuan:.4f} ({tracker.savings_pct}%)[/bold green]")

    if not lines:
        return f"[dim]未找到匹配 '{model_filter}' 的模型数据。[/dim]"

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# /vision — 多模态视觉模式
# ══════════════════════════════════════════════════════════════

register_command(
    "/vision",
    "切换视觉粘贴模式 (Ctrl+Alt+V 粘贴图片，多模态模型转录)",
    "/vision [on|off]",
)


@_handler("/vision")
def _cmd_vision(*, args: str = "", session_state: dict = None, **kwargs: Any) -> str:
    """切换视觉粘贴模式。

    开启后，按 Ctrl+Alt+V 可将剪贴板图片通过模型池中
    的多模态模型转录为文字，注入到对话中。
    """
    repl = session_state.get("_repl") if session_state else None
    if not repl:
        return "[dim]REPL 未初始化[/dim]"

    bridge = getattr(repl, "_vision_bridge", None)
    if not bridge:
        return "[dim]视觉桥接器未初始化[/dim]"

    arg = args.strip().lower()
    if arg == "on":
        repl._vision_enabled = True
        repl._start_vision_monitor()
        return (
            "[bold green]👁 视觉模式已开启[/bold green]\n"
            "按 [bold]Ctrl+Alt+V[/bold] 粘贴剪贴板图片，"
            "系统将自动用多模态模型转录为文字。"
        )
    elif arg == "off":
        repl._vision_enabled = False
        if repl._clipboard_monitor.is_running:
            repl._clipboard_monitor.stop()
        return "[dim]👁 视觉模式已关闭[/dim]"
    else:
        status = "开启" if repl._vision_enabled else "关闭"
        hint = (
            f"👁 视觉模式: [bold]{status}[/bold]\n"
            f"用法: /vision on 开启 | /vision off 关闭\n"
            f"开启后按 Ctrl+Alt+V 粘贴剪贴板图片"
        )
        return hint
