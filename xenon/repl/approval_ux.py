"""Approval UX (R1-4: extracted from repl.py).

All interactive approval panels: plan approval, budget-checkpoint
continuation, legacy permission-gate delegation, unified tool-approval
panel and the terminal waiting spinner. Mixed into REPL as
``ApprovalUxMixin``.
"""

from __future__ import annotations

import sys
from typing import Any

from rich.panel import Panel
from rich.prompt import Prompt

from xenon.nodes.approval_policy import (
    APPROVAL_ALLOWED_ONCE,
    APPROVAL_CANCELLED,
    APPROVAL_REJECTED,
    APPROVAL_UNAVAILABLE,
    targets_outside_workspace,
)

logger = None  # 由 repl.py 回填（避免循环导入）
class _ConsoleProxy:
    """动态转发到 repl.console：测试 monkeypatch repl.console 时同步生效。"""

    def __getattr__(self, name):
        from xenon.repl.repl import console

        return getattr(console, name)

    def __enter__(self):
        from xenon.repl.repl import console

        return console.__enter__()

    def __exit__(self, *exc):
        from xenon.repl.repl import console

        return console.__exit__(*exc)


console: Any = _ConsoleProxy()


def _config():
    """惰性取 repl 的 get_config：测试 monkeypatch repl.get_config 时同步生效。"""

    from xenon.repl.repl import get_config

    return get_config()


