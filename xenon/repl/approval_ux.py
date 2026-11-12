"""Approval UX (R1-4: extracted from repl.py).

All interactive approval panels: plan approval, budget-checkpoint
continuation, legacy permission-gate delegation, unified tool-approval
panel and the terminal waiting spinner. Mixed into REPL as
``ApprovalUxMixin``.
"""

from __future__ import annotations

import sys
from pathlib import Path
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


def grant_targets_once(workspace: Path, tool_name: str, params: dict) -> None:
    """Let the tool's fence accept exactly what the human just approved (#413).

    The approval panel runs before the tool; the tool's path fence runs inside
    it.  Without this hand-off the fence overruled the human's answer, and the
    only way forward was an environment variable plus a restart.

    Scope is the literal target the human saw, and it is single-use: approving
    one file does not open its directory, and a second write asks again.
    """

    from xenon.nodes.approval_policy import write_targets_for
    from xenon.nodes.path_targets import resolve_target_path
    from xenon.nodes.sandbox_roots import grant_once

    for raw in write_targets_for(tool_name, params):
        try:
            grant_once(resolve_target_path(raw, cwd=workspace))
        except Exception:  # noqa: BLE001 — an unparsable target is simply not granted
            continue


class ApprovalUxMixin:
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
        """Identify the concrete execution path(s) a call touches.

        §9 binds a remembered answer to "工具 + 执行路径", so the key is the
        targets the human is being shown.  ``write_targets_for`` covers writes;
        a command has no path, so its own command string is the target.
        """

        from xenon.nodes.approval_policy import write_targets_for

        try:
            targets = tuple(
                sorted(str(p) for p in (write_targets_for(tool_name, params or {}) or []))
            )
        except Exception:  # noqa: BLE001 — 目标解析失败按“无目标”处理，保守询问
            targets = ()
        if not targets:
            command = str((params or {}).get("command") or "").strip()
            if command:
                targets = (command,)
        return (tool_name, targets)

    def _confirm_tool(
        self, tool_name: str, params: dict, risk: str, *, from_child: bool = False
    ) -> tuple[bool, str]:
        """Kernel approval bridge: answer one REQUIRE_APPROVAL decision.

        The kernel's ``PermissionPolicy`` decides *whether* to ask; this only
        renders the question and records the answer in the session's
        ``GrantBook`` — the single store §9 defines.

        ``from_child``（#373）：回答子任务经 broker 转发的请求时，本次回答
        只作用于这一次调用，不写入任何记忆。子任务只能继承父级已有的授权，
        不能自己积累。
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

        outcome = self._confirm_tool_approval(
            tool_name,
            params or {},
            reason=f"风险等级 {risk}",
            from_child=from_child,
            # The kernel only asks when the mode says "a human must answer"
            # (``scoped`` writes).  Auto-allowing in-workspace writes here made
            # ``/permissions default`` silent: the decision the mode promised
            # was answered by the UI before the panel could appear.
            adjudicated=True,
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
        *,
        from_child: bool = False,
        adjudicated: bool = False,
    ) -> str:
        """工具边界审批：工作区内写直接放行，越界写与命令询问。

        返回值是封闭结果词表：allowed-once / rejected / cancelled /
        unavailable；只有 allowed-once 是授权。非交互且无 assume_yes 时返回
        unavailable（fail-closed）。

        三档（技术文档 §9）：
          y  本次允许 —— 只放行这一次，下次再问
          a  全部允许 —— 该工具 + 该执行路径在期限内不再问（默认 30 分钟）
          n  拒绝

        已记住的授权由 ``GrantBook`` 先行回答，面板不会再出现；这里只在
        “没有任何授权”时提问，并把人的回答交回给同一个 book。

        ``from_child``（#373）：子任务经 broker 转发的请求只给 y/n/q 三个
        选择，任何“允许”都只作用于当前这一次调用，不写入任何记忆。父级已有的
        授权仍然先行命中——那就是“继承”。
        """

        self._last_approval_scope = ""
        if _config().interaction.assume_yes:
            return APPROVAL_ALLOWED_ONCE
        if not sys.stdin.isatty():
            logger.info("非交互环境：%s 未获边界审批", tool_name)
            return APPROVAL_UNAVAILABLE

        # 写工具在工作区内免问（用户决策 1）。执行级工具（command /
        # spawn_agent / 执行型 mcp_call）无论参数如何都必须询问——
        # 模型带一个工作区内的 file_path 不能绕过审批面板（H-2 修复）。
        #
        # ``adjudicated`` disables the shortcut: when the kernel asked, the
        # mode (``scoped``) has already decided a human must answer, and
        # answering for them silently turned ``/permissions default`` into
        # ``accept_edits``.
        from xenon.nodes.tool_classify import required_execution_level

        if (
            not adjudicated
            and required_execution_level(tool_name, params) < 3
            and not targets_outside_workspace(tool_name, params, self._workspace_root())
        ):
            return APPROVAL_ALLOWED_ONCE

        brief = str(params.get("command") or params.get("action") or "")[:120]
        diff_text = self._diff_preview(tool_name, params)
        target_note = self._current_target_note(tool_name, params)
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
                if target_note:
                    body += f"\n作用范围：{target_note}"
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
                hint = (
                    "y=本次允许  n=拒绝  q=取消任务"
                    if from_child
                    else "y=本次允许  t=本轮内该能力都不再问  "
                    "a=本会话都允许（仅这条命令/路径）  n=拒绝  q=取消任务"
                )
                console.print(
                    Panel(
                        body + chr(10) + chr(10) + f"[dim]{hint}[/dim]",
                        title="需要授权",
                        border_style="yellow",
                        padding=(0, 1),
                    )
                )
                try:
                    choice = self._ask_approval_choice(from_child=from_child)
                except (KeyboardInterrupt, EOFError):
                    return APPROVAL_CANCELLED

        if choice == "q":
            console.print("[dim]· 已取消任务[/dim]")
            if hasattr(callback, "resume_after_prompt"):
                callback.resume_after_prompt(tool_name, params)
            return APPROVAL_CANCELLED
        if choice not in {"y", "a", "t"}:
            console.print("[dim]· 未授权[/dim]")
            if hasattr(callback, "resume_after_prompt"):
                callback.resume_after_prompt(tool_name, params)
            return APPROVAL_REJECTED

        # 授权落地：工具自带的路径围栏必须同意，否则它会推翻人的回答（#413）。
        remembered = False
        if not from_child:
            grant_targets_once(self._workspace_root(), tool_name, params)
            remembered = self._remember_approval(tool_name, params, choice)

        if choice == "t":
            turn = str(getattr(self, "_current_kernel_turn", "") or "")
            from xenon.kernel.session_grants import TURN_ALLOW, grant_from_choice
            from xenon.repl.permission_map import capability_for_tool

            capability = capability_for_tool(tool_name)
            if capability is not None and turn and self._grant_book is not None:
                grant = grant_from_choice(
                    TURN_ALLOW,
                    capability=capability,
                    targets=("*",),
                    workspace=str(self._workspace_root()),
                    reason=f"this turn: {capability.value}",
                    turn=turn,
                )
                if grant is not None:
                    self._grant_book.add(grant)
                    # The book is only read at the *start* of a turn, so an
                    # answer given mid-turn has to reach the running policy
                    # directly or the very next call asks again — measured on
                    # a real task, which kept prompting after "t".
                    self._push_grant_to_runtime(grant)
            console.print(
                "[dim]· 本轮内不再询问该类操作（下一轮会重新询问）[/dim]"
            )
            self._last_approval_scope = "turn"
        elif choice == "a":
            if remembered:
                ttl = self._grant_book.ttl_seconds
                console.print(
                    f"[dim]· 本会话 {ttl / 60:.0f} 分钟内不再询问"
                    f"（仅 {target_note or tool_name}）[/dim]"
                )
                self._last_approval_scope = "session"
            else:
                # Some calls carry no path or command to key a grant on (an
                # MCP call with no target, a child's request).  Promising
                # silence we did not store would be a lie the user only
                # discovers at the next identical prompt.
                console.print(
                    "[dim]· 已授权本次；这类调用没有可绑定的执行路径，"
                    "下次仍会询问[/dim]"
                )
                self._last_approval_scope = "once"
        else:
            console.print("[dim]· 已授权本次操作[/dim]")
        if hasattr(callback, "resume_after_prompt"):
            callback.resume_after_prompt(tool_name, params)
        return APPROVAL_ALLOWED_ONCE

    def _remember_approval(self, tool_name: str, params: dict, choice: str) -> bool:
        """Record a remembered answer in the session's single ``GrantBook``.

        Only ``a`` reaches here.  ``y`` ("本次允许") is *not* remembered: the
        call in flight has already been authorized (the panel returned True and
        the tool fence was opened), so storing it would silently turn "just
        this once" into "every time".  §9: 只放行这一次，下次再用仍需询问。

        The grant is scoped to the capability and the concrete execution path —
        never to the whole capability, and never to a tool name (``bash`` would
        otherwise release every later shell command).

        Returns whether a grant was actually stored; the panel's "N 分钟内不再
        询问" promise depends on it.
        """

        if choice != "a":
            return False
        from xenon.kernel.session_grants import ALLOW, grant_from_choice
        from xenon.repl.permission_map import capability_for_tool

        capability = capability_for_tool(tool_name)
        if capability is None:
            return False
        _tool, targets = self._approval_key(tool_name, params)
        grant = grant_from_choice(
            ALLOW,
            capability=capability,
            targets=targets,
            workspace=str(self._workspace_root()),
            reason=f"approved via {tool_name}",
            turn=str(getattr(self, "_current_kernel_turn", "") or ""),
            ttl_seconds=self._grant_book.ttl_seconds,
        )
        if grant is None:
            return False
        stored = self._grant_book.add(grant)
        self._push_grant_to_runtime(stored)
        return True

    def _push_grant_to_runtime(self, grant: Any) -> None:
        """Make an answer given *mid-turn* apply to the turn in flight.

        The book is read once when a turn starts, so an answer given while it
        runs would otherwise only take effect on the next turn — measured on a
        real task, where pressing "t" kept prompting because nothing re-read the
        book.  The running policy is the only thing that can act on it now.
        """

        runtime = getattr(self, "_active_runtime", None)
        policies = getattr(runtime, "_policies", ()) if runtime is not None else ()
        for policy in policies:
            add = getattr(policy, "grant", None)
            if callable(add):
                try:
                    add(grant.as_scoped())
                except Exception:  # noqa: BLE001 — a grant must not break a run
                    logger.warning("policy rejected the grant", exc_info=True)

    def _current_target_note(self, tool_name: str, params: dict) -> str:
        """What an ``a`` answer would actually cover, in one line."""

        _tool, targets = self._approval_key(tool_name, params)
        if not targets:
            return ""
        return "、".join(targets[:3]) + (" 等" if len(targets) > 3 else "")

    def _ask_approval_choice(self, default: str = "n", *, from_child: bool = False) -> str:
        """Read y/a/n/q from the terminal.

        ``from_child`` (a brokered child request) narrows the answer set to
        y/n/q: a delegated model may only ask, it may not be remembered.
        """

        allowed = ["y", "t", "a", "n", "q"]
        if from_child:
            allowed = ["y", "n", "q"]
        answer = Prompt.ask(
            "选择",
            choices=allowed,
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

