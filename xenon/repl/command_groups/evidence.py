"""``/evidence`` — L0 证据层自证视图。

默认是**给人看的结构化摘要**：会话概览 + 链核验一行 + 每回合一段
（意图 / 引擎 / 工具统计 / 判定 / 回复预览）。
``/evidence raw`` 保留**给机器/审计员看**的逐条哈希明细。
``/evidence all``（或 ``global``）列出全局会话目录；``/evidence <会话ID>``
查看历史会话。所有核验都是确定性的（重算哈希 + 前驱链接），无 LLM 调用。
"""

from __future__ import annotations

from typing import Any

from xenon.repl.command_registry import command_handler, register_command

register_command(
    "/evidence",
    "查看 L0 证据账本（结构化会话摘要 + 哈希链自证）",
    "/evidence [all|global|<会话ID>] [raw]",
)

_CHAIN_LABELS = {
    "ok": "✅ 完整（全部事件可验）",
    "partial": "⚠️ 部分（旧格式/混合模式/缺密钥的 keyed 事件）",
    "legacy": "⚠️ 旧格式（无哈希字段，无法自证）",
    "broken": "❌ 断裂（哈希链不连续，可能被修改）",
    "empty": "（空）",
}

_CHAIN_ICONS = {"ok": "🟢", "partial": "🟡", "legacy": "🟡", "broken": "🔴", "empty": "⚪"}

_VERDICT_LABELS = {
    "passed": "✅ 发布成功",
    "degraded": "🟡 降级发布（重试耗尽，附核验清单）",
    "fused": "❌ 熔断未完成（同因失败，说“继续”可续接）",
    "interrupted": "⏸ 中断（说“继续”可续接）",
    "failed-retried": "🔁 重试后仍未过校验",
    "running": "⏳ 进行中",
    "pending-approval": "⏸ 待用户确认",
}


def _fmt_head(hash_hex: str) -> str:
    return f"{hash_hex[:12]}…" if hash_hex else "-"


def _chain_line(report: Any) -> str:
    """一行链自证：状态 + 可验计数 + head + 模式。"""
    label = _CHAIN_LABELS.get(report.status, report.status)
    head = _fmt_head(report.head_hash or "")
    return f"链: {label} · {report.verified}/{report.total} 条可验 · head {head} · {report.mode}"


def _tool_line(tool_events: list[dict]) -> str:
    if not tool_events:
        return "🔧 无工具调用"
    failed = sum(1 for e in tool_events if not e.get("success"))
    names: dict[str, int] = {}
    for e in tool_events:
        name = str(e.get("tool") or "?")
        names[name] = names.get(name, 0) + 1
    parts = " · ".join(f"{k} ×{v}" for k, v in sorted(names.items()))
    state = f" · 失败 {failed}" if failed else " · 0 失败"
    return f"🔧 {len(tool_events)} 次工具（{parts}）{state}"


def _human_view(events: list[dict], title: str, path: Any) -> str:
    """给人看：会话概览 + 每回合一段结构化摘要。"""
    from xenon.session.events import (
        HMAC_KEY_ENV,
        evidence_hmac_key,
        verify_event_chain,
    )

    report = verify_event_chain(events, key=evidence_hmac_key())
    lines = [
        f"🔗 L0 证据账本 · {title}",
        f"文件: {path}",
        "",
        _chain_line(report),
    ]
    if report.broken_at is not None:
        lines.append(f"   ⚠️ 首次断裂: 第 {report.broken_at} 条（其后不可信）")
    if report.keyed_pending:
        lines.append(
            f"   ⚠️ {report.keyed_pending} 条 keyed 事件待核验"
            f"（设置 {HMAC_KEY_ENV} 后查看）"
        )

    # 会话级目标（如有）
    goals = [e for e in events if e.get("type") == "goal/opened"]
    if goals:
        objective = str(goals[-1].get("objective") or "").strip()
        if objective:
            lines.append(f"🎯 目标: {objective[:80]}")

    # 按回合分组（turn/user 开启新块）
    turns: list[dict] = []
    current: dict = {}
    for event in events:
        etype = event.get("type")
        if etype == "turn/user":
            current = {"user": event, "tools": [], "verdict": None, "reply": ""}
            turns.append(current)
            continue
        if not current:
            continue
        if etype == "tool/result":
            current["tools"].append(event)
        elif etype == "turn/verdict":
            current["verdict"] = event
        elif etype == "turn/assistant":
            current["reply"] = str(event.get("preview") or "").replace("\n", " ")

    if turns:
        lines.append("")
        lines.append("会话回合:")
    for i, turn in enumerate(turns, 1):
        user = turn["user"]
        text = str(user.get("text") or "").replace("\n", " ").strip()
        intent = str(user.get("intent") or "—")
        verdict = turn["verdict"]
        if verdict is not None:
            status = str(verdict.get("status") or "passed")
            verdict_label = _VERDICT_LABELS.get(status, status)
            reasons = list(verdict.get("reasons") or [])
            if status == "passed" and reasons:
                verdict_label = f"🟡 发布成功 · 附 {len(reasons)} 条核验提醒"
            elif reasons and status not in {"passed"}:
                verdict_label += f"（{reasons[0][:40]}）"
            engine = str(verdict.get("engine") or "—")
            steps = verdict.get("steps")
            head = (
                f"📌 回合 {i} · 🎯 {intent} · {engine}"
                f" · 步数 {steps}" if steps is not None else f"📌 回合 {i} · 🎯 {intent} · {engine}"
            )
        else:
            verdict_label = "（无判定记录）"
            head = f"📌 回合 {i} · 🎯 {intent}"
        lines.append(head)
        lines.append(f"   💬 {text[:90]}")
        lines.append(f"   {_tool_line(turn['tools'])}")
        lines.append(f"   🏁 {verdict_label}")
        if turn["reply"]:
            lines.append(f"   📤 {turn['reply'][:100]}")
    lines.append("")
    lines.append("链核验：逐条重算 sha256(事件正文) 并校验 prev 链接，确定性、无 LLM；")
    lines.append("明细: /evidence raw（逐条事件 + 哈希）")
    return "\n".join(lines)


