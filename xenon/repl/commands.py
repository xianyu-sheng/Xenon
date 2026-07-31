"""
Slash Commands — 斜杠命令处理器。

每个命令是一个独立的函数，接收 REPL 上下文并返回要显示的文本。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.panel import Panel

from xenon.repl.model_registry import BUILTIN_MODES
from xenon.repl.command_registry import (
    COMMANDS,  # noqa: F401 - compatibility export
    _HANDLERS,  # noqa: F401 - compatibility export
    ExitSignal,  # noqa: F401 - compatibility export
    command_handler,
    dispatch_command,  # noqa: F401 - compatibility export
    register_command,
)
from xenon.repl.command_groups.runtime import (
    cmd_optimize as _cmd_optimize,  # noqa: F401 - compatibility export
    cmd_stream as _cmd_stream,  # noqa: F401 - compatibility export
    cmd_thinking as _cmd_thinking,  # noqa: F401 - compatibility export
    cmd_verbose as _cmd_verbose,  # noqa: F401 - compatibility export
)
from xenon.repl.command_groups.cache import (
    _CACHE_CAUSE_LABELS,  # noqa: F401 - compatibility export
    _CACHE_STATE_LABELS,  # noqa: F401 - compatibility export
    _cache_doctor,  # noqa: F401 - compatibility export
    _cache_explain,  # noqa: F401 - compatibility export
    _cache_history,  # noqa: F401 - compatibility export
    _cache_lanes,  # noqa: F401 - compatibility export
    _cache_optimize,  # noqa: F401 - compatibility export
    _cache_status,  # noqa: F401 - compatibility export
    _cache_tracker,  # noqa: F401 - compatibility export
    _cmd_cache,  # noqa: F401 - compatibility export
    _cmd_cost,  # noqa: F401 - compatibility export
    _cmd_fix_cache,  # noqa: F401 - compatibility export
)
from xenon.repl.command_groups.common import confirm_action, console
from xenon.repl.command_groups.skill import (
    _FUZZY_ALIASES,  # noqa: F401 - compatibility export
    _SKILL_FUZZY,  # noqa: F401 - compatibility export
    _cmd_skill,  # noqa: F401 - compatibility export
    _execute_installed_skill,  # noqa: F401 - compatibility export
    _extract_skill_name,  # noqa: F401 - compatibility export
    _fallback_skill_steps,  # noqa: F401 - compatibility export
    _format_skill_preview,  # noqa: F401 - compatibility export
    _fuzzy_match_subcommand,  # noqa: F401 - compatibility export
    _generate_skill_steps,  # noqa: F401 - compatibility export
    _parse_skill_steps,  # noqa: F401 - compatibility export
    _register_skill_handler,  # noqa: F401 - compatibility export
    _skill_auto_generate,  # noqa: F401 - compatibility export
    _skill_create_interactive,  # noqa: F401 - compatibility export
    _skill_import_from_url,  # noqa: F401 - compatibility export
    _skill_manual_create,  # noqa: F401 - compatibility export
)
from xenon.repl.command_groups.session import (
    _cmd_clear,  # noqa: F401 - compatibility export
    _cmd_compact,  # noqa: F401 - compatibility export
    _cmd_config,  # noqa: F401 - compatibility export
    _cmd_context,  # noqa: F401 - compatibility export
    _cmd_exit,  # noqa: F401 - compatibility export
    _cmd_help,  # noqa: F401 - compatibility export
    _cmd_history,  # noqa: F401 - compatibility export
    _cmd_load,  # noqa: F401 - compatibility export
    _cmd_resume,  # noqa: F401 - compatibility export
    _cmd_save,  # noqa: F401 - compatibility export
    _cmd_sessions,  # noqa: F401 - compatibility export
    _cmd_undo,  # noqa: F401 - compatibility export
)
from xenon.repl.command_groups.workspace import (
    _cmd_edit,  # noqa: F401 - compatibility export
    _cmd_permissions,  # noqa: F401 - compatibility export
    _cmd_project,  # noqa: F401 - compatibility export
    _cmd_vision,  # noqa: F401 - compatibility export
)

if TYPE_CHECKING:
    from xenon.repl.model_registry import ModelRegistry
    from xenon.repl.context_manager import ContextManager

# ── 命令处理器 ────────────────────────────────────────────

# Private compatibility name retained for existing command modules/tests.
_handler = command_handler

# Backward-compatible alias for P3-Q8 confirmations.
_confirm = confirm_action


# /set_model ───────────────────────────────────────────────

register_command(
    "/set_model",
    "交互式选择或配置模型",
    "/set_model [alias] [provider/model_name] [reasoning_effort=max] [base_url=xxx]",
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
        if m.reasoning_effort:
            lines.append(f"           推理强度: {m.reasoning_effort}")

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

# Session, history, and context commands live in repl.command_groups.session.
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


# Runtime toggle commands live in repl.command_groups.runtime.


# /sub-agent ──────────────────────────────────────────────

register_command(
    "/sub-agent",
    "委派子 Agent 执行任务（支持多引擎和并行）",
    "/sub-agent <task> [--engine react|plan_execute|reflection|plan_react|plan_reflection|react_reflection|direct] [--timeout N] [--parallel task1|task2|...]",
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
        ("docs_fetch", "llms.txt 优先的官方文档检索", "url, query, max_pages, max_chars"),
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
    "/memory status|list|search|inspect|doctor|add|replace|rollback|archive|restore|pin|migrate|clear [参数]",
)

@_handler("/memory")
def _cmd_memory(*, args: str, session_state: dict[str, Any], **kwargs: Any) -> str:
    repl = session_state.get("_repl")
    if repl is not None and hasattr(repl, "_get_memory_service"):
        return _cmd_memory_v2(args=args, repl=repl)

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

        # 标志可位于内容前后，也可同时使用；``--`` 后全部按正文处理。
        import shlex

        try:
            tokens = shlex.split(sub_args)
        except ValueError as exc:
            return f"记忆参数解析失败: {exc}"

        mem_type = "fact"
        tags: list[str] = []
        content_tokens: list[str] = []
        parse_flags = True
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token == "--":
                parse_flags = False
                i += 1
                continue
            if parse_flags and token in {"--type", "--tags"}:
                if i + 1 >= len(tokens) or tokens[i + 1].startswith("--"):
                    return f"参数 {token} 缺少值"
                value = tokens[i + 1]
                if token == "--type":
                    mem_type = value
                else:
                    tags = [tag.strip() for tag in value.split(",") if tag.strip()]
                i += 2
                continue
            if parse_flags and token.startswith("--type="):
                mem_type = token.split("=", 1)[1]
            elif parse_flags and token.startswith("--tags="):
                tags = [
                    tag.strip()
                    for tag in token.split("=", 1)[1].split(",")
                    if tag.strip()
                ]
            else:
                content_tokens.append(token)
            i += 1

        valid_types = {"fact", "project", "error", "preference"}
        if mem_type not in valid_types:
            return f"无效记忆类型: {mem_type}（可选: {', '.join(sorted(valid_types))}）"
        text = " ".join(content_tokens)

        if not text.strip():
            return "用法: /memory add <记忆内容>"

        memory = store.add(text.strip(), type=mem_type, tags=tags)
        return f"✅ 已添加记忆 [{memory.id}]: {memory.content[:60]}"

    elif sub in {"delete", "forget"}:
        memory_id = sub_args.strip()
        if not memory_id:
            return "用法: /memory delete <记忆 ID>"
        if store.delete(memory_id):
            return f"✅ 已删除记忆 [{memory_id}]"
        return f"未找到记忆 [{memory_id}]"

    elif sub == "clear":
        if not _confirm("确认清空全部跨会话记忆？", default=False):
            return "已取消清空记忆。"
        count = store.clear()
        return f"已清空 {count} 条记忆。"

    else:
        return "用法: /memory list|search|add|delete|clear [参数]"


def _cmd_memory_v2(*, args: str, repl: Any) -> str:
    """Interactive command surface backed by the governed v2 service."""
    import shlex

    from xenon.memory import MemoryKind, MemoryScope

    service = repl._get_memory_service()
    parts = args.split(maxsplit=1) if args.strip() else []
    sub = parts[0].lower() if parts else "list"
    sub_args = parts[1] if len(parts) > 1 else ""

    if sub in {"status", "where"}:
        lines = ["Xenon Memory v2 存储位置:"]
        for scope in (
            MemoryScope.USER,
            MemoryScope.PROJECT_LOCAL,
            MemoryScope.PROJECT_SHARED,
        ):
            try:
                backend = service.registry.get(scope)
                lines.append(f"  {scope.value}: {backend.root}")
            except ValueError:
                lines.append(f"  {scope.value}: 未激活（当前未检测到项目）")
        lines.extend([
            "  session: 当前进程内存（退出即清除）",
            "",
            "metadata.json 是机器权威数据；INDEX.md 与分类 Markdown 是可读视图。",
            "写入采用跨进程事务锁；冲突只提示，不会静默覆盖。",
            (
                f"阈值: 单条 {service.policy.max_item_tokens} · "
                f"分类 {service.policy.max_leaf_tokens} · "
                f"作用域 {service.policy.max_active_tokens} tokens"
            ),
        ])
        return "\n".join(lines)

    if sub == "list":
        include_archived = "--all" in sub_args
        records = service.list_records(include_archived=include_archived)
        if not records:
            return "暂无 v2 记忆。可直接说：记住，项目使用 Python 3.12。"
        lines = [f"共 {len(records)} 条记忆（JSON 为权威数据，Markdown 为可读视图）:\n"]
        for record in records:
            lines.append(
                f"  [{record.id}] [{record.scope.value}/{record.kind.value}/{record.status.value}] "
                f"{record.content[:100]}"
            )
            lines.append(
                f"     检索 {record.retrieval_count} 次 · 使用 {record.use_count} 次 · "
                f"创建 {record.created_at[:10]}"
            )
        return "\n".join(lines)

    if sub == "search":
        try:
            search_tokens = shlex.split(sub_args)
        except ValueError as exc:
            return f"搜索参数解析失败: {exc}"
        explain = "--explain" in search_tokens
        query = " ".join(token for token in search_tokens if token != "--explain")
        if not query:
            return "用法: /memory search <关键词>"
        matches = service.explain_retrieval(query, limit=20)
        if not matches:
            return f"未找到与 '{query}' 相关的记忆。"
        lines = [f"找到 {len(matches)} 条记忆:"]
        for match in matches:
            record = match.record
            lines.append(f"  [{record.id}] [{record.scope.value}] {record.content}")
            if explain:
                lines.append(
                    f"     分数 {match.score:.2f} · {'；'.join(match.reasons)}"
                )
        return "\n".join(lines)

    if sub in {"inspect", "show"}:
        memory_id = sub_args.strip()
        if not memory_id:
            return "用法: /memory inspect <记忆 ID>"
        record = service.get(memory_id)
        if record is None:
            return f"未找到记忆 [{memory_id}]"
        lines = [
            f"记忆 [{record.id}]",
            f"  状态: {record.status.value}",
            f"  范围/类型: {record.scope.value} / {record.kind.value}",
            f"  内容: {record.content}",
            f"  位置: {service.destination_for(record.scope, record.kind)}",
            f"  创建: {record.created_at}",
            f"  更新: {record.updated_at}",
            f"  最近检索: {record.last_retrieved_at or '-'} ({record.retrieval_count} 次)",
            f"  最近使用: {record.last_used_at or '-'} ({record.use_count} 次)",
            f"  重要度/置信度: {record.importance:.2f} / {record.confidence:.2f}",
            f"  固定: {'是' if record.pinned else '否'}",
            f"  来源: {record.source}",
            f"  校验和: {record.checksum}",
        ]
        if record.tags:
            lines.append(f"  标签: {', '.join(record.tags)}")
        if record.evidence:
            lines.append(f"  证据: {record.evidence}")
        if record.supersedes:
            lines.append(f"  替代: {record.supersedes}")
        if record.expires_at:
            lines.append(f"  过期: {record.expires_at}")
        return "\n".join(lines)

    if sub in {"doctor", "check"}:
        report = service.diagnose()
        state = "健康" if report.healthy and not report.issues else "可用但有提示"
        if not report.healthy:
            state = "发现错误"
        lines = [
            f"Memory doctor: {state}",
            f"  活动 {report.active_count} · 非活动 {report.inactive_count} · "
            f"活动约 {report.active_tokens} tokens",
        ]
        if not report.issues:
            lines.append("  未发现结构、权限、容量或生命周期问题。")
        for issue in report.issues:
            target = f" [{issue.memory_id}]" if issue.memory_id else ""
            lines.append(
                f"  {issue.severity.upper()} [{issue.scope.value}]{target} "
                f"{issue.message}"
            )
        return "\n".join(lines)

    if sub == "add":
        if not sub_args.strip():
            return (
                "用法: /memory add <内容> [--scope user|project-local|project-shared|session] "
                "[--kind preference|fact|decision|constraint|lesson]"
            )
        try:
            tokens = shlex.split(sub_args)
        except ValueError as exc:
            return f"记忆参数解析失败: {exc}"
        scope = (
            MemoryScope.PROJECT_LOCAL
            if service.registry.has_project
            else MemoryScope.USER
        )
        kind = MemoryKind.FACT
        content: list[str] = []
        index = 0
        try:
            while index < len(tokens):
                token = tokens[index]
                if token == "--scope":
                    scope = MemoryScope(tokens[index + 1])
                    index += 2
                elif token.startswith("--scope="):
                    scope = MemoryScope(token.split("=", 1)[1])
                    index += 1
                elif token in {"--kind", "--type"}:
                    raw_kind = tokens[index + 1]
                    raw_kind = {"project": "fact", "error": "lesson"}.get(raw_kind, raw_kind)
                    kind = MemoryKind(raw_kind)
                    index += 2
                elif token.startswith(("--kind=", "--type=")):
                    raw_kind = token.split("=", 1)[1]
                    raw_kind = {"project": "fact", "error": "lesson"}.get(raw_kind, raw_kind)
                    kind = MemoryKind(raw_kind)
                    index += 1
                else:
                    content.append(token)
                    index += 1
        except (IndexError, ValueError) as exc:
            return f"无效记忆参数: {exc}"
        try:
            receipt = service.remember(
                " ".join(content),
                scope=scope,
                kind=kind,
                source="slash-command",
                confidence=1.0,
            )
        except ValueError as exc:
            return f"❌ 未写入记忆：{exc}"
        action = "已添加" if receipt.created else "已去重并更新"
        result = (
            f"✅ {action}记忆 [{receipt.record.id}]\n"
            f"范围: {scope.value}\n位置: {receipt.destination}\n"
            f"撤销: /memory archive {receipt.record.id}"
        )
        if receipt.conflict_ids:
            result += (
                "\n⚠️ 潜在冲突（未覆盖）: " + ", ".join(receipt.conflict_ids)
                + "\n明确替代: /memory replace <旧ID> <新内容>"
            )
        if receipt.warning:
            result += f"\n提示: {receipt.warning}"
        return result

    if sub == "replace":
        try:
            tokens = shlex.split(sub_args)
        except ValueError as exc:
            return f"替换参数解析失败: {exc}"
        if len(tokens) < 2:
            return "用法: /memory replace <旧记忆 ID> <新内容>"
        memory_id, content = tokens[0], " ".join(tokens[1:])
        try:
            receipt = service.replace(memory_id, content)
        except ValueError as exc:
            return f"❌ 未替换记忆：{exc}"
        result = (
            f"✅ 已用 [{receipt.record.id}] 替代 [{memory_id}]\n"
            f"位置: {receipt.destination}\n"
            f"撤销替代: /memory rollback {receipt.record.id}"
        )
        if receipt.conflict_ids:
            result += "\n仍有潜在冲突: " + ", ".join(receipt.conflict_ids)
        if receipt.warning:
            result += f"\n提示: {receipt.warning}"
        return result

    if sub == "rollback":
        memory_id = sub_args.strip()
        if not memory_id:
            return "用法: /memory rollback <替代记忆 ID>"
        if service.rollback(memory_id):
            return f"✅ 已撤销替代 [{memory_id}]，前一版本已恢复"
        return f"无法撤销 [{memory_id}]：它不是活动的替代记忆或替代链已变化"

    if sub in {"archive", "delete", "forget"}:
        memory_id = sub_args.strip()
        if not memory_id:
            return "用法: /memory archive <记忆 ID>"
        if service.archive(memory_id):
            return f"✅ 已归档记忆 [{memory_id}]（可用 /memory restore {memory_id} 恢复）"
        return f"未找到活动记忆 [{memory_id}]"

    if sub == "restore":
        memory_id = sub_args.strip()
        if not memory_id:
            return "用法: /memory restore <记忆 ID>"
        try:
            restored = service.restore(memory_id)
        except ValueError as exc:
            return f"❌ 无法恢复记忆：{exc}"
        if restored:
            return f"✅ 已恢复记忆 [{memory_id}]"
        return f"未找到已归档记忆 [{memory_id}]"

    if sub in {"pin", "unpin"}:
        memory_id = sub_args.strip()
        if not memory_id:
            return f"用法: /memory {sub} <记忆 ID>"
        pinned = sub == "pin"
        if service.set_pinned(memory_id, pinned):
            action = "固定" if pinned else "取消固定"
            return f"✅ 已{action}记忆 [{memory_id}]"
        return f"未找到活动记忆 [{memory_id}]"

    if sub == "migrate":
        from xenon.repl.memory import MemoryStore

        target_scope = MemoryScope.USER
        if sub_args.strip():
            try:
                tokens = shlex.split(sub_args)
                if tokens == ["--scope", "project-local"]:
                    target_scope = MemoryScope.PROJECT_LOCAL
                elif tokens == ["--scope", "user"]:
                    target_scope = MemoryScope.USER
                else:
                    return "用法: /memory migrate [--scope user|project-local]"
            except ValueError as exc:
                return f"迁移参数解析失败: {exc}"
        legacy = MemoryStore().list_all()
        if not legacy:
            return "未发现旧版 ~/.xenon/memory.json 记忆。"
        kind_map = {
            "preference": MemoryKind.PREFERENCE,
            "error": MemoryKind.LESSON,
            "project": MemoryKind.FACT,
            "fact": MemoryKind.FACT,
        }
        created = deduplicated = skipped = 0
        for item in legacy:
            try:
                receipt = service.remember(
                    item.content,
                    scope=target_scope,
                    kind=kind_map.get(item.type, MemoryKind.FACT),
                    tags=item.tags,
                    source="legacy-v1-migration",
                    confidence=0.8,
                )
                if receipt.created:
                    created += 1
                else:
                    deduplicated += 1
            except (ValueError, OSError):
                skipped += 1
        return (
            f"✅ 旧版记忆迁移完成：新增 {created}，去重 {deduplicated}，跳过 {skipped}。\n"
            "旧文件未删除；确认新记忆无误后可自行备份或移除。"
        )

    if sub == "clear":
        records = service.list_records()
        if not records:
            return "暂无活动记忆。"
        if not _confirm(f"确认归档全部 {len(records)} 条活动记忆？", default=False):
            return "已取消。"
        for record in records:
            service.archive(record.id)
        return f"✅ 已归档 {len(records)} 条记忆；未物理删除，可按 ID 恢复。"

    return (
        "用法: /memory status|list|search|inspect|doctor|add|replace|rollback|"
        "archive|restore|pin|unpin|migrate|clear [参数]"
    )


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
