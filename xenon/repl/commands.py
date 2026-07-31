"""
Slash Commands — 斜杠命令处理器。

每个命令是一个独立的函数，接收 REPL 上下文并返回要显示的文本。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.panel import Panel

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
from xenon.repl.command_groups.agent import (
    _cmd_ask,  # noqa: F401 - compatibility export
    _cmd_code,  # noqa: F401 - compatibility export
    _cmd_run,  # noqa: F401 - compatibility export
    _cmd_sub_agent,  # noqa: F401 - compatibility export
)
from xenon.repl.command_groups.model import (
    _cmd_import_models,  # noqa: F401 - compatibility export
    _cmd_mode,  # noqa: F401 - compatibility export
    _cmd_model,  # noqa: F401 - compatibility export
    _cmd_models,  # noqa: F401 - compatibility export
    _cmd_pool,  # noqa: F401 - compatibility export
    _cmd_provider,  # noqa: F401 - compatibility export
    _cmd_reload_models,  # noqa: F401 - compatibility export
    _cmd_remove_model,  # noqa: F401 - compatibility export
    _cmd_set_model,  # noqa: F401 - compatibility export
    _cmd_set_profile,  # noqa: F401 - compatibility export
    _cmd_set_role,  # noqa: F401 - compatibility export
    _model_hint_local,  # noqa: F401 - compatibility export
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


# Model and provider commands live in repl.command_groups.model.
# Agent execution commands live in repl.command_groups.agent.




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
