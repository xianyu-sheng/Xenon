"""Commands that toggle live REPL runtime behaviour."""

from __future__ import annotations

from typing import Any

from xenon.repl.command_registry import command_handler, register_command


register_command("/thinking", "切换推理过程显示（折叠/展开）", "/thinking [on|off]")


@command_handler("/thinking")
def cmd_thinking(*, args: str, session_state: dict, **kwargs: Any) -> str:
    repl = session_state.get("_repl")
    if repl:
        if args.strip().lower() == "on":
            repl._show_thinking = True
            return "✅ 推理过程显示已开启（每次都会展示工具调用明细）"
        if args.strip().lower() == "off":
            repl._show_thinking = False
            return "✅ 推理过程显示已关闭（默认折叠，Ctrl+O 可随时展开）"
        status = "展开" if repl._show_thinking else "折叠（Ctrl+O 展开）"
        return f"当前推理过程: {status}\n用法: /thinking on 或 /thinking off"
    return "❌ 无法获取 REPL 状态"


register_command("/stream", "切换流式输出模式", "/stream [on|off]")


@command_handler("/stream")
def cmd_stream(*, args: str, session_state: dict, **kwargs: Any) -> str:
    repl = session_state.get("_repl")
    if repl:
        if args.strip().lower() == "on":
            repl.streaming = True
            repl.status_bar.set_streaming(True)
            return "✅ 流式输出已开启"
        if args.strip().lower() == "off":
            repl.streaming = False
            repl.status_bar.set_streaming(False)
            return "✅ 流式输出已关闭"
        status = "开启" if repl.streaming else "关闭"
        return f"当前流式输出: {status}\n用法: /stream on 或 /stream off"
    return "❌ 无法获取 REPL 状态"


register_command("/optimize", "切换输入指令自动优化", "/optimize [on|off]")


@command_handler("/optimize")
def cmd_optimize(*, args: str, session_state: dict, **kwargs: Any) -> str:
    repl = session_state.get("_repl")
    if repl:
        if args.strip().lower() == "on":
            repl.optimize_prompts = True
            return "✅ 输入优化已开启\n口语化输入将自动重构为结构化 prompt"
        if args.strip().lower() == "off":
            repl.optimize_prompts = False
            return "✅ 输入优化已关闭\n输入将原样发送给模型"
        status = "开启" if repl.optimize_prompts else "关闭"
        return (
            f"当前输入优化: {status}\n\n"
            "开启后，口语化输入会自动重构为结构化 prompt，例如：\n"
            '  输入: "帮我写个快排"\n'
            '  优化: "## 任务\\n帮我写个快排\\n## 要求\\n代码完整可运行..."\n\n'
            "用法: /optimize on 或 /optimize off"
        )
    return "❌ 无法获取 REPL 状态"


register_command("/verbose", "切换详细输出模式（显示思考过程和工具调用）", "/verbose [on|off]")


@command_handler("/verbose")
def cmd_verbose(*, args: str, session_state: dict, **kwargs: Any) -> str:
    repl = session_state.get("_repl")
    if repl:
        if args.strip().lower() == "on":
            repl.verbose = True
            return "✅ 详细模式已开启\n引擎执行时将显示思考过程、工具调用和观察结果"
        if args.strip().lower() == "off":
            repl.verbose = False
            return "✅ 详细模式已关闭"
        status = "开启" if repl.verbose else "关闭"
        return f"当前详细模式: {status}\n用法: /verbose on 或 /verbose off"
    return "❌ 无法获取 REPL 状态"


register_command("/restart", "优雅重启 REPL（重载配置、可选保存会话）", "/restart [--fresh]")


@command_handler("/restart")
def cmd_restart(*, args: str, session_state: dict, **kwargs: Any) -> str:
    """优雅重启 REPL。

    用法：
        /restart        - 保存并恢复会话
        /restart --fresh - 不保存会话，全新启动
    """
    repl = session_state.get("_repl")
    if not repl:
        return "❌ 无法获取 REPL 状态"

    if not hasattr(repl, "_restart_manager"):
        return "❌ 重启管理器未初始化"

    # 解析参数
    preserve_session = "--fresh" not in args.lower()

    # 请求重启（主循环会检测并处理）
    repl._restart_manager.coordinator.request_restart(preserve_session)

    if preserve_session:
        return "🔄 正在准备重启（保存会话）..."
    else:
        return "🔄 正在准备重启（全新会话）..."


register_command("/debug-tokens", "显示详细的 token 计算信息", "/debug-tokens")


@command_handler("/debug-tokens")
def cmd_debug_tokens(*, args: str, session_state: dict, **kwargs: Any) -> str:
    """显示详细的 token 计算信息（P0-Critical）。

    用法：
        /debug-tokens        - 显示完整统计
        /debug-tokens --per-turn - 包含每条消息的详细 token 数
    """
    repl = session_state.get("_repl")
    if not repl:
        return "❌ 无法获取 REPL 状态"

    ctx = repl.context
    if not ctx:
        return "❌ 无法获取 ContextManager"

    debug_info = ctx.debug_tokens()
    show_per_turn = "--per-turn" in args.lower()

    lines = [
        "🔍 Token 计算详情",
        "",
        "## 总览",
        f"  历史消息数: {debug_info['history_length']}",
        f"  启发式估算总计: {debug_info['estimated_total']} tokens",
        f"  当前使用量: {debug_info['current_usage']} tokens",
        f"  累计 token 数: {debug_info['cumulative_tokens']} tokens",
        f"  数据来源: {debug_info['last_usage_source']}",
        f"  最大容量: {debug_info['max_tokens']} tokens",
        f"  使用率: {debug_info['usage_ratio']:.1%}",
        "",
    ]

    if debug_info['real_usage']:
        lines.extend([
            "## 最近一次真实 Usage",
            f"  Prompt tokens: {debug_info['real_usage']['prompt']}",
            f"  Completion tokens: {debug_info['real_usage']['completion']}",
            f"  Total tokens: {debug_info['real_usage']['total']}",
            "",
        ])
    else:
        lines.extend([
            "## 最近一次真实 Usage",
            "  （无真实 usage 数据，可能是首次调用前或使用了 mock）",
            "",
        ])

    if show_per_turn and debug_info['per_turn']:
        lines.append("## 每条消息的 Token 详情")
        for turn_info in debug_info['per_turn']:
            lines.append(
                f"  [{turn_info['index']}] {turn_info['role']} "
                f"({turn_info['turn_type']}): {turn_info['tokens']} tokens"
            )
            lines.append(f"      预览: {turn_info['content_preview']}")
        lines.append("")
        lines.append("💡 提示: 使用 /debug-tokens --per-turn 查看每条消息的详细信息")
    elif not show_per_turn:
        lines.append("💡 提示: 使用 /debug-tokens --per-turn 查看每条消息的详细信息")

    return "\n".join(lines)

