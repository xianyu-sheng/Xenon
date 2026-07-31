"""Cache and cost slash commands.

This group reports provider cache telemetry without making model calls.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from xenon.repl.command_registry import command_handler, register_command

# /cost — DeepSeek 缓存命中率 + 费用面板
# ══════════════════════════════════════════════════════════════

register_command(
    "/cost",
    "显示缓存命中率 + 费用明细（本地计算，不消耗 API）",
    "/cost [模型名]",
)


@command_handler("/cost")
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
        if tracker.total_calls > 0 and tracker.cache_reported_calls == 0:
            return (
                "[yellow]缓存命中率不可用（n/a）[/yellow]：当前厂商响应未提供 "
                "cache hit/miss 字段，不能解释为 0%。使用 [bold]/cache doctor[/bold] 查看详情。"
            )
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
# /cache — 请求级缓存状态、解释、历史与诊断
# ══════════════════════════════════════════════════════════════

register_command(
    "/cache",
    "解释缓存状态与命中原因（完全本地，不调用 API）",
    "/cache [status|explain|history [数量]|lanes|doctor|optimize --dry-run|--apply|--disable]",
)
register_command(
    "/fix-cache",
    "检查或启用安全的缓存优化（/cache optimize 的别名）",
    "/fix-cache [--dry-run|--apply|--disable]",
)

_CACHE_CAUSE_LABELS = {
    "cache_hit": "厂商确认缓存命中",
    "cold_family": "首次观察到这个缓存族",
    "warming": "缓存族仍在预热阶段",
    "provider_best_effort_miss": "稳定缓存族仍未命中（服务端缓存为 best-effort）",
    "cache_fields_unavailable": "厂商未返回缓存字段",
    "model_switch": "模型发生切换",
    "engine_switch": "思考引擎发生切换",
    "phase_switch": "引擎调用阶段发生切换",
    "toolset_changed": "工具或请求契约发生变化",
    "project_changed": "项目上下文发生变化",
    "context_compacted": "上下文压缩、清空或撤销后进入新代次",
    "stable_prefix_changed": "稳定提示前缀发生变化",
    "history_rewritten": "请求历史不是旧轨道的追加，已自动分叉新代次",
}

_CACHE_STATE_LABELS = {
    "cold": "COLD",
    "warming": "WARMING",
    "warm": "WARM",
    "miss": "MISS",
    "unavailable": "N/A",
}


def _cache_tracker(session_state: dict | None):
    repl = session_state.get("_repl") if session_state else None
    return getattr(repl, "_cache_tracker", None) if repl else None


def _cache_status(tracker) -> str:
    event = tracker.latest_event
    if event is None:
        return (
            "[bold]缓存状态: COLD[/bold]\n"
            "  尚无模型响应。cold 表示没有观测样本，不是 0% 命中。\n"
            "  Xenon 只读取厂商 usage，不会为预热额外发起付费请求。"
        )
    state = _CACHE_STATE_LABELS.get(event["state"], event["state"].upper())
    total = tracker.cache_hits + tracker.cache_misses
    rate = f"{tracker.cache_hit_rate:.1%}" if total else "n/a"
    efficiency = event.get("prefix_efficiency")
    efficiency_text = f"{efficiency:.1%}" if efficiency is not None else "n/a"
    return "\n".join([
        f"[bold]缓存状态: {state}[/bold]",
        f"  实际命中率: [bold]{rate}[/bold]  ·  字段覆盖率: {tracker.cache_field_coverage:.0%}",
        f"  最近请求: {event['model_id']} · {event['engine']}/{event['phase']}",
        f"  实际 token: hit {event['cache_hit_tokens']:,} · miss {event['cache_miss_tokens']:,}",
        f"  前缀效率: {efficiency_text}  ·  缓存族: {event['cache_family'][:12]}",
        f"  缓存轨道: {(event.get('cache_lane') or '未跟踪')[:20]}"
        f" · 代次 {int(event.get('lane_generation', 0))}"
        f" · 可复用约 {int(event.get('lane_reusable_tokens', 0)):,} tokens",
        f"  证据来源: 厂商 usage 字段  ·  本地累计 {tracker.total_calls} 次请求",
    ])


def _cache_explain(tracker) -> str:
    event = tracker.latest_event
    if event is None:
        return _cache_status(tracker)
    cause = event.get("cause", "")
    lines = [
        f"[bold]最近一次缓存判定: {_CACHE_STATE_LABELS.get(event['state'], event['state'])}[/bold]",
        f"  原因: {_CACHE_CAUSE_LABELS.get(cause, cause or '未知')}",
        f"  请求族: {event['model_id']} · {event['engine']}/{event['phase']} · 第 {event['family_call']} 次",
    ]
    if event["cache_fields_present"]:
        lines.append(
            f"  直接证据: API 返回 hit={event['cache_hit_tokens']:,}, "
            f"miss={event['cache_miss_tokens']:,}, 覆盖率={event['cache_field_coverage']:.0%}"
        )
    else:
        lines.append("  直接证据: API 没有返回 cache hit/miss 字段，因此显示 n/a，而不是 0%。")
    if cause not in {"cache_hit", "cache_fields_unavailable"}:
        lines.append("  说明: 原因由本地 Manifest 差异推断；命中 token 始终以厂商 usage 为准。")
    return "\n".join(lines)


def _cache_lanes(repl) -> str:
    context = getattr(repl, "ctx_mgr", None) if repl else None
    registry = getattr(context, "prompt_lanes", None)
    if registry is None:
        return "[dim]PromptLaneRegistry 未初始化。[/dim]"
    snapshots = registry.snapshots()
    if not snapshots:
        return (
            "[bold]Cache Rails[/bold]\n"
            "  尚无模型轨道；第一次实际模型调用后会自动创建。"
        )
    active = [item for item in snapshots if item.get("active")]
    archived = [item for item in snapshots if not item.get("active")]
    lines = [
        "[bold]Cache Rails · 模型提示词轨道[/bold]",
        f"  活跃 {len(active)} · 已归档 {len(archived)} · 上下文 epoch {context.cache_epoch}",
    ]
    for lane in sorted(
        active,
        key=lambda item: float(item.get("last_used_at", 0.0)),
        reverse=True,
    ):
        lines.append(
            f"  ● {lane['model_id']} · {lane['engine']}/{lane['phase']} "
            f"· 请求 {lane['request_count']} · 约 {lane['estimated_prompt_tokens']:,} tokens "
            f"· cursor {lane['last_event_id']} · {lane['lane_id']}"
        )
    if archived:
        rewrites = sum(
            1 for item in archived if item.get("fork_reason") == "history_rewritten"
        )
        lines.append(
            f"  历史分叉: {rewrites} 次（压缩/撤销等正常 epoch 切换不计为分叉）"
        )
    lines.append("  说明：轨道只保存哈希与计数；真实命中仍以厂商 usage 为准。")
    return "\n".join(lines)


def _cache_history(tracker, limit: int) -> str:
    events = tracker.stored_events(limit)
    if not events:
        return "[dim]暂无缓存历史。[/dim]"
    lines = ["[bold]最近缓存请求（仅哈希与计数）[/bold]"]
    for event in events:
        stamp = datetime.fromtimestamp(float(event.get("timestamp", 0))).strftime("%m-%d %H:%M:%S")
        state = _CACHE_STATE_LABELS.get(str(event.get("state", "")), "?")
        cause = _CACHE_CAUSE_LABELS.get(str(event.get("cause", "")), str(event.get("cause", "")))
        lines.append(
            f"  {stamp}  {state:<7}  {event.get('model_id', '?')} "
            f"· {event.get('engine', '?')}/{event.get('phase', '?')} "
            f"· {int(event.get('cache_hit_tokens', 0)):,}/{int(event.get('cache_miss_tokens', 0)):,} "
            f"· {cause}"
        )
    return "\n".join(lines)


def _cache_doctor(tracker) -> str:
    icons = {"ok": "✅", "warn": "⚠️", "info": "ℹ️"}
    lines = ["[bold]Cache Doctor[/bold]"]
    for check in tracker.diagnostics():
        lines.append(
            f"  {icons.get(check['level'], '·')} [bold]{check['name']}[/bold]: {check['detail']}"
        )
    lines.append("\n  建议先看 [bold]/cache explain[/bold]；缓存是服务端 best-effort，Xenon 不会制造付费预热流量。")
    return "\n".join(lines)


def _cache_optimize(tracker, repl, mode: str) -> str:
    """Inspect or toggle safe cache-aware routing without model calls."""
    normalized = mode.strip().lower() or "--dry-run"
    if normalized not in {"--dry-run", "--apply", "--disable", "--off"}:
        return "用法: /cache optimize [--dry-run|--apply|--disable]"

    if normalized == "--apply":
        try:
            persisted = tracker.set_cache_affinity_enabled(True, persist=True)
        except OSError as exc:
            return f"❌ 无法保存缓存优化设置，原设置保持不变: {exc}"
        action = "✅ 已启用同能力模型缓存亲和"
        persistence = "已持久化" if persisted else "仅当前会话"
    elif normalized in {"--disable", "--off"}:
        try:
            persisted = tracker.set_cache_affinity_enabled(False, persist=True)
        except OSError as exc:
            return f"❌ 无法保存缓存优化设置，原设置保持不变: {exc}"
        action = "⏸️ 已关闭同能力模型缓存亲和"
        persistence = "已持久化" if persisted else "仅当前会话"
    else:
        action = "🔎 Dry run：没有修改任何设置"
        persistence = "只读检查"

    enabled = tracker.cache_affinity_enabled
    lines = [
        "[bold]Cache Optimize[/bold]",
        f"  {action}（{persistence}）",
        "",
        "  ✅ 五层 Prompt Compiler：已启用",
        "  ✅ 工具 schema 确定性排序：已启用",
        f"  {'✅' if enabled else '⏸️'} 同能力模型缓存亲和：{'开启' if enabled else '关闭'}",
        "  🛡️ 硬边界：只重排同 tier、健康且基础分差 ≤ 0.25 的模型",
        "  🛡️ 优先级：显式 -m / 会话锁 / 能力与健康分 > 缓存信号",
        "  🚫 不会改写 Prompt、改变工具协议或制造付费预热请求",
    ]
    settings_path = tracker.cache_settings_path
    if settings_path is not None:
        lines.append(f"  🔒 本地设置：{settings_path}（不含 Prompt 或凭据）")

    decision = getattr(repl, "auto_router", None) if repl else None
    decision = getattr(decision, "last_cache_affinity_decision", None)
    if isinstance(decision, dict) and decision.get("reason") != "not_evaluated":
        before = decision.get("before") or []
        after = decision.get("after") or []
        lines.append(
            "  最近路由："
            f"{decision.get('reason', 'unknown')} · "
            f"{' → '.join(before[:1]) or '无候选'}"
            f"{' → ' + str(after[0]) if after and after != before else ''}"
        )

    warnings = [
        check for check in tracker.diagnostics()
        if check.get("level") == "warn"
    ]
    if warnings:
        lines.append("\n  需要人工判断（不会自动修改）：")
        for check in warnings:
            lines.append(f"    ⚠️ {check['name']}：{check['detail']}")
    elif not tracker.latest_event:
        lines.append("\n  ℹ️ 尚无响应样本；当前不能证明命中或未命中。")
    else:
        lines.append("\n  ✅ 当前未发现需要修改的稳定前缀问题。")
    return "\n".join(lines)


@command_handler("/cache")
def _cmd_cache(*, args: str = "", session_state: dict = None, **kwargs: Any) -> str:
    tracker = _cache_tracker(session_state)
    if tracker is None:
        return "[dim]CacheTracker 未初始化。[/dim]"
    parts = args.strip().split()
    action = parts[0].lower() if parts else "status"
    if action == "status":
        return _cache_status(tracker)
    if action == "explain":
        return _cache_explain(tracker)
    if action == "doctor":
        return _cache_doctor(tracker)
    if action == "lanes":
        repl = session_state.get("_repl") if session_state else None
        return _cache_lanes(repl)
    if action == "optimize":
        mode = parts[1] if len(parts) > 1 else "--dry-run"
        repl = session_state.get("_repl") if session_state else None
        return _cache_optimize(tracker, repl, mode)
    if action == "history":
        try:
            limit = min(100, max(1, int(parts[1]))) if len(parts) > 1 else 10
        except ValueError:
            return "用法: /cache history [1-100]"
        return _cache_history(tracker, limit)
    return (
        "用法: /cache [status|explain|history [数量]|lanes|doctor|"
        "optimize --dry-run|--apply|--disable]"
    )


@command_handler("/fix-cache")
def _cmd_fix_cache(*, args: str = "", session_state: dict = None, **kwargs: Any) -> str:
    """Compatibility entry point backed by the real cache optimizer."""
    mode = args.strip() or "--dry-run"
    return _cmd_cache(args=f"optimize {mode}", session_state=session_state)



