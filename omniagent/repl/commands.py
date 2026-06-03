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
        try:
            config = registry.add_model(model_id, alias, **extra)
            return f"✅ 模型已注册: {alias} -> {config.model_id}"
        except Exception as e:
            return f"❌ 注册失败: {e}"

    # 无参数 → 交互式选择
    from omniagent.repl.provider_registry import get_configured_providers
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
    "/mode [mode_name]\n可用: direct, plan-execute, react, reflection",
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
    from omniagent.repl.setup_wizard import interactive_setup

    repl = session_state.get("_repl")
    if repl:
        interactive_setup(repl.registry)
        return ""
    return "❌ 无法获取 REPL 状态"


# /model ───────────────────────────────────────────────────

register_command("/model", "交互式切换模型", "/model")

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
        choice = _IntPrompt.ask(
            "输入编号切换模型",
            choices=[str(i) for i in range(1, len(models) + 1)],
            default="1",
        )
    except (KeyboardInterrupt, EOFError, OSError):
        return "已取消"

    selected = models[choice - 1]
    registry.role_priority["planner"] = [selected.alias]
    return f"✅ 已切换到: {selected.alias} ({selected.model_id})"


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
        ("command", "执行终端命令", "action='dir' 或 action='python test.py'"),
        ("write_file", "写入文件", "file_path, content"),
        ("read_file", "读取文件", "file_path"),
        ("list_files", "目录遍历（glob 模式）", "file_path='.', pattern='*.py', max_depth=5"),
        ("search_files", "文件内容搜索（grep）", "file_path='.', search_pattern='TODO', file_filter='*.py'"),
        ("git", "Git 操作", "git_command='status|diff|log|add|commit|branch'"),
        ("web_fetch", "抓取网页内容", "url='https://example.com'"),
    ]

    lines = ["可用工具类型:\n"]
    for name, desc, params in tools_info:
        lines.append(f"  [bold]{name}[/bold] — {desc}")
        lines.append(f"    参数: {params}")
        lines.append("")
    lines.append("工具可在 YAML 工作流中通过 action_type 字段使用。")
    lines.append("也可在 REPL 中通过 /code 命令间接使用。")
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

@_handler("/shortcut")
def _cmd_shortcut(*, args: str, registry: ModelRegistry, session_state: dict[str, Any], **kwargs: Any) -> str:
    from omniagent.repl.shortcut_manager import ShortcutManager

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
    from rich.panel import Panel

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
        from omniagent.utils.llm_client import chat_completion

        model_ids = registry.get_role_priority("planner") if registry else []
        if not model_ids:
            return []

        prompt = f"""根据以下描述，生成一组 shell 命令（Windows PowerShell 兼容）。

描述: {description}

要求:
- 返回 JSON 数组，每个元素是一条 shell 命令
- 命令要实用、安全
- 只返回 JSON 数组，不要其他内容

示例: ["echo 'hello'", "dir"]"""

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
    "/skill create|list|run|delete [参数]",
)

@_handler("/skill")
def _cmd_skill(*, args: str, registry: ModelRegistry, session_state: dict[str, Any], **kwargs: Any) -> str:
    from omniagent.repl.skill_manager import SkillManager

    manager = SkillManager()
    parts = args.split(maxsplit=1) if args.strip() else []
    sub = parts[0].lower() if parts else "list"
    sub_args = parts[1] if len(parts) > 1 else ""

    if sub == "list":
        skills = manager.list_all()
        if not skills:
            return "暂无技能。使用 /skill create 创建。"

        lines = [f"共 {len(skills)} 个技能:\n"]
        for s in skills:
            lines.append(f"  /{s.name} — {s.description}")
            lines.append(f"    步骤: {len(s.steps)} 个")
        return "\n".join(lines)

    elif sub == "create":
        return _skill_create_interactive(manager, registry=registry)

    elif sub == "run":
        if not sub_args:
            return "用法: /skill run <name> [参数]"
        parts2 = sub_args.split(maxsplit=1)
        name = parts2[0]
        run_args = parts2[1] if len(parts2) > 1 else ""
        model_ids = registry.get_role_priority("planner")
        return manager.execute(name, run_args, model_priority=model_ids)

    elif sub == "delete":
        if not sub_args:
            return "用法: /skill delete <name>"
        if manager.remove(sub_args.strip()):
            return f"✅ 已删除技能: {sub_args.strip()}"
        return f"❌ 技能 /{sub_args.strip()} 不存在"

    else:
        return "用法: /skill create|list|run|delete [参数]"


def _skill_create_interactive(manager, registry=None) -> str:
    """交互式创建技能。支持智能生成和手动配置两种模式。"""
    from rich.prompt import Prompt as _Prompt
    from rich.panel import Panel

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


def _skill_auto_generate(name: str, description: str, manager, registry=None) -> str:
    """智能生成技能步骤。"""
    from rich.panel import Panel
    from rich.prompt import Prompt as _Prompt

    console.print("\n[dim]🤖 正在根据你的描述生成技能步骤...[/dim]\n")

    # 用 LLM 生成步骤
    steps, system_prompt = _generate_skill_steps(description, registry)

    if not steps:
        console.print("[yellow]⚠️  自动生成失败，切换到手动模式。[/yellow]")
        return _skill_manual_create(name, description, manager)

    # 展示生成结果供用户学习
    console.print(Panel(
        _format_skill_preview(steps, system_prompt),
        title="[bold green]✅ 自动生成的技能[/bold green]",
        border_style="green",
        padding=(1, 2),
    ))

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
    return f"✅ 技能 /{skill.name} 已创建！使用 /skill run {skill.name} 执行。"


def _generate_skill_steps(description: str, registry=None) -> tuple[list[dict], str]:
    """用 LLM 根据描述生成技能步骤。"""
    try:
        from omniagent.utils.llm_client import chat_completion

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
    return f"✅ 技能 /{skill.name} 已创建！使用 /skill run {skill.name} 执行。"


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
    from omniagent.repl.code_editor import CodeEditor

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
