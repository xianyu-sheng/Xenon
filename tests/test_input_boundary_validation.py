"""MCP 服务器名与配置校验回归测试。

Bug 背景：add_server_pending 是惰性注册（不连接），此前对名字零校验，
空名/含 ':' 的名字会静默进入 pending，直到首次工具调用才以晦涩方式
失败；':' 名还会与 server:tool 命名空间路由产生歧义。
"""

from __future__ import annotations

import pytest

from xenon.mcp.registry import MCPRegistry


class TestServerNameValidation:
    def test_normal_stdio_name_accepted(self):
        reg = MCPRegistry()
        reg.add_server_pending("fs", command="npx", args=["-y", "srv"])
        assert reg.get_pending_server_names() == ["fs"]

    def test_url_mode_accepted(self):
        reg = MCPRegistry()
        reg.add_server_pending("web", url="http://localhost:3000/sse")
        assert "web" in reg.get_pending_server_names()

    @pytest.mark.parametrize("name", ["", "   "])
    def test_empty_name_rejected(self, name):
        reg = MCPRegistry()
        with pytest.raises(ValueError, match="不能为空"):
            reg.add_server_pending(name, command="npx")

    def test_whitespace_padded_name_rejected(self):
        reg = MCPRegistry()
        with pytest.raises(ValueError, match="空白"):
            reg.add_server_pending(" fs ", command="npx")

    def test_colon_name_rejected(self):
        """':' 与 server:tool 命名空间冲突——call_tool 按 split(':',1) 解析。"""
        reg = MCPRegistry()
        with pytest.raises(ValueError, match="命名空间"):
            reg.add_server_pending("a:b", command="npx")
        # 不得残留 pending
        assert reg.get_pending_server_names() == []

    def test_missing_command_and_url_rejected(self):
        reg = MCPRegistry()
        with pytest.raises(ValueError, match="需要 command 或 url"):
            reg.add_server_pending("z")
        assert reg.get_pending_server_names() == []

    def test_add_server_immediate_also_validates(self):
        reg = MCPRegistry()
        with pytest.raises(ValueError, match="不能为空"):
            reg.add_server("", command="npx")

    def test_duplicate_name_still_deduplicated(self):
        reg = MCPRegistry()
        reg.add_server_pending("fs", command="npx")
        reg.add_server_pending("fs", command="npx")  # 幂等跳过，不报错
        assert reg.get_pending_server_names() == ["fs"]


class TestShortcutNameValidation:
    """快捷指令名会动态注册成 /<name> 斜杠命令，此前 create() 对
    ../evil、a/b、空名零校验，污染命令命名空间。"""

    @pytest.fixture
    def manager(self, tmp_path):
        from xenon.repl.shortcut_manager import ShortcutManager
        return ShortcutManager(path=tmp_path / "shortcuts.yaml")

    def test_normal_and_unicode_names(self, manager):
        manager.create("deploy", "d", ["echo hi"])
        manager.create("我的命令", "d", ["echo hi"])
        manager.create("my-cmd_2", "d", ["echo hi"])
        assert set(manager.shortcuts) == {"deploy", "我的命令", "my-cmd_2"}

    @pytest.mark.parametrize("name", ["../evil", "a/b", "a:b", "a\\b"])
    def test_path_like_names_rejected(self, manager, name):
        with pytest.raises(ValueError, match="快捷指令名"):
            manager.create(name, "d", ["echo hi"])
        assert manager.shortcuts == {}

    def test_empty_name_rejected(self, manager):
        with pytest.raises(ValueError, match="不能为空"):
            manager.create("", "d", ["echo hi"])

    def test_empty_steps_rejected(self, manager):
        with pytest.raises(ValueError, match="至少需要一个执行步骤"):
            manager.create("ok", "d", [])


class TestModelPoolFromConfigValidation:
    """from_config 此前对用户 YAML 零校验：顶层 list / entry 为字符串
    泄漏 AttributeError；weight 为字符串/负数静默通过破坏加权调度。"""

    def test_normal_config(self):
        from xenon.repl.model_pool import ModelPool
        p = ModelPool()
        p.from_config({"pro": {"model_id": "deepseek/v4-pro", "weight": 5.0}})
        assert len(p._entries) == 1

    def test_top_level_list_rejected(self):
        from xenon.repl.model_pool import ModelPool
        with pytest.raises(ValueError, match="模型池配置格式错误"):
            ModelPool().from_config(["x"])

    def test_non_dict_entry_rejected(self):
        from xenon.repl.model_pool import ModelPool
        with pytest.raises(ValueError, match="模型 'pro' 的配置格式错误"):
            ModelPool().from_config({"pro": "just-a-string"})

    def test_string_weight_rejected(self):
        from xenon.repl.model_pool import ModelPool
        with pytest.raises(ValueError, match="weight 必须是数字"):
            ModelPool().from_config({"pro": {"model_id": "a/b", "weight": "high"}})

    def test_negative_weight_rejected(self):
        from xenon.repl.model_pool import ModelPool
        with pytest.raises(ValueError, match="weight 必须为正数"):
            ModelPool().from_config({"pro": {"model_id": "a/b", "weight": -5}})

    def test_numeric_string_weight_coerced(self):
        from xenon.repl.model_pool import ModelPool
        p = ModelPool()
        p.from_config({"pro": {"model_id": "a/b", "weight": "2.5"}})
        assert p._entries["pro"].weight == 2.5


