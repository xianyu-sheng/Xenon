"""交互路径的工具运行时绑定 — 与评测路径隔离强度一致。

``bind_tool_runtime`` 此前只在两处调用：子代理 spawn（``react_engine.py``）
与 SWE-bench 评测（``evals/swebench_xenon.py``）。交互模式因此运行在
``ToolExecutor.runtime is None`` 状态下，``_runtime_params`` 原样返回模型
参数——而 ``cwd`` 在 ``ToolNode._VALID_PARAMS`` 内，且围栏根由 ``cwd`` 推导
（``ToolNode._get_allowed_root``）。结果是模型自选的 ``cwd`` 会**移动围栏本身**
而不是被围栏约束。

本文件锁住：交互路径也绑定 ToolRuntime，使两条路径强度一致。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xenon.engine.tool_runtime import ToolRuntime
from xenon.nodes.tool_executor import ToolExecutor
from xenon.repl.repl import REPL


@pytest.fixture
def repl_with_project(tmp_path):
    """构造一个只填了 project_ctx 的 REPL，绕开完整 __init__ 的重量级装配。"""
    with patch.object(REPL, "__init__", lambda self: None):
        repl = REPL()
    repl.project_ctx = MagicMock(root=tmp_path, working_dir=tmp_path)
    return repl, tmp_path


class TestModelSuppliedCwdIsOverridden:
    """核心不变量：可信根覆盖模型给的 cwd。"""

    def test_runtime_none_keeps_model_cwd(self, tmp_path):
        """回归前的行为，作为对照——说明为什么必须绑定。"""
        executor = ToolExecutor()
        params = {"file_path": "x.txt", "cwd": "/tmp/attacker-chosen"}
        assert executor._runtime_params(params)["cwd"] == "/tmp/attacker-chosen"

    def test_bound_runtime_overwrites_model_cwd(self, tmp_path):
        executor = ToolExecutor(runtime=ToolRuntime(workspace_root=tmp_path))
        params = {"file_path": "x.txt", "cwd": "/tmp/attacker-chosen"}
        assert executor._runtime_params(params)["cwd"] == str(tmp_path.resolve())


class TestInteractiveBinding:
    """REPL 侧的绑定必须真的发生，且用对根目录。"""

    def test_binds_project_root(self, repl_with_project):
        repl, tmp_path = repl_with_project
        captured: dict[str, Path] = {}

        def fake_bind(engine, runtime):
            captured["root"] = runtime.workspace_root

        with patch("xenon.engine.tool_runtime.bind_tool_runtime", fake_bind):
            repl._bind_interactive_tool_runtime(MagicMock())

        assert captured["root"] == tmp_path.resolve()

    def test_falls_back_to_working_dir(self, tmp_path):
        """未探测到项目根时用 working_dir，不应直接放弃绑定。"""
        with patch.object(REPL, "__init__", lambda self: None):
            repl = REPL()
        repl.project_ctx = MagicMock(root=None, working_dir=tmp_path)
        captured: dict[str, Path] = {}

        with patch(
            "xenon.engine.tool_runtime.bind_tool_runtime",
            lambda e, r: captured.__setitem__("root", r.workspace_root),
        ):
            repl._bind_interactive_tool_runtime(MagicMock())

        assert captured["root"] == tmp_path.resolve()

    def test_missing_directory_degrades_without_raising(self):
        """目录不存在（被删/被改名）不得让任务整体不可运行。

        ToolRuntime.__post_init__ 对非目录抛 ValueError；绑定失败只应记警告，
        ToolNode 自身围栏仍通过 cwd 兜底生效。
        """
        with patch.object(REPL, "__init__", lambda self: None):
            repl = REPL()
        repl.project_ctx = MagicMock(
            root=Path("/nonexistent/definitely-not-here"), working_dir=None
        )
        repl._bind_interactive_tool_runtime(MagicMock())  # 不抛即通过

    def test_run_engine_binds_before_run(self, repl_with_project):
        """绑定必须发生在 engine.run() 之前，否则首个工具调用不受约束。"""
        repl, tmp_path = repl_with_project
        order: list[str] = []

        engine = MagicMock()
        engine.run.side_effect = lambda *a, **k: (order.append("run"), "done")[1]

        with patch.object(
            REPL,
            "_bind_interactive_tool_runtime",
            lambda self, e: order.append("bind"),
        ):
            spec = MagicMock(
                name="react",
                mode_line="",
                result_title="R",
                log_result_diagnostics=False,
                preserve_thinking_panel=False,
            )
            spec.factory.return_value = engine
            for attr in (
                "_start_log_capture",
                "_make_callback",
                "_inject_mcp_tools_into_engine",
                "_stop_log_capture",
                "_persist_engine_trace",
                "_render_engine_result",
                "_start_steering_listener",
                "_engine_model_used",
                "_finish_engine_turn",
            ):
                setattr(repl, attr, MagicMock())
            repl.model_pool = MagicMock()
            repl.auto_router = MagicMock()
            repl.registry = MagicMock(models={})
            repl._permission_gate = MagicMock()
            repl.agent_context = MagicMock()
            repl.ctx_mgr = MagicMock()
            repl.status_bar = MagicMock()
            repl._last_mode_line = ""
            try:
                repl._run_engine(spec, "task", ["deepseek/deepseek-chat"])
            except Exception:
                # 渲染/收尾路径的 mock 不完整无妨——只断言先后顺序。
                pass

        assert order[:1] == ["bind"], f"绑定未先于 run 发生: {order}"
