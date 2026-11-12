"""MCP 服务器名与配置校验回归测试。

Bug 背景：add_server_pending 是惰性注册（不连接），此前对名字零校验，
空名/含 ':' 的名字会静默进入 pending，直到首次工具调用才以晦涩方式
失败；':' 名还会与 server:tool 命名空间路由产生歧义。
"""

from __future__ import annotations

import pytest

from xenon.kernel.contracts import TaskSpec
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

    @pytest.mark.parametrize(
        "text",
        ["x" * 100000, "🎉" * 20000, "aaaa" * 5000],
        ids=["x-100k", "emoji-20k", "aaaa-20k"],
    )
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


class TestTaskSpecRejectsMalformedInput:
    """The kernel validates at the boundary, not three layers down.

    The legacy engines learned this from a real crash: ``run(None)`` fell
    through to execution-policy text handling instead of saying "your
    objective is not a string".
    """

    @pytest.mark.parametrize("bad_input", [None, 123, ["task"]])
    def test_objective_must_be_a_string(self, bad_input):
        with pytest.raises(ValueError, match="非空字符串"):
            TaskSpec(task_id="t", objective=bad_input, workspace=".")

    @pytest.mark.parametrize("bad_input", ["", "   "])
    def test_objective_must_not_be_blank(self, bad_input):
        with pytest.raises(ValueError, match="非空字符串"):
            TaskSpec(task_id="t", objective=bad_input, workspace=".")

    def test_task_id_must_not_be_blank(self):
        with pytest.raises(ValueError, match="非空字符串"):
            TaskSpec(task_id="  ", objective="do it", workspace=".")

    def test_a_valid_spec_still_builds(self):
        spec = TaskSpec(task_id="t", objective="do it", workspace=".")

        assert spec.objective == "do it"
