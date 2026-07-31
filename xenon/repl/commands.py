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

from xenon.repl.command_groups.resources import (
    _MCP_USAGE,  # noqa: F401 - compatibility export
    _cmd_library,  # noqa: F401 - compatibility export
    _cmd_mcp,  # noqa: F401 - compatibility export
    _cmd_setup,  # noqa: F401 - compatibility export
    _cmd_skill_discover,  # noqa: F401 - compatibility export
    _cmd_skill_install,  # noqa: F401 - compatibility export
    _cmd_status,  # noqa: F401 - compatibility export
    _cmd_tools,  # noqa: F401 - compatibility export
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

# ── 命令处理器 ────────────────────────────────────────────

# Private compatibility name retained for existing command modules/tests.
_handler = command_handler

# Backward-compatible alias for P3-Q8 confirmations.
_confirm = confirm_action


# Model and provider commands live in repl.command_groups.model.
# Agent execution commands live in repl.command_groups.agent.






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
