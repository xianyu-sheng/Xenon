"""Approval UX (R1-4: extracted from repl.py).

All interactive approval panels: plan approval, budget-checkpoint
continuation, legacy permission-gate delegation, unified tool-approval
panel and the terminal waiting spinner. Mixed into REPL as
``ApprovalUxMixin``.
"""

from __future__ import annotations

import sys
from typing import Any

from rich.markup import escape
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


    def _approval_key(self, tool_name: str, params: dict | None) -> tuple:
        """H-7：allowed-once 绑定 (工具, 目标路径集合)，而非工具名。

        同一轮里对同一工具的不同路径仍需分别询问；同工具同路径才免问。
        """
        from xenon.nodes.approval_policy import write_targets_for

        try:
            targets = tuple(
                sorted(str(p) for p in (write_targets_for(tool_name, params or {}) or []))
            )
        except Exception:  # noqa: BLE001 — 目标解析失败按“无目标”处理，保守询问
            targets = ()
        return (tool_name, targets)

    def _confirm_tool(
        self, tool_name: str, params: dict, risk: str, *, from_child: bool = False
    ) -> tuple[bool, str]:
        """旧权限门的确认回调：委托给统一的边界审批面板。

        PermissionGate.check 仍负责模式语义（PLAN 只读 / BYPASS 全允许 /
        风险路由），但所有需要询问的路径都走同一个面板（工作区策略 +
        TTL 规则 + allowed-once），不再有自己的 CRITICAL 弹窗。

        ``from_child``（#373）：回答子任务经 broker 转发的请求时，本次回答
        只作用于这一次调用——不写 TTL 规则、不登记 allowed-once、不产生
        会话授权。子任务只能继承父级已有的授权，不能自己积累。
        """

        self._last_approval_scope = ""
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
        if self._approval_key(tool_name, params) in self._boundary_approved_once:
            return True, ""

        outcome = self._confirm_tool_approval(
            tool_name, params or {}, reason=f"风险等级 {risk}", from_child=from_child
        )
        if outcome == APPROVAL_ALLOWED_ONCE:
            # What the human chose to remember: "" (once), "session", "always"
            # (a rule file) or "scoped" (just this command).
            return True, ""
        if outcome == APPROVAL_CANCELLED:
            return False, "用户取消任务"
        return False, "用户拒绝"


    def _confirm_tool_approval(
        self,
        tool_name: str,
        params: dict,
        reason: str = "",
        *,
        from_child: bool = False,
    ) -> str:
        """工具边界审批：工作区内写直接放行，越界写与命令询问。

        返回值是封闭结果词表：allowed-once / rejected / cancelled /
        unavailable；只有 allowed-once 是授权。非交互且无 assume_yes 时返回
        unavailable（fail-closed）。“a”登记带 TTL 的会话规则。

        ``from_child``（#373）：子任务经 broker 转发的请求只给 y/n/q 三个
        选择，任何“允许”都只作用于当前这一次调用，不写入任何记忆（TTL
        规则、allowed-once、会话授权）。父级已有的授权仍然先命中上面的
        短路径——那就是“继承”。
        """

        self._last_approval_scope = ""
        if self._approval_rules.is_allowed(tool_name):
            return APPROVAL_ALLOWED_ONCE
        if _config().interaction.assume_yes:
            return APPROVAL_ALLOWED_ONCE
        if not sys.stdin.isatty():
            logger.info("非交互环境：%s 未获边界审批", tool_name)
            return APPROVAL_UNAVAILABLE

        # 写工具在工作区内免问（用户决策 1）。执行级工具（command /
        # spawn_agent / 执行型 mcp_call）无论参数如何都必须询问——
        # 模型带一个工作区内的 file_path 不能绕过审批面板（H-2 修复）。
        from xenon.nodes.tool_classify import required_execution_level

        if (
            required_execution_level(tool_name, params) < 3
            and not targets_outside_workspace(tool_name, params, self._workspace_root())
        ):
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
                advisory_items = self.agent_context.get_security_advisories()
                if advisory_items:
                    sources = sorted(
                        {
                            f"{item['provenance']['source_type']} · "
                            f"{item['provenance']['source_ref']}"
                            for item in advisory_items
                        }
                    )[:3]
                    rules = sorted(
                        {
                            str(item["advisory"].get("rule_id") or "")
                            for item in advisory_items
                            if item.get("advisory", {}).get("rule_id")
                        }
                    )[:5]
                    body += (
                        "\n\n⚠️ 该动作可能受不可信内容影响"
                        f"\n来源：{escape('; '.join(sources))}"
                        f"\n命中规则：{escape(', '.join(rules))}"
                    )
                if diff_text:
                    body += f"\n\n{diff_text}"
                console.print(
                    Panel(
                        body
                        + chr(10) + chr(10)
                        + "[dim]y=只这次  a=本会话都允许  "
                        "A=永远允许（写入规则文件）  s=只允许这条命令  "
                        "n=拒绝  q=取消任务[/dim]",
                        title="需要授权",
                        border_style="yellow",
                        padding=(0, 1),
                    )
                )
                try:
                    choice = self._ask_approval_choice(from_child=from_child)
                except (KeyboardInterrupt, EOFError):
                    return APPROVAL_CANCELLED
        if choice == "a":
            if from_child:
                # A child request has no y/a/A/s/q prompt; defensive only.
                return APPROVAL_REJECTED
            ttl = self._approval_rules.allow(tool_name)
            self._boundary_approved_once.add(self._approval_key(tool_name, params))
            console.print(
                f"[dim]· 本会话 {ttl / 60:.0f} 分钟内自动允许 {tool_name}"
            )
            if hasattr(callback, "resume_after_prompt"):
                callback.resume_after_prompt(tool_name, params)
            self._last_approval_scope = "session"
            return APPROVAL_ALLOWED_ONCE
        if choice == "A":
            if from_child:
                return APPROVAL_REJECTED
            # "Always" writes a rule, exactly like Claude Code's "don't ask
            # again": the decision becomes a file the human can read, review and
            # commit — not an invisible flag.
            selector = self._persist_permission_rule(tool_name, params)
            self._boundary_approved_once.add(self._approval_key(tool_name, params))
            if hasattr(callback, "resume_after_prompt"):
                callback.resume_after_prompt(tool_name, params)
            console.print(f"[dim]· 已写入规则 {selector}")
            self._last_approval_scope = "always"
            return APPROVAL_ALLOWED_ONCE
        if choice == "s":
            if from_child:
                return APPROVAL_REJECTED
            self._boundary_approved_once.add(self._approval_key(tool_name, params))
            if hasattr(callback, "resume_after_prompt"):
                callback.resume_after_prompt(tool_name, params)
            console.print("[dim]· 已只允许这条命令（本会话）[/dim]")
            self._last_approval_scope = "scoped"
            return APPROVAL_ALLOWED_ONCE
        if choice == "y":
            if not from_child:
                # One-shot within this round.  A child's answer must not even
                # do that: it applies to this call only, nothing more.
                self._boundary_approved_once.add(self._approval_key(tool_name, params))
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

    def _persist_permission_rule(self, tool_name: str, params: dict) -> str:
        """Write an ``allow`` rule for exactly what was asked, and return it.

        Scope is the literal target (the command, or the path), never the whole
        capability: "always allow this command" is what the human saw.  A broader
        rule is a deliberate act of editing the file.
        """

        from xenon.kernel.permission_rules import (
            ALLOW,
            PermissionRule,
            default_rule_paths,
            save_rule,
        )
        from xenon.repl.permission_map import capability_for_tool

        capability = capability_for_tool(tool_name)
        target = str(
            params.get("command")
            or params.get("file_path")
            or params.get("path")
            or ""
        ).strip()
        if capability is None or not target:
            # Nothing to scope to: refuse to invent a rule that authorizes more
            # than the human was shown.
            return "(unsupported: no capability or target to scope)"
        rule = PermissionRule(
            decision=ALLOW,
            capability=capability,
            pattern=target,
            source="user",
            reason=f"approved via {tool_name}",
        )
        paths = dict(default_rule_paths(self._workspace_root()))
        path = paths["user"]
        try:
            save_rule(rule, path)
        except OSError as exc:
            return f"(could not write {path}: {exc})"
        return rule.selector()


    def _ask_approval_choice(self, default: str = "n", *, from_child: bool = False) -> str:
        """Read y/n/a/q without fighting prompt_toolkit for stdin.

        The REPL owns the terminal through prompt_toolkit.  Asking with
        rich's ``Prompt.ask`` (builtin ``input()``) while that session is
        alive makes both readers compete for the same handle: on Windows the
        keystrokes get swallowed and the prompt appears to hang until Ctrl-C.
        So when a prompt_toolkit session exists we ask through it — it owns
        the reader either way — and only fall back to ``Prompt.ask`` when
        there is none (headless, tests, exotic terminals).

        ``from_child`` (a brokered child request) narrows the answer set to
        y/n/q: a delegated model may only ask, it may not be remembered.
        """

        allowed = {"y", "n", "a", "A", "s", "q"}
        if from_child:
            allowed = {"y", "n", "q"}
        question = "选择 [y/n/a/A/s/q] (n): " if not from_child else "选择 [y/n/q] (n): "
        session = getattr(self, "_pt_session", None)
        # Only inside a kernel turn, where the REPL's prompt has already
        # returned and the session is the live owner of the terminal.  Outside
        # one (slash commands, tests, headless) ``input()`` is safe and
        # cheaper, so we keep the historic path.
        in_turn = getattr(self, "_active_runtime", None) is not None
        if session is not None and in_turn:
            while True:
                # ``default=`` in prompt_toolkit *prefills the edit buffer*, it
                # is not a placeholder: a buffer pre-filled with "n" turns a
                # typed "y" into "ny", which is invalid, so every answer
                # re-prompts and the panel looks hung (#395).  The "(n)" hint is
                # already in the question; empty input means the default.
                answer = session.prompt(question).strip().lower()
                if answer == "":
                    answer = default
                if answer in allowed:
                    return answer
                hint = "y / n / q" if from_child else "y / n / a / A / s / q"
                console.print(f"[dim]· 请输入 {hint}[/dim]")
        answer = Prompt.ask(
            "选择",
            choices=["y", "n", "q"] if from_child else ["y", "n", "a", "A", "s", "q"],
            default=default,
            show_choices=True,
            case_sensitive=False,
        )
        return answer

    def _terminal_waiting(self, detail: str):
        """Return a waiting context, with a no-op fallback for partial REPLs."""
        from contextlib import nullcontext

        activity = getattr(self, "_terminal_activity", None)
        if activity is None:
            return nullcontext()
        return activity.waiting(detail)

