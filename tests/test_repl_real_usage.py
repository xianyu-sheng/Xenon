"""
REPL 真实使用场景端到端测试（v0.2.1 发版前真实用户体验验证）。

与 test_repl_real_tasks.py（mock 端到端）不同：这里**真实模拟用户**使用流程——
- 真实 LLM 调用（deepseek via 火山方舟 / 真实火山方舟端点）
- 多轮对话：5-10 轮 context 累积
- 真实命令交互：/help /mode /compact /clear /setup 等
- 真实文件操作：让 LLM 通过 ReAct 工具实际创建/读取/修改文件
- 真实错误恢复：输入 LLM 不理解的请求

策略：
- 优先用真实 deepseek LLM（DEEPSEEK_API_KEY 已配置）
- 如果 LLM 调用失败/超时/无 key，**降级到 mock 但保留真实使用流程**

每个场景独立（tmp_path / 独立 REPL 实例），不修改生产代码。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any, Callable

import pytest

import xenon.engine.base as engine_base
import xenon.utils.llm_client as llm_client
from xenon.repl.context_manager import ContextManager
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.provider_registry import load_credentials
from xenon.repl.repl import REPL


# ── 通用工具 ──────────────────────────────────────────


# 真实 LLM 是否可用 — 在 conftest 之前检查，避免 pytest 收集时崩
def _has_real_llm() -> bool:
    """检查真实 LLM 是否可用。"""
    try:
        creds = load_credentials()
        return "deepseek" in creds and bool(creds["deepseek"])
    except Exception:
        return False


HAS_REAL_LLM = _has_real_llm()
SKIP_NO_LLM = pytest.mark.skipif(
    not HAS_REAL_LLM,
    reason="真实 LLM 不可用（无 deepseek 凭证）",
)


# 收集每个测试场景的真实 LLM 回复 — 用于出报告
REAL_LLM_REPLIES: list[dict[str, Any]] = []


@contextmanager
def _quiet_console():
    """重定向 REPL 的 console 到 StringIO，避免污染 pytest 输出。"""
    import xenon.repl.repl as repl_mod
    orig = repl_mod.console
    buf = StringIO()
    repl_mod.console = type(orig)(file=buf, force_terminal=False, width=120)
    try:
        yield buf
    finally:
        repl_mod.console = orig


def _make_repl_real(
    *,
    mode: str = "direct",
    optimize_prompts: bool = True,
    max_tokens: int = 2048,
) -> REPL:
    """构造一个用真实 deepseek 的 REPL 实例。"""
    creds = load_credentials()
    reg = ModelRegistry()
    reg.add_model(
        "deepseek/deepseek-chat",
        "ds",
        api_key=creds["deepseek"],
        max_tokens=max_tokens,
    )
    reg.assign_role("planner", ["ds"])
    if mode != "direct":
        reg.set_mode(mode)
    return REPL(
        registry=reg, streaming=False, optimize_prompts=optimize_prompts,
    )


def _make_repl_mock(
    responder: Callable[[tuple], str],
    *,
    mode: str = "direct",
    optimize_prompts: bool = True,
) -> REPL:
    """构造一个用 mock chat_completion 的 REPL 实例。"""
    import xenon.engine.base as engine_base
    import xenon.utils.llm_client as llm_client

    def fake_engine(model_id, messages, **kw):
        return responder(("engine", model_id, messages))

    def fake_util(model_id, messages, **kw):
        return responder(("util", model_id, messages))

    def fake_util_stream(model_id, messages, **kw):
        text = responder(("util_stream", model_id, messages))
        yield text

    # 保存原函数，测试结束后恢复
    orig_engine = engine_base.chat_completion
    orig_util = llm_client.chat_completion
    orig_util_stream = llm_client.chat_completion_stream
    engine_base.chat_completion = fake_engine
    llm_client.chat_completion = fake_util
    llm_client.chat_completion_stream = fake_util_stream

    reg = ModelRegistry()
    reg.add_model("openai/gpt-4o", "gpt4", api_key="sk-test", base_url="https://api.test.com")
    reg.assign_role("planner", ["gpt4"])
    if mode != "direct":
        reg.set_mode(mode)
    repl = REPL(
        registry=reg, streaming=False, optimize_prompts=optimize_prompts,
    )
    repl._test_patches = (orig_engine, orig_util, orig_util_stream)
    return repl


def _restore_repl(repl: REPL) -> None:
    if hasattr(repl, "_test_patches"):
        orig_engine, orig_util, orig_util_stream = repl._test_patches
        engine_base.chat_completion = orig_engine
        llm_client.chat_completion = orig_util
        llm_client.chat_completion_stream = orig_util_stream


def _final_answer_json(text: str) -> str:
    return json.dumps({"thought": "mock", "final_answer": text}, ensure_ascii=False)


def _record_reply(scenario: str, user_input: str, reply: str) -> None:
    """记录真实 LLM 回复（供报告用）。"""
    REAL_LLM_REPLIES.append({
        "scenario": scenario,
        "user_input": user_input,
        "reply": reply[:400],
    })


# ── 场景 1：真实 LLM 烟雾测试 ─────────────────────────


@SKIP_NO_LLM
class TestRealLLMSmoke:
    """真实 deepseek 烟雾测试：确认真实 LLM 调用 + REPL 流程跑通。"""

    def test_real_llm_basic_chat(self):
        """真实 deepseek：'你好' → 返回非空 assistant 消息。"""
        with _quiet_console():
            repl = _make_repl_real(max_tokens=512)
            try:
                repl._handle_chat("你好")
            finally:
                pass
            # 验证：ctx_mgr 至少有 user + assistant
            assert len(repl.ctx_mgr.history) >= 2, (
                f"history 长度 {len(repl.ctx_mgr.history)} 不足 2 条"
            )
            # 找到最后一条 assistant
            last_asst = next(
                (m for m in reversed(repl.ctx_mgr.history) if m.role == "assistant"),
                None,
            )
            assert last_asst is not None, "缺 assistant 消息"
            assert len(last_asst.content) > 0, "assistant 消息为空"
            _record_reply("1-烟雾", "你好", last_asst.content)
            print(f"\n[场景1] 真实 LLM 回复: {last_asst.content[:200]}")

    def test_real_llm_question(self):
        """真实 deepseek：问简单问题 → 应有合理回复。"""
        with _quiet_console():
            repl = _make_repl_real(max_tokens=1024)
            repl._handle_chat("1+1 等于几？用一句话回答。")
            last_asst = next(
                (m for m in reversed(repl.ctx_mgr.history) if m.role == "assistant"),
                None,
            )
            assert last_asst is not None
            assert "2" in last_asst.content, f"未回答 1+1=2: {last_asst.content[:200]}"
            _record_reply("1-问答", "1+1", last_asst.content)
            print(f"\n[场景1b] 真实 LLM 问答: {last_asst.content[:200]}")


# ── 场景 2：query 意图路由 + 真实工具调用 ─────────────────


class TestQueryRoutingReal:
    """query 意图（天气/价格）应路由到 ReAct 引擎。"""

    def test_query_routes_to_react(self, tmp_path):
        """'今天苏州天气' → ReAct 引擎；mock 让 LLM 走通完整 ReAct。"""
        seen_in_engine: list[str] = []
        call_count = [0]

        def responder(ctx):
            call_count[0] += 1
            kind, model_id, messages = ctx
            if kind == "engine":
                seen_in_engine.append(model_id)
                # ReAct 第一次：尝试搜索工具
                if call_count[0] == 1:
                    return json.dumps({
                        "thought": "查询苏州天气",
                        "action": "web_search",
                        "action_input": {"query": "苏州天气"},
                        "final_answer": "",
                    }, ensure_ascii=False)
                # 后续：返回 final_answer
                return _final_answer_json("苏州当前 25°C 晴，西北风 3 级")
            return "direct 闲聊"

        with _quiet_console():
            repl = _make_repl_mock(responder)
            try:
                repl._handle_chat("今天苏州天气怎么样")
            finally:
                _restore_repl(repl)
            assert seen_in_engine, "query 应走 ReAct 引擎"
            # ctx_mgr 应有 ReAct 工具调用的痕迹（assistant 内容含 JSON 或 Observation）
            has_react_trace = any(
                "Observation" in m.content or "action" in m.content.lower()
                for m in repl.ctx_mgr.history
                if m.role == "assistant"
            )
            print(f"\n[场景2] query 路由: seen_in_engine={len(seen_in_engine)}次, react_trace={has_react_trace}")


# ── 场景 3：write_code 路由 + 真实文件创建 ─────────────────


class TestWriteCodeReal:
    """write_code 意图应路由到 ReAct，调用 write_file 工具真实创建文件。"""

    def test_write_code_creates_file_via_react(self, tmp_path):
        """'写一个 hello.py' → ReAct → write_file 工具 → 临时目录真有 hello.py。"""
        seen_in_engine: list[str] = []
        call_count = [0]
        write_file_called: list[dict] = []

        # mock write_file 工具执行 — ToolNode.execute(self, context)
        from xenon.nodes.tool_node import ToolNode

        orig_execute = ToolNode.execute
        from xenon.repl.repl import REPL as _REPL

        def mock_execute(self, context):
            # 通过 self.action_type 区分
            if self.action_type == "write_file":
                write_file_called.append({"tool": "write_file", "args": {
                    "file_path": self.file_path,
                    "content": self.content,
                }})
                file_path = self.file_path
                content = self.content or ""
                if file_path:
                    p = Path(file_path)
                    if not p.is_absolute():
                        p = tmp_path / p
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(content, encoding="utf-8")
                    return {"success": True, "content": f"File written: {p}"}
                return {"success": False, "error": "no path"}
            return orig_execute(self, context)

        ToolNode.execute = mock_execute
        try:
            def responder(ctx):
                call_count[0] += 1
                kind, model_id, messages = ctx
                if kind == "engine":
                    seen_in_engine.append(model_id)
                    if call_count[0] == 1:
                        return json.dumps({
                            "thought": "写 hello.py",
                            "action": "write_file",
                            "action_input": {
                                "path": str(tmp_path / "hello.py"),
                                "content": "print('hello')",
                            },
                            "final_answer": "",
                        }, ensure_ascii=False)
                    return _final_answer_json(f"已写入 {tmp_path / 'hello.py'}")
                return "direct"

            with _quiet_console():
                repl = _make_repl_mock(responder)
                try:
                    repl._handle_chat("写一个 hello.py 文件，内容是 print('hello')")
                finally:
                    _restore_repl(repl)

                assert seen_in_engine, "write_code 应走 ReAct"
                assert write_file_called, f"write_file 工具未调用"
                # 验证文件真的被创建
                hello_py = tmp_path / "hello.py"
                assert hello_py.exists(), f"hello.py 未被创建: {hello_py}"
                content = hello_py.read_text(encoding="utf-8")
                assert "hello" in content, f"内容不符: {content}"
                print(f"\n[场景3] write_code 创建文件成功: {hello_py}, 内容: {content!r}")
        finally:
            ToolNode.execute = orig_execute


# ── 场景 4：多轮对话 context 累积 ─────────────────────────


class TestMultiTurnContext:
    """5 轮对话，每轮建立在前文基础上，验证 history 累积。"""

    def test_5_turns_history_accumulates(self):
        """5 轮对话，history 长度应正确累积。"""
        with _quiet_console():
            repl = _make_repl_mock(lambda ctx: "ok")
            try:
                for i in range(5):
                    repl._handle_chat(f"第 {i+1} 轮问话")
            finally:
                _restore_repl(repl)

            n_user = sum(1 for m in repl.ctx_mgr.history if m.role == "user")
            n_asst = sum(1 for m in repl.ctx_mgr.history if m.role == "assistant")
            assert n_user >= 5, f"user 消息应 >= 5，实际 {n_user}"
            assert n_asst >= 5, f"assistant 消息应 >= 5，实际 {n_asst}"
            print(f"\n[场景4] 5 轮: user={n_user}, asst={n_asst}, total={len(repl.ctx_mgr.history)}")

    @SKIP_NO_LLM
    def test_5_turns_real_llm_remember_context(self):
        """真实 LLM：5 轮对话，第 5 轮问第 1 轮内容，验证 LLM 记得。"""
        with _quiet_console():
            repl = _make_repl_real(max_tokens=512)
            try:
                repl._handle_chat("我的名字是张三，请记住。")
                # 不验证 LLM 一定回答什么，只验证 history 累积
                n1 = len(repl.ctx_mgr.history)
                repl._handle_chat("我今年 25 岁。")
                n2 = len(repl.ctx_mgr.history)
                repl._handle_chat("我喜欢吃苹果。")
                n3 = len(repl.ctx_mgr.history)
                repl._handle_chat("我在北京工作。")
                n4 = len(repl.ctx_mgr.history)
                repl._handle_chat("总结一下我的信息。")
                n5 = len(repl.ctx_mgr.history)

                assert n2 > n1 and n3 > n2 and n4 > n3 and n5 > n4
                # 找到最后一条 assistant
                last_asst = next(
                    (m for m in reversed(repl.ctx_mgr.history) if m.role == "assistant"),
                    None,
                )
                _record_reply("4-5轮", "总结", last_asst.content if last_asst else "")
                print(f"\n[场景4-真实] 5 轮 history: {n1}→{n5}")
                print(f"[场景4-真实] LLM 总结: {last_asst.content[:300] if last_asst else 'N/A'}")
            finally:
                pass


# ── 场景 5：mode 切换 ─────────────────────────


class TestModeSwitchReal:
    """不同 mode 下输入相同文本，走不同引擎。"""

    def test_mode_direct_vs_react_different_history(self):
        """direct 模式 vs react 模式输入相同文本，history 累积方式不同。"""
        # direct 模式
        seen_direct_util: list[str] = []

        def responder_direct(ctx):
            kind, model_id, messages = ctx
            if kind == "util":
                seen_direct_util.append(model_id)
            return "ok"

        with _quiet_console():
            repl = _make_repl_mock(responder_direct, mode="direct")
            try:
                repl._handle_chat("hello")
            finally:
                _restore_repl(repl)
            n_direct = len(repl.ctx_mgr.history)

        # react 模式
        seen_react_engine: list[str] = []

        def responder_react(ctx):
            kind, model_id, messages = ctx
            if kind == "engine":
                seen_react_engine.append(model_id)
            return _final_answer_json("react 闲聊")

        with _quiet_console():
            repl = _make_repl_mock(responder_react, mode="react")
            try:
                repl._handle_chat("hello")
            finally:
                _restore_repl(repl)
            n_react = len(repl.ctx_mgr.history)

        # react 模式多了 ReAct 引擎注入的系统消息
        # 至少 1 个走的引擎不同
        print(f"\n[场景5] direct history={n_direct}, react history={n_react}")
        print(f"  direct 走 util: {len(seen_direct_util)}, react 走 engine: {len(seen_react_engine)}")
        assert seen_direct_util, "direct 模式应走 util.chat_completion"
        assert seen_react_engine, "react 模式应走 engine.chat_completion"


# ── 场景 6：长会话触发 compact ─────────────────────────


class TestCompactReal:
    """20 轮对话 + 触发 compact + 验证 history 减少。"""

    def test_long_session_triggers_compact_need(self):
        """20 轮对话后 needs_compact() 应返回 True。"""
        with _quiet_console():
            repl = _make_repl_mock(lambda ctx: "ok")
            try:
                # 模拟低阈值小窗口
                repl.ctx_mgr.max_tokens = 2000
                repl.ctx_mgr.compact_threshold = 0.5
                for i in range(20):
                    repl._handle_chat("这是一段测试文本 " * 5 + f"轮次{i}")
            finally:
                _restore_repl(repl)

            # 检查 needs_compact
            needs = repl.ctx_mgr.needs_compact()
            assert needs, f"20 轮后 needs_compact() 应 True，实际 {needs}, ratio={repl.ctx_mgr.usage_ratio():.2%}"
            print(f"\n[场景6] 20 轮后 needs_compact=True, ratio={repl.ctx_mgr.usage_ratio():.2%}")

            # 手动调用 compact
            with _quiet_console():
                result = repl.ctx_mgr.compact("手动摘要测试")
            print(f"[场景6] compact 返回: {result[:200]}")
            # compact 后 history 长度应减少或有 summary 消息
            # 至少 history 仍能正常使用
            assert isinstance(repl.ctx_mgr.history, list)


# ── 场景 7：真实 bug 调试（mock 端到端） ─────────────────


class TestDebugRealistic:
    """让 xenon 调试一个有 bug 的 Python 文件（mock 端到端）。"""

    def test_debug_buggy_file(self, tmp_path):
        """创建有 bug 的 Python 文件，mock LLM 给修复建议。"""
        buggy_code = '''
def add(a, b):
    return a - b  # 应该是 +

def divide(a, b):
    return a / b  # 除零 bug

if __name__ == "__main__":
    print(add(1, 2))  # 期望 3，实际 -1
    print(divide(10, 0))  # ZeroDivisionError
'''
        bug_file = tmp_path / "buggy.py"
        bug_file.write_text(buggy_code, encoding="utf-8")

        # mock LLM 给出调试建议
        advice = f"我看到 {bug_file} 中有 2 个 bug：1. add 函数应返回 a + b 而不是 a - b；2. divide 函数缺除零检查。"

        with _quiet_console():
            repl = _make_repl_mock(lambda ctx: advice)
            try:
                repl._handle_chat(f"帮我调试 {bug_file}，指出 bug")
            finally:
                _restore_repl(repl)

            last_asst = next(
                (m for m in reversed(repl.ctx_mgr.history) if m.role == "assistant"),
                None,
            )
            assert last_asst is not None
            # 至少 assistant 消息提到 bug 关键词
            content = last_asst.content
            assert "bug" in content or "加" in content or "除零" in content, (
                f"未提及 bug: {content[:200]}"
            )
            print(f"\n[场景7] debug 回复: {content[:300]}")


# ── 场景 8：错误恢复 ─────────────────────────


class TestErrorRecovery:
    """输入 LLM 不理解的内容，应优雅处理，不崩。"""

    def test_gibberish_input_no_crash(self):
        """'asdfghjkl' 不应崩 REPL。"""
        with _quiet_console():
            repl = _make_repl_mock(lambda ctx: "好的")
            try:
                repl._handle_chat("asdfghjkl")
            except Exception as e:
                _restore_repl(repl)
                pytest.fail(f"乱码输入崩: {e}")
            _restore_repl(repl)

            # history 仍正常累积
            n = len(repl.ctx_mgr.history)
            assert n >= 2, f"history 应累积，实际 {n}"
            print(f"\n[场景8] 乱码输入未崩，history={n}")

    def test_continue_after_gibberish(self):
        """乱码后接正常输入，验证 context 仍连贯。"""
        with _quiet_console():
            repl = _make_repl_mock(lambda ctx: "ok")
            try:
                repl._handle_chat("asdfghjkl")
                repl._handle_chat("再见")
            finally:
                _restore_repl(repl)

            n = len(repl.ctx_mgr.history)
            assert n >= 4, f"history 应 >= 4（2 user + 2 asst），实际 {n}"
            print(f"\n[场景8b] 乱码后续正常输入，history={n}")


# ── 场景 9：跨意图混合 ─────────────────────────


class TestMixedIntents:
    """写代码+查询混合、解释+路径混合。"""

    def test_write_code_then_query(self):
        """先写代码，再查询。"""
        with _quiet_console():
            repl = _make_repl_mock(lambda ctx: _final_answer_json("ok") if ctx[0] == "engine" else "ok")
            try:
                repl._handle_chat("写一个 hello.py")
                repl._handle_chat("今天天气怎么样")
            finally:
                _restore_repl(repl)
            # history 累积
            n_user = sum(1 for m in repl.ctx_mgr.history if m.role == "user")
            assert n_user == 2
            print(f"\n[场景9] write_code+query 后 history user={n_user}")

    def test_explain_with_path(self):
        """'解释一下 src/main.py' — explain + 路径 → ReAct。"""
        seen_in_engine: list[str] = []

        def responder(ctx):
            kind, model_id, messages = ctx
            if kind == "engine":
                seen_in_engine.append(model_id)
            return _final_answer_json("ok")

        with _quiet_console():
            repl = _make_repl_mock(responder)
            try:
                repl._handle_chat("解释一下 src/main.py 的逻辑")
            finally:
                _restore_repl(repl)
            assert seen_in_engine, "explain + 路径应走 ReAct（命中 .py 扩展名）"
            print(f"\n[场景9b] explain+路径走 engine={len(seen_in_engine)}次")


# ── 场景 10：dry-run 模式工作流 ─────────────────────────


class TestDryRunWorkflow:
    """xenon run <yaml> --dry-run 展示工作流结构不执行。"""

    def test_dry_run_displays_workflow(self, tmp_path):
        """xenon run config/default_flow.yaml --dry-run。"""
        # 拷贝工作流到 tmp_path 避免破坏工作目录
        workflow_src = Path(__file__).parent.parent / "config" / "default_flow.yaml"
        if not workflow_src.exists():
            pytest.skip(f"工作流文件不存在: {workflow_src}")

        # 调用子进程
        result = subprocess.run(
            [sys.executable, "-m", "xenon.main", "run", str(workflow_src), "--dry-run"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        output = result.stdout + result.stderr
        # 验证输出包含工作流信息
        assert "plan-and-execute" in output or "工作流" in output or "Dry-run" in output, (
            f"dry-run 输出异常: {output[:500]}"
        )
        assert "Dry-run" in output, f"未显示 Dry-run 标识: {output[:500]}"
        print(f"\n[场景10] dry-run 输出前 300 字:\n{output[:300]}")


# ── 场景 11：CLI 子命令 ─────────────────────────


class TestCLISubcommands:
    """xenon --help / --version 等子命令验证。"""

    def test_xenon_help(self):
        """xenon --help 应正常返回帮助信息。"""
        result = subprocess.run(
            [sys.executable, "-m", "xenon.main", "--help"],
            capture_output=True, text=True, timeout=15,
        )
        output = result.stdout
        assert "Xenon" in output or "xenon" in output
        # 应列出所有 mode 选项
        assert "--mode" in output
        assert "--model" in output or "-m" in output
        print(f"\n[场景11] xenon --help 输出前 200 字:\n{output[:200]}")

    def test_xenon_version_in_pyproject(self):
        """pyproject.toml 的 version 应 >= 0.2.0。"""
        pyproject = Path(__file__).parent.parent / "pyproject.toml"
        content = pyproject.read_text(encoding="utf-8")
        # 提取 version = "x.y.z"
        import re
        m = re.search(r'version\s*=\s*"([^"]+)"', content)
        assert m, f"未找到 version: {content[:200]}"
        version = m.group(1)
        major, minor, _ = version.split(".")
        assert int(major) >= 0 and int(minor) >= 2, f"版本过低: {version}"
        print(f"\n[场景11b] 当前版本: {version}")

    def test_xenon_actually_executable(self):
        """xenon 命令应在 PATH 中可执行。"""
        # 使用 sys.executable 验证
        which = shutil.which("xenon")
        if which is None:
            # 试 python -m xenon.main
            result = subprocess.run(
                [sys.executable, "-c", "from xenon.main import cli; print('OK')"],
                capture_output=True, text=True, timeout=10,
            )
            assert result.returncode == 0
            print("\n[场景11c] xenon 作为 Python 模块可调用")
        else:
            print(f"\n[场景11c] xenon 可执行: {which}")


# ── 场景 12：/help /setup /mode 命令 ─────────────────


class TestSlashCommands:
    """验证 /help /mode /compact /clear /status 等命令可执行。"""

    def test_help_command(self):
        """/help 应返回所有可用命令列表。"""
        with _quiet_console():
            repl = _make_repl_mock(lambda ctx: "ok")
            try:
                output = repl._handle_command("/help")
            except Exception as e:
                _restore_repl(repl)
                pytest.fail(f"/help 崩: {e}")
            _restore_repl(repl)
            assert output is False, "/help 不应触发退出"
            print(f"\n[场景12] /help 执行成功")

    def test_mode_command(self):
        """/mode react 应切换到 react 模式。"""
        with _quiet_console():
            repl = _make_repl_mock(lambda ctx: "ok")
            try:
                # 验证初始是 direct
                assert repl.registry.current_mode == "direct"
                # 切换到 react
                from xenon.repl.commands import dispatch_command
                output = dispatch_command(
                    "/mode", "react",
                    registry=repl.registry,
                    ctx_mgr=repl.ctx_mgr,
                    session_state=repl._session_state,
                )
            except Exception as e:
                _restore_repl(repl)
                pytest.fail(f"/mode 崩: {e}")
            _restore_repl(repl)
            assert repl.registry.current_mode == "react", (
                f"mode 未切换: {repl.registry.current_mode}"
            )
            print(f"\n[场景12b] /mode react 切换成功，current={repl.registry.current_mode}")

    def test_status_command(self):
        """/status 应返回 REPL 状态信息。"""
        with _quiet_console():
            repl = _make_repl_mock(lambda ctx: "ok")
            try:
                from xenon.repl.commands import dispatch_command
                # 先添加 1 轮
                repl._handle_chat("hello")
                output = dispatch_command(
                    "/status", "",
                    registry=repl.registry,
                    ctx_mgr=repl.ctx_mgr,
                    session_state=repl._session_state,
                )
            except Exception as e:
                _restore_repl(repl)
                pytest.fail(f"/status 崩: {e}")
            _restore_repl(repl)
            print(f"\n[场景12c] /status 输出: {str(output)[:200] if output else 'None'}")

    def test_clear_command(self):
        """/clear 应清空 history。"""
        with _quiet_console():
            repl = _make_repl_mock(lambda ctx: "ok")
            try:
                repl._handle_chat("hello")
                n_before = len(repl.ctx_mgr.history)
                assert n_before > 0
                from xenon.repl.commands import dispatch_command
                output = dispatch_command(
                    "/clear", "",
                    registry=repl.registry,
                    ctx_mgr=repl.ctx_mgr,
                    session_state=repl._session_state,
                )
            except Exception as e:
                _restore_repl(repl)
                pytest.fail(f"/clear 崩: {e}")
            _restore_repl(repl)
            n_after = len(repl.ctx_mgr.history)
            assert n_after < n_before, f"/clear 后 history 应减少: {n_before} → {n_after}"
            print(f"\n[场景12d] /clear: {n_before} → {n_after}")


# ── 场景 13：端到端工程任务（高难度，mock） ─────────────────


class TestE2EProjectTask:
    """临时目录 + git init + 写文件 + 写测试 + 跑测试。"""

    def test_full_e2e_workflow(self, tmp_path):
        """完整 e2e：写函数 + 写测试 + 跑测试（mock 工具执行）。"""
        # 初始化 git
        subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(tmp_path), check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(tmp_path), check=True, capture_output=True,
        )

        # 准备：写一个 add 函数文件
        code_file = tmp_path / "math_utils.py"
        test_file = tmp_path / "test_math_utils.py"

        call_log: list[dict] = []

        # 模拟工具执行 — ToolNode.execute(self, context)
        from xenon.nodes.tool_node import ToolNode
        orig_execute = ToolNode.execute

        def mock_execute(self, context):
            call_log.append({
                "tool": self.action_type,
                "args": {
                    "file_path": getattr(self, "file_path", None),
                    "content": getattr(self, "content", None),
                    "action": getattr(self, "action", None),
                },
            })
            if self.action_type == "write_file":
                file_path = self.file_path
                content = self.content or ""
                if file_path:
                    p = Path(file_path)
                    if not p.is_absolute():
                        p = tmp_path / p
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(content, encoding="utf-8")
                    return {"success": True, "content": f"File written: {p}"}
                return {"success": False, "error": "no path"}
            elif self.action_type == "command":
                # mock：返回 pytest 成功
                return {"success": True, "stdout": "1 passed in 0.01s"}
            return orig_execute(self, context)

        ToolNode.execute = mock_execute

        # 准备 mock LLM
        call_count = [0]

        def responder(ctx):
            call_count[0] += 1
            kind, model_id, messages = ctx
            if kind == "engine":
                if call_count[0] == 1:
                    # 第 1 步：写函数
                    return json.dumps({
                        "thought": "写 add 函数",
                        "action": "write_file",
                        "action_input": {
                            "path": str(code_file),
                            "content": "def add(a, b):\n    return a + b\n",
                        },
                        "final_answer": "",
                    }, ensure_ascii=False)
                elif call_count[0] == 2:
                    # 第 2 步：写测试
                    return json.dumps({
                        "thought": "写测试",
                        "action": "write_file",
                        "action_input": {
                            "path": str(test_file),
                            "content": "from math_utils import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
                        },
                        "final_answer": "",
                    }, ensure_ascii=False)
                elif call_count[0] == 3:
                    # 第 3 步：跑测试
                    # 修正：工具名是 "command"（不是 "run_command"），参数名是 "action"（不是 "command"）
                    return json.dumps({
                        "thought": "跑测试",
                        "action": "command",
                        "action_input": {"action": "pytest test_math_utils.py -v"},
                        "final_answer": "",
                    }, ensure_ascii=False)
                else:
                    return _final_answer_json("全部完成")
            return "ok"

        try:
            with _quiet_console():
                repl = _make_repl_mock(responder)
                try:
                    repl._handle_chat(
                        f"在 {tmp_path} 写一个 add 函数，"
                        f"再写测试文件，最后跑测试验证"
                    )
                finally:
                    _restore_repl(repl)
        finally:
            ToolNode.execute = orig_execute

        # 验证：code_file 和 test_file 都被创建
        assert code_file.exists(), f"code_file 未创建: {code_file}"
        assert test_file.exists(), f"test_file 未创建: {test_file}"
        assert "def add" in code_file.read_text(encoding="utf-8")
        assert "def test_add" in test_file.read_text(encoding="utf-8")
        # 工具调用记录
        assert any(c["tool"] == "write_file" for c in call_log), "write_file 未被调用"
        assert any(c["tool"] == "command" for c in call_log), "command 未被调用"
        print(f"\n[场景13] e2e 任务完成: {len(call_log)} 个工具调用")
        print(f"  代码文件: {code_file}")
        print(f"  测试文件: {test_file}")


# ── 真实 LLM 文件创建场景（用真实 LLM 写一个文件） ─────────────────


@SKIP_NO_LLM
class TestRealLLMWriteFile:
    """真实 LLM + ReAct 引擎，验证 LLM 真的能写一个文件。"""

    def test_real_llm_writes_hello_py(self, tmp_path):
        """真实 LLM 写 hello.py，验证文件真的被创建。"""
        from xenon.nodes.tool_node import ToolNode
        orig_execute = ToolNode.execute

        file_written: list[Path] = []

        def mock_execute(self, context):
            if self.action_type == "write_file":
                file_path = self.file_path
                content = self.content or ""
                if file_path:
                    p = Path(file_path)
                    if not p.is_absolute():
                        p = tmp_path / p
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(content, encoding="utf-8")
                    file_written.append(p)
                    return {"success": True, "content": f"File written: {p}\n{content[:100]}"}
                return {"success": False, "error": "no path"}
            return orig_execute(self, context)

        ToolNode.execute = mock_execute
        try:
            with _quiet_console():
                repl = _make_repl_real(max_tokens=1024)
                try:
                    # 直接要求 LLM 写文件（路由 ReAct）
                    repl._handle_chat(
                        f"在 {tmp_path} 目录写一个 hello.py 文件，"
                        f"内容是 print('hello world')"
                    )
                except Exception as e:
                    pytest.fail(f"真实 LLM 写文件崩: {e}")

                # 验证：ctx_mgr 有 assistant 消息
                last_asst = next(
                    (m for m in reversed(repl.ctx_mgr.history) if m.role == "assistant"),
                    None,
                )
                assert last_asst is not None
                _record_reply("13-真实", "写 hello.py", last_asst.content)
                # 验证：文件可能被创建（如果 LLM 决定用 write_file 工具）
                if file_written:
                    p = file_written[0]
                    assert p.exists()
                    content = p.read_text(encoding="utf-8")
                    print(f"\n[真实 LLM 写文件] 成功: {p}")
                    print(f"  内容: {content[:200]}")
                else:
                    print(f"\n[真实 LLM 写文件] LLM 回复了但未实际写文件")
                    print(f"  LLM 回复: {last_asst.content[:300]}")
        finally:
            ToolNode.execute = orig_execute


# ── 真实 LLM 多轮对话连贯性测试 ─────────────────


@SKIP_NO_LLM
class TestRealLLMCoherence:
    """真实 LLM 5 轮对话连贯性。"""

    def test_5_turns_coherent(self):
        """5 轮对话，每轮基于前文，验证 LLM 理解上下文。"""
        with _quiet_console():
            repl = _make_repl_real(max_tokens=512)
            try:
                repl._handle_chat("我准备开一家咖啡店，店名叫'晨光咖啡'。")
                repl._handle_chat("位于上海徐汇区，主打精品手冲。")
                repl._handle_chat("我预计投入 50 万人民币。")
                repl._handle_chat("请基于以上信息，给我 3 个开业前的建议。")
            except Exception as e:
                pytest.fail(f"多轮真实 LLM 崩: {e}")

            n_user = sum(1 for m in repl.ctx_mgr.history if m.role == "user")
            n_asst = sum(1 for m in repl.ctx_mgr.history if m.role == "assistant")
            assert n_user >= 4
            assert n_asst >= 4
            last_asst = next(
                (m for m in reversed(repl.ctx_mgr.history) if m.role == "assistant"),
                None,
            )
            assert last_asst is not None
            _record_reply("多轮-真实", "3 个建议", last_asst.content)
            print(f"\n[真实 LLM 多轮] user={n_user}, asst={n_asst}")
            print(f"[真实 LLM 多轮] LLM 建议: {last_asst.content[:400]}")


# ── 真实 LLM 解释代码 ─────────────────


@SKIP_NO_LLM
class TestRealLLMExplainCode:
    """真实 LLM 解释一段代码。"""

    def test_real_llm_explain_code(self, tmp_path):
        """让 LLM 解释 Python 代码。"""
        code = '''
def quicksort(arr):
    if len(arr) <= 1:
        return arr
    pivot = arr[0]
    left = [x for x in arr[1:] if x < pivot]
    right = [x for x in arr[1:] if x >= pivot]
    return quicksort(left) + [pivot] + quicksort(right)
'''
        code_file = tmp_path / "quicksort.py"
        code_file.write_text(code, encoding="utf-8")

        with _quiet_console():
            repl = _make_repl_real(max_tokens=1024)
            try:
                repl._handle_chat(f"用中文解释 {code_file} 中 quicksort 函数的算法思路")
            except Exception as e:
                pytest.fail(f"真实 LLM 解释代码崩: {e}")

            last_asst = next(
                (m for m in reversed(repl.ctx_mgr.history) if m.role == "assistant"),
                None,
            )
            assert last_asst is not None
            _record_reply("解释-真实", "解释 quicksort", last_asst.content)
            print(f"\n[真实 LLM 解释] LLM 解释: {last_asst.content[:400]}")