class TestPermissionGateUnknownRisk:
    """risk_override 非法值曾静默 fallthrough 到「允许」。

    risk_override 是 ToolExecutor 掌握的最高风险信息（覆盖运行时注册的
    动态工具——名字不在静态工具表中）。权限层遇到无法识别的风险等级
    必须 fail-closed，而非放行。
    """

    @pytest.fixture
    def gate(self):
        import logging
        # disable() 是进程级全局开关，必须配对恢复——否则后续任何依赖
        # 日志捕获的测试（test_runtime_resilience 等）会因 root logger
        # 被静默而失败（全套件跑时的顺序依赖污染）。
        logging.disable(logging.CRITICAL)
        try:
            from xenon.repl.permissions import PermissionGate
            yield PermissionGate()
        finally:
            logging.disable(logging.NOTSET)

    @pytest.mark.parametrize("risk", ["BOGUS_RISK", "bogus", "Write", "critical "])
    def test_unknown_risk_fails_closed(self, gate, risk):
        allowed, reason = gate.check("mcp_call", {"server": "s"}, risk_override=risk)
        assert allowed is False
        assert "CRITICAL" in reason

    @pytest.mark.parametrize(
        ("risk", "expected_allowed"),
        [("READ", True), ("WRITE", False), ("CRITICAL", False)],
    )
    def test_valid_risks_unchanged(self, gate, risk, expected_allowed):
        # DEFAULT 模式无确认回调：READ 放行，WRITE/CRITICAL 要求确认
        allowed, _ = gate.check("mcp_call", {"server": "s"}, risk_override=risk)
        assert allowed is expected_allowed


class TestDifficultyEstimatorBoundary:
    """空输入与重复刷屏内容不应被调度成困难任务。

    空串此前落到 intent_base 默认 0.3 + 基础 0.3 = 0.6（tier 3 标准任务）；
    "x"*100000 因长度加分冲到 tier 5——长度是信息密度的弱信号，
    重复内容长度大但信息量为零。
    """

    @pytest.mark.parametrize("text", ["", "   ", "\n\n  \t"])
    def test_empty_input_is_trivial(self, text):
        from xenon.repl.difficulty_estimator import DifficultyEstimator
        est = DifficultyEstimator()
        tier = DifficultyEstimator.estimate_tier(est.estimate(text, []))
        assert tier == 1

    @pytest.mark.parametrize("text", ["x" * 100000, "🎉" * 20000, "aaaa" * 5000])
    def test_repetitive_flood_not_hard(self, text):
        from xenon.repl.difficulty_estimator import DifficultyEstimator
        est = DifficultyEstimator()
        tier = DifficultyEstimator.estimate_tier(est.estimate(text, []))
        assert tier <= 3

    def test_real_complex_task_still_tier5(self):
        from xenon.repl.difficulty_estimator import DifficultyEstimator
        est = DifficultyEstimator()
        tier = DifficultyEstimator.estimate_tier(
            est.estimate("帮我重构整个项目的架构，考虑性能和并发安全", [])
        )
        assert tier == 5


class TestEngineRunInputValidation:
    """引擎 run() 对 None/非字符串输入的统一防御。

    Bug 背景：ReActEngine.run(None) 此前穿透到 execution_policy.strip_execution_boundary
    的 text.split() 裸崩 AttributeError（react_engine.py:338 → execution_policy.py:53）。
    根因是 base.py:1521 的 run(user_input: str) 契约无入口校验，各引擎假设上游
    （REPL/工作流/会话重放）总传 str。根因级修复：BaseEngine 提供统一校验，
    全部 6 个引擎 run() 首行调用。
    """

    def _make_engine(self):
        from xenon.engine.react_engine import ReActEngine
        return ReActEngine(["prov/m1"])

    @pytest.mark.parametrize("bad_input", [None, 123, ["task"]])
    def test_react_run_rejects_non_str(self, bad_input):
        eng = self._make_engine()
        with pytest.raises(ValueError, match="user_input 必须为非空字符串"):
            eng.run(bad_input)

    def test_react_run_rejects_empty_string(self):
        eng = self._make_engine()
        with pytest.raises(ValueError, match="user_input 必须为非空字符串"):
            eng.run("   ")

    def test_all_engine_runs_validate(self):
        """全部 6 个引擎 run() 首行都应调用统一校验。"""
        import inspect
        from xenon.engine.react_engine import ReActEngine
        from xenon.engine.plan_execute_engine import PlanExecuteEngine
        from xenon.engine.reflection_engine import ReflectionEngine
        from xenon.engine.combined_engines import (
            PlanReactEngine,
            PlanReflectionEngine,
            ReactReflectionEngine,
        )

        for cls in (
            ReActEngine,
            PlanExecuteEngine,
            ReflectionEngine,
            PlanReactEngine,
            PlanReflectionEngine,
            ReactReflectionEngine,
        ):
            src = inspect.getsource(cls.run)
            assert "_validate_run_input" in src, (
                f"{cls.__name__}.run 未调用统一输入校验"
            )

    def test_validation_in_base_engine(self):
        from xenon.engine.base import BaseEngine
        assert hasattr(BaseEngine, "_validate_run_input")
