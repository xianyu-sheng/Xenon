"""Memory management slash commands.

This group covers the v1 and v2 /memory command surfaces.
"""

from __future__ import annotations

import shlex
from typing import Any

from xenon.repl.command_groups.common import confirm_action
from xenon.repl.command_registry import command_handler, register_command


# /memory ──────────────────────────────────────────────────

register_command(
    "/memory",
    "管理跨会话记忆",
    "/memory status|list|search|inspect|doctor|add|replace|rollback|archive|restore|pin|migrate|clear [参数]",
)

@command_handler("/memory")
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
        if not confirm_action("确认清空全部跨会话记忆？", default=False):
            return "已取消清空记忆。"
        count = store.clear()
        return f"已清空 {count} 条记忆。"

    else:
        return "用法: /memory list|search|add|delete|clear [参数]"


def _cmd_memory_v2(*, args: str, repl: Any) -> str:
    """Interactive command surface backed by the governed v2 service."""

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
        if not confirm_action(f"确认归档全部 {len(records)} 条活动记忆？", default=False):
            return "已取消。"
        for record in records:
            service.archive(record.id)
        return f"✅ 已归档 {len(records)} 条记忆；未物理删除，可按 ID 恢复。"

    return (
        "用法: /memory status|list|search|inspect|doctor|add|replace|rollback|"
        "archive|restore|pin|unpin|migrate|clear [参数]"
    )


