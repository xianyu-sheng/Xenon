"""Model and provider configuration slash commands.

This group covers model registration, removal, listing, pool management,
batch import/export, performance profiles, role assignment, paradigm
switching, and provider inspection.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from xenon.repl.command_registry import command_handler, register_command
from xenon.repl.model_registry import BUILTIN_MODES

if TYPE_CHECKING:
    from xenon.repl.model_registry import ModelRegistry


# /set_model ───────────────────────────────────────────────

register_command(
    "/set_model",
    "交互式选择或配置模型",
    "/set_model [alias] [provider/model_name] [reasoning_effort=max] [base_url=xxx]",
)

@command_handler("/set_model")
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
        "deepseek-v4-pro": "旗舰编程与复杂 Agent · 1M 上下文",
        "deepseek-v4-flash": "高并发高性价比 · 1M 上下文",
        "deepseek-chat": "旧别名（2026-07-24 停用）",
        "deepseek-reasoner": "旧别名（2026-07-24 停用）",
        "gemini-2.0-flash": "最新，快速", "gemini-1.5-pro": "长上下文",
        "glm-4-plus": "旗舰", "glm-4-flash": "快速，免费",
        "qwen-max": "旗舰", "qwen-plus": "性价比高", "qwen-turbo": "快速，便宜",
        "moonshot-v1-128k": "128K 上下文",
    }
    return hints.get(model_name, "")


# /remove_model ────────────────────────────────────────────

register_command("/remove_model", "移除一个模型", "/remove_model <alias>")

@command_handler("/remove_model")
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

@command_handler("/models")
def _cmd_models(*, registry: ModelRegistry, **kwargs: Any) -> str:
    models = registry.list_models()
    if not models:
        return "暂无已注册模型。使用 /set_model 添加模型。"

    lines = ["已注册模型:\n"]
    for m in models:
        lines.append(f"  [{m.alias}] {m.model_id}")
        if m.base_url:
            lines.append(f"           端点: {m.base_url}")
        if m.reasoning_effort:
            lines.append(f"           推理强度: {m.reasoning_effort}")

    if registry.role_priority:
        lines.append("\n角色分配:")
        for role, aliases in registry.role_priority.items():
            lines.append(f"  {role}: {' -> '.join(aliases)}")

    return "\n".join(lines)

# /pool ────────────────────────────────────────────────────

register_command("/pool", "查看模型调用池（v0.4.0）", "/pool")

@command_handler("/pool")
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


@command_handler("/import_models")
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


@command_handler("/reload_models")
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


@command_handler("/set_profile")
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

# /set_role ────────────────────────────────────────────────

register_command(
    "/set_role",
    "为角色设置模型优先级",
    "/set_role <role> <alias1> [alias2] [alias3] ...",
)

@command_handler("/set_role")
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

@command_handler("/mode")
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


# /model ───────────────────────────────────────────────────

register_command(
    "/model",
    "按别名或 provider/model 直接切换模型",
    "/model [alias|provider/model]",
)


def _activate_model(selected: Any, *, registry: ModelRegistry, session_state: dict) -> None:
    """Make a selected model effective for both static and auto routing."""
    registry.role_priority["planner"] = [selected.alias]
    repl = session_state.get("_repl")
    if repl is not None:
        # AutoRouter otherwise keeps choosing from its scored pool and can
        # ignore the planner role changed by /model.
        repl._preferred_model_ids = [selected.model_id]
        auto_router = getattr(repl, "auto_router", None)
        if auto_router is not None and hasattr(auto_router, "reset_session_lock"):
            auto_router.reset_session_lock()
        if hasattr(repl, "_failed_models"):
            repl._failed_models.discard(selected.model_id)

    pool = session_state.get("model_pool")
    if pool is not None and hasattr(pool, "get") and pool.get(selected.alias) is None:
        pool.register(
            selected.model_id,
            alias=selected.alias,
            weight=getattr(selected, "weight", 1.0),
            api_key=getattr(selected, "api_key", ""),
            base_url=getattr(selected, "base_url", ""),
        )


@command_handler("/model")
def _cmd_model(*, args: str = "", session_state: dict, registry: ModelRegistry, **kwargs: Any) -> str:
    from rich.table import Table as _Table
    from rich.prompt import IntPrompt as _IntPrompt
    from rich.console import Console as _Console

    models = registry.list_models()
    if not models:
        return "暂无已注册模型。请先执行 /set_model 注册模型。"

    requested = args.strip()
    if requested:
        # Accept either the displayed alias or the canonical provider/model ID.
        selected = next(
            (m for m in models if m.alias == requested or m.model_id == requested),
            None,
        )
        if selected is None:
            aliases = ", ".join(m.alias for m in models)
            return f"❌ 未找到模型 '{requested}'。可用别名: {aliases}"
        _activate_model(selected, registry=registry, session_state=session_state)
        return f"✅ 已切换到: {selected.alias} ({selected.model_id})"

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
    _activate_model(selected, registry=registry, session_state=session_state)
    return f"✅ 已切换到: {selected.alias} ({selected.model_id})"



# /provider ────────────────────────────────────────────────

register_command("/provider", "查看已配置的厂商和可用模型", "/provider")

@command_handler("/provider")
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