class ApprovalUxMixin:
    def _confirm_checkpoint(self, reason: str) -> str:
        """预算检查点续跑审批：y=继续下一窗口 / n=停止并交付进度草稿。fail-closed。"""

        if not sys.stdin.isatty():
            return "declined"
        with self._permission_prompt_lock:
            callback = getattr(self, "_active_callback", None)
            if hasattr(callback, "suspend_for_prompt"):
                callback.suspend_for_prompt()
            with self._terminal_waiting("等待续跑审批"):
                console.print()
                console.print(
                    Panel(
                        str(reason)[:800],
                        title="⏳ 预算检查点",
                        border_style="cyan",
                        padding=(0, 1),
                    )
                )
                try:
                    choice = Prompt.ask(
                        "是否继续执行下一窗口？",
                        choices=["y", "n"],
                        default="n",
                        show_choices=True,
                        case_sensitive=False,
                    )
                except (KeyboardInterrupt, EOFError):
                    choice = "n"
        if hasattr(callback, "resume_after_prompt"):
            callback.resume_after_prompt("budget_checkpoint", {})
        if choice == "y":
            self._record_event("checkpoint/continued", reason=str(reason)[:200])
            return "approved"
        self._record_event("checkpoint/stopped", reason=str(reason)[:200])
        return "declined"


    def _confirm_plan(self, plan_text: str) -> dict:
        """计划审批面板：批准退出计划模式，驳回让模型修改后重提。"""

        if not sys.stdin.isatty():
            return {"approved": False, "feedback": "非交互环境无法批准计划"}
        with self._permission_prompt_lock:
            callback = getattr(self, "_active_callback", None)
            if hasattr(callback, "suspend_for_prompt"):
                callback.suspend_for_prompt()
            with self._terminal_waiting("等待计划审批"):
                console.print()
                console.print(
                    Panel(
                        str(plan_text)[:1500],
                        title="📋 计划待审批",
                        border_style="cyan",
                        padding=(0, 1),
                    )
                )
                try:
                    choice = Prompt.ask(
                        "选择",
                        choices=["y", "n"],
                        default="n",
                        show_choices=True,
                        case_sensitive=False,
                    )
                except (KeyboardInterrupt, EOFError):
                    return {"approved": False, "feedback": "用户取消"}
        if choice == "y":
            self._plan_mode_active = False
            self._record_event("plan/approved", plan=str(plan_text)[:200])
            console.print("[dim]· 计划已批准，退出计划模式开始执行[/dim]")
            if hasattr(callback, "resume_after_prompt"):
                callback.resume_after_prompt("submit_plan", {})
            return {"approved": True, "feedback": ""}
        console.print("[dim]· 计划被驳回[/dim]")
        if hasattr(callback, "resume_after_prompt"):
            callback.resume_after_prompt("submit_plan", {})
        return {"approved": False, "feedback": "用户驳回，请修改计划后重新提交"}


    def _confirm_tool(
        self, tool_name: str, params: dict, risk: str
    ) -> tuple[bool, str]:
        """旧权限门的确认回调：委托给统一的边界审批面板。

        PermissionGate.check 仍负责模式语义（PLAN 只读 / BYPASS 全允许 /
        风险路由），但所有需要询问的路径都走同一个面板（工作区策略 +
        TTL 规则 + allowed-once），不再有自己的 CRITICAL 弹窗。
        """

        if _config().interaction.assume_yes:
            return True, ""
        if not sys.stdin.isatty():
            return (
                False,
                "非交互环境无法确认危险操作；请显式使用 "
                "/permissions bypass 或设置 XENON_ASSUME_YES=1",
            )

        # 边界审批已经处理过（TTL 规则或本轮 allowed-once）时不再重复询问。
        if self._approval_rules.is_allowed(tool_name):
            return True, ""
        if tool_name in self._boundary_approved_once:
            return True, ""

        outcome = self._confirm_tool_approval(
            tool_name, params or {}, reason=f"风险等级 {risk}"
        )
        if outcome == APPROVAL_ALLOWED_ONCE:
            return True, ""
        if outcome == APPROVAL_CANCELLED:
            return False, "用户取消任务"
        return False, "用户拒绝"


    def _confirm_tool_approval(
        self,
        tool_name: str,
        params: dict,
        reason: str = "",
    ) -> str:
        """工具边界审批：工作区内写直接放行，越界写与命令询问。

        返回值是封闭结果词表：allowed-once / rejected / cancelled /
        unavailable；只有 allowed-once 是授权。非交互且无 assume_yes 时返回
        unavailable（fail-closed）。“a”登记带 TTL 的会话规则。
        """

        if self._approval_rules.is_allowed(tool_name):
            return APPROVAL_ALLOWED_ONCE
        if _config().interaction.assume_yes:
            return APPROVAL_ALLOWED_ONCE
        if not sys.stdin.isatty():
            logger.info("非交互环境：%s 未获边界审批", tool_name)
            return APPROVAL_UNAVAILABLE

        # 写工具在工作区内免问（用户决策 1）。命令类没有目标路径，会走到询问。
        if not targets_outside_workspace(tool_name, params, self._workspace_root()):
            return APPROVAL_ALLOWED_ONCE

        brief = str(params.get("command") or params.get("action") or "")[:120]
        diff_text = self._diff_preview(tool_name, params)
        with self._permission_prompt_lock:
            callback = getattr(self, "_active_callback", None)
            if hasattr(callback, "suspend_for_prompt"):
                callback.suspend_for_prompt()
            with self._terminal_waiting("等待工具授权"):
                console.print()
                body = (
                    f"模型请求执行 [bold]{tool_name}[/bold]，超出工作区或属于命令执行。\n"
                    + (f"命令: {brief}\n" if brief else "")
                    + f"原因：{reason or '工具边界策略'}"
                )
                if diff_text:
                    body += f"\n\n{diff_text}"
                console.print(
                    Panel(
                        body,
                        title="需要授权",
                        border_style="yellow",
                        padding=(0, 1),
                    )
                )
                try:
                    choice = Prompt.ask(
                        "选择",
                        choices=["y", "n", "a", "q"],
                        default="n",
                        show_choices=True,
                        case_sensitive=False,
                    )
                except (KeyboardInterrupt, EOFError):
                    return APPROVAL_CANCELLED
        if choice == "a":
            ttl = self._approval_rules.allow(tool_name)
            self._boundary_approved_once.add(tool_name)
            console.print(
                f"[dim]· 本会话 {ttl / 60:.0f} 分钟内自动允许 {tool_name}"
            )
            if hasattr(callback, "resume_after_prompt"):
                callback.resume_after_prompt(tool_name, params)
            return APPROVAL_ALLOWED_ONCE
        if choice == "y":
            self._boundary_approved_once.add(tool_name)
            console.print("[dim]· 已授权本次操作[/dim]")
            if hasattr(callback, "resume_after_prompt"):
                callback.resume_after_prompt(tool_name, params)
            return APPROVAL_ALLOWED_ONCE
        if choice == "q":
            console.print("[dim]· 已取消任务[/dim]")
            if hasattr(callback, "resume_after_prompt"):
                callback.resume_after_prompt(tool_name, params)
            return APPROVAL_CANCELLED
        console.print("[dim]· 未授权[/dim]")
        if hasattr(callback, "resume_after_prompt"):
            callback.resume_after_prompt(tool_name, params)
        return APPROVAL_REJECTED


    def _terminal_waiting(self, detail: str):
        """Return a waiting context, with a no-op fallback for partial REPLs."""
        from contextlib import nullcontext

        activity = getattr(self, "_terminal_activity", None)
        if activity is None:
            return nullcontext()
        return activity.waiting(detail)

