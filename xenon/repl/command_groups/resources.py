"""External resources and system slash commands.

This group covers MCP server management, library discovery,
skill browsing/installation, tool listing, system status,
and interactive setup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xenon.repl.command_groups.common import confirm_action
from xenon.repl.command_registry import command_handler, register_command

if TYPE_CHECKING:
    from xenon.repl.model_registry import ModelRegistry
    from xenon.repl.context_manager import ContextManager


# /mcp ──────────────────────────────────────────────────

register_command("/mcp", "管理 MCP 服务器连接", "/mcp [add|list|tools|remove|discover|install] [args]")

@command_handler("/mcp")
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
            if not confirm_action(f"移除 MCP 服务器 '{name}'？", default=False):
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
            if not confirm_action(f"移除惰性 MCP 服务器 '{name}'（尚未连接）？", default=False):
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

@command_handler("/library")
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


@command_handler("/skill-discover")
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


@command_handler("/skill-install")
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

@command_handler("/status")
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

@command_handler("/setup")
def _cmd_setup(*, session_state: dict, **kwargs: Any) -> str:
    from xenon.repl.setup_wizard import interactive_setup

    repl = session_state.get("_repl")
    if repl:
        interactive_setup(repl.registry, model_pool=repl.model_pool)
        return ""
    return "❌ 无法获取 REPL 状态"


# /tools ───────────────────────────────────────────────────

register_command("/tools", "查看所有可用工具类型", "/tools")

@command_handler("/tools")
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



