"""Plan-mode slash command.

Plan mode is **logged soft guidance**: while active, requests get a planning
instruction and write/execute contracts are converted into pending approval
gates instead of running.  It never replaces the workspace/approval policy —
those stay independent.

Kernel plan approval is a REPL decision over an immutable ``planned`` ledger.
It records a session event (``plan/approved`` / ``plan/rejected``) and never
appends to or rewrites the kernel ledger.
"""

from __future__ import annotations

from typing import Any

from xenon.repl.command_registry import command_handler, register_command

register_command(
    "/plan",
    "切换计划模式；审批 kernel 待确认计划",
    "/plan [on|off|status|approve|reject]",
)


@command_handler("/plan")
def _cmd_plan(*, args: str, session_state: dict, **kwargs: Any) -> str:
    repl = session_state.get("_repl")
    if repl is None:
        return "❌ /plan 需要交互会话"

    arg = args.strip().lower()
    if arg in {"on", "开启", "打开"}:
        repl._plan_mode_active = True
    elif arg in {"off", "关闭", "退出"}:
        repl._plan_mode_active = False
    elif arg in {"approve", "批准", "通过"}:
        return _decide_plan(repl, approve=True)
    elif arg in {"reject", "驳回", "拒绝"}:
        return _decide_plan(repl, approve=False)
    elif arg in {"", "status", "状态"}:
        return _plan_status(repl)
    else:
        return "用法: /plan [on|off|status|approve|reject]"

    repl._record_event("plan_mode/set", active=bool(repl._plan_mode_active))
    state = "开启" if repl._plan_mode_active else "关闭"
    return f"✅ 计划模式已{state}"


def _session_events(repl: Any) -> list[Any]:
    log = getattr(repl, "_session_events", None)
    reader = getattr(log, "read", None)
    if not callable(reader):
        return []
    try:
        return list(reader())
    except Exception:  # noqa: BLE001 — approval UI must not crash on IO
        return []


def _plan_status(repl: Any) -> str:
    from xenon.repl.kernel_evidence import (
        current_kernel_evidence,
        plan_approval,
        plan_lines,
        planned_plan,
    )

    state = "开启" if repl._plan_mode_active else "关闭"
    lines = [f"📋 计划模式：{state}（/plan on|off 切换）"]
    pointer = getattr(repl, "_last_kernel_ledger", None)
    evidence = current_kernel_evidence(repl)
    if evidence is not None:
        lines.append("")
        lines.extend(plan_lines(evidence))
        if not evidence.integrity.valid:
            lines.append("⚠️ 链无效：以上计划仅供取证，不可作为可信交付。")
            return "\n".join(lines)
        extracted = planned_plan(evidence)
        if extracted is not None:
            decision = plan_approval(_session_events(repl), evidence.task_id)
            if decision is None:
                lines.append("⏸ 计划待确认：/plan approve 或 /plan reject")
            elif decision["status"] == "approved":
                lines.append("✅ 计划已批准（尚未执行）")
            else:
                lines.append("❌ 计划已驳回（未执行）")
    elif pointer:
        lines.append("")
        lines.append(
            "⚠️ 当前 kernel ledger 不可读；不退回 legacy 目标投影。"
        )
    return "\n".join(lines)


def _decide_plan(repl: Any, *, approve: bool) -> str:
    from xenon.repl.kernel_evidence import (
        current_kernel_evidence,
        plan_approval,
        planned_plan,
    )

    evidence = current_kernel_evidence(repl)
    if evidence is None:
        pointer = getattr(repl, "_last_kernel_ledger", None)
        if pointer:
            return "❌ 当前 kernel ledger 不可读；审批被拒绝。"
        return "❌ 当前没有可审批的 kernel 计划。"
    extracted = planned_plan(evidence)
    if extracted is None:
        if not evidence.integrity.valid:
            return "❌ kernel ledger 链无效；审批被拒绝。"
        phase = str((evidence.result or {}).get("phase") or "")
        return f"❌ 当前 kernel ledger 不是待确认计划（phase={phase or '未知'}）。"

    decision = plan_approval(_session_events(repl), evidence.task_id)
    if decision is not None:
        if decision["status"] == "approved":
            return "✅ 该计划已批准并绑定；无需重复审批。"
        return "❌ 该计划已驳回；如需重来请重新生成计划。"

    record = getattr(repl, "_record_event", None)
    if not callable(record):
        return "❌ 当前会话没有事件记录通道；审批被拒绝。"
    event_type = "plan/approved" if approve else "plan/rejected"
    event_id = record(
        event_type,
        task_id=evidence.task_id,
        ledger=str(evidence.ledger_path),
        plan_digest=extracted.digest,
        step_count=len(extracted.plan.steps),
    )
    if not event_id:
        return "❌ 审批事实写入失败；审批被拒绝。"
    if approve:
        repl._plan_mode_active = False
        record("plan_mode/set", active=False)
        approved_text = (
            "✅ 计划已批准并绑定（尚未执行）。" + chr(10) +
            f"   task: {evidence.task_id} · steps: {len(extracted.plan.steps)}"
        )
        make_callback = getattr(repl, "_make_callback", None)
        registry = getattr(repl, "registry", None)
        if not callable(make_callback) or registry is None:
            return approved_text
        from xenon.repl.runtime_adapter import resume_kernel_plan

        state = evidence.task_state
        objective = str(state.get("objective") or "")
        user_objective = str(
            state.get("user_objective") or state.get("objective") or ""
        )
        try:
            run = resume_kernel_plan(
                repl,
                task_id=evidence.task_id,
                plan=extracted.plan,
                plan_digest=extracted.digest,
                approval_id=event_id,
                objective=objective,
                user_objective=user_objective,
            )
        except Exception as exc:  # noqa: BLE001 — approval command must report
            return (
                "✅ 审批已记录，但计划执行启动失败：" + chr(10) +
                f"   {str(exc)[:300]}"
            )
        if run is None:
            return "✅ 审批已记录，但计划执行失败；未标记为已交付。"
        return (
            "✅ 计划已批准并执行。" + chr(10) +
            f"   phase: {run.result.phase.value} · "
            f"accepted: {run.result.accepted} · "
            f"commit: {(run.result.finalize_sha or '-')[:12]}"
        )
    mode_state = "保持开启" if repl._plan_mode_active else "保持关闭"
    return f"❌ 计划已驳回；未执行，计划模式{mode_state}。"