def _machine_view(events: list[dict], title: str, path: Any) -> str:
    """给机器/审计员看：事件类型统计 + 逐条哈希。"""
    from xenon.session.events import (
        HMAC_KEY_ENV,
        evidence_hmac_key,
        verify_event_chain,
    )

    report = verify_event_chain(events, key=evidence_hmac_key())
    counts: dict[str, int] = {}
    for event in events:
        etype = str(event.get("type") or "?")
        counts[etype] = counts.get(etype, 0) + 1
    lines = [
        f"🔗 L0 证据账本（原始） · {title}",
        f"文件: {path}",
        "",
        f"事件总数 {report.total}",
        _CHAIN_LABELS.get(report.status, report.status),
        f"链模式: {report.mode}",
    ]
    if report.verified:
        lines.append(f"可验事件 {report.verified}（head {_fmt_head(report.head_hash or '')}）")
    if report.keyed_pending:
        lines.append(
            f"⚠️ {report.keyed_pending} 条 keyed 事件无法核验（缺 {HMAC_KEY_ENV}）"
        )
    if report.legacy:
        lines.append(f"旧格式事件 {report.legacy}（在哈希链之前，不可自证）")
    if report.broken_at is not None:
        lines.append(f"首次断裂: 第 {report.broken_at} 条")
    for etype, n in sorted(counts.items()):
        lines.append(f"{etype} × {n}")
    if events:
        lines.append("")
        lines.append("逐条事件（自尾向前）:")
        for event in reversed(events[-12:]):
            h = str(event.get("h", "（旧格式无哈希）"))
            lines.append(
                f"  {event.get('type')}  h={h[:12]}  "
                f"prev={str(event.get('prev', '-'))[:12]}"
            )
    return "\n".join(lines)


@command_handler("/evidence")
def _cmd_evidence(*, args: str, session_state: dict, **kwargs: Any) -> str:
    from xenon.session.events import (
        SessionEventLog,
        evidence_hmac_key,
        session_events_dir,
        verify_event_chain,
    )

    repl = session_state.get("_repl")
    parts = (args or "").strip().split()
    arg = parts[0].lower() if parts else ""
    raw = len(parts) >= 2 and parts[-1].lower() in {"raw", "--raw"}
    if arg in {"raw", "--raw"}:
        arg, raw = "", True

    # ── 全局视图 ──
    if arg in {"all", "global", "--global"}:
        directory = session_events_dir()
        if not directory.exists():
            return "（会话事件目录不存在，暂无证据账本）"
        files = sorted(
            directory.glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not files:
            return "（会话事件目录为空，暂无证据账本）"
        lines = [f"📒 全局证据账本（{directory}）", ""]
        for path in files[:20]:
            events = SessionEventLog(path.stem, directory=directory).read()
            report = verify_event_chain(events, key=evidence_hmac_key())
            icon = "🔒" if (report.status == "ok" and report.keyed) else _CHAIN_ICONS[report.status]
            turns = sum(1 for e in events if e.get("type") == "turn/user")
            lines.append(
                f"{icon} {path.stem}  ·  事件 {report.total} / 轮次 {turns} / "
                f"可验 {report.verified}  [{'完整' if report.status == 'ok' else report.status}]"
            )
        if len(files) > 20:
            lines.append(
                f"\n… 另有 {len(files) - 20} 个会话；/evidence <会话ID> 查看详情"
            )
        lines.append("\n默认 /evidence 查看当前会话摘要；/evidence raw 看逐条哈希。")
        return "\n".join(lines)

    # ── 指定会话 / 当前会话 ──
    log = getattr(repl, "_session_events", None)
    current = arg in {"", "self", "current", "this"}
    if arg and not current:
        log = SessionEventLog(arg, directory=session_events_dir())
    if log is None:
        return "（无会话事件日志）"

    title = "当前会话" if current else f"会话 {log.session_id}"
    events = log.read()
    view = _machine_view if raw else _human_view
    return view(events, title, log.path)
