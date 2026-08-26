"""P1测试：验证状态栏同步功能。

测试场景：
1. 模型切换后状态栏更新
2. fallback时状态栏显示实际使用的模型
3. 压缩后状态栏无突变
4. /model命令后状态栏更新
5. /mode命令后状态栏更新
"""

from __future__ import annotations

import io

from rich.console import Console

from xenon.repl.context_manager import ContextManager
from xenon.repl.model_registry import ModelRegistry
from xenon.repl.status_bar import StatusBar
from xenon.repl.model_pool import ModelPool
from xenon.repl.auto_router import AutoRouter


def _make_status_bar(ctx_mgr=None, registry=None, usage_tracker=None):
    """创建测试用的 StatusBar 实例。"""
    console = Console(file=io.StringIO(), width=120, force_terminal=False)
    cm = ctx_mgr or ContextManager()
    reg = registry or ModelRegistry()
    return StatusBar(console, cm, reg, usage_tracker=usage_tracker)


def test_scenario_1_model_switch_updates_statusbar():
    """场景1：模型切换后状态栏更新。"""
    bar = _make_status_bar()

    # 初始状态
    initial = bar.render()
    assert "未设置" in str(initial.renderable)

    # 切换模型
    bar.set_last_model("anthropic/claude-3-5-sonnet-20241022")
    updated = bar.render()
    content = str(updated.renderable)

    # 验证状态栏包含新模型（模型名会被截断显示为 ...de-3-5-sonnet-20241022）
    assert "sonnet" in content or "claude" in content or "anthropic" in content
    assert "未设置" not in content


def test_scenario_2_fallback_shows_actual_used_model():
    """场景2：fallback时状态栏显示实际使用的模型。"""
    pool = ModelPool()
    pool.register("provider_a/model-pro", alias="pro", weight=5.0)
    pool.register("provider_b/model-mini", alias="mini", weight=0.5)

    registry = ModelRegistry()
    bar = _make_status_bar(registry=registry)

    # 模拟主模型失败，fallback到备用模型
    bar.set_last_model("provider_a/model-pro")
    first = bar.render()
    assert "model-pro" in str(first.renderable)

    # 切换到备用模型
    bar.set_last_model("provider_b/model-mini")
    fallback = bar.render()
    content = str(fallback.renderable)

    # 验证状态栏显示实际使用的备用模型
    assert "model-mini" in content
    assert "model-pro" not in content


def test_scenario_3_compact_no_statusbar_mutation():
    """场景3：压缩后状态栏无突变。

    注意：此测试验证状态栏在上下文变化（如消息增加）后保持模型信息稳定。
    由于 compact() 方法依赖完整的 ContextManager 初始化，此处测试模拟
    压缩场景（大量消息）而不实际调用压缩。
    """
    cm = ContextManager()
    registry = ModelRegistry()
    bar = _make_status_bar(ctx_mgr=cm, registry=registry)

    # 设置模型
    bar.set_last_model("test/model-x")

    # 添加消息（模拟会话增长）
    for i in range(10):
        cm.add_user_message(f"message {i}")
        cm.add_assistant_message(f"response {i}")

    # 检查状态栏在大量消息后的稳定性
    before = bar.render()
    before_content = str(before.renderable)
    model_before = "test/model-x"
    assert model_before in before_content

    # 再添加更多消息（模拟继续使用同一模型）
    for i in range(10, 20):
        cm.add_user_message(f"message {i}")
        cm.add_assistant_message(f"response {i}")

    # 验证模型信息保持不变
    after = bar.render()
    after_content = str(after.renderable)

    # 验证模型信息未突变
    assert model_before in after_content
    # 验证其他核心信息仍然存在
    assert "模型:" in after_content or "Token:" in after_content
    # 验证消息数正确增长
    assert "消息:" in after_content


def test_scenario_4_model_command_updates_statusbar():
    """场景4：/model命令后状态栏更新。

    这个测试模拟 /model 命令执行后的状态栏更新流程。
    """
    registry = ModelRegistry()
    pool = ModelPool()

    # 注册多个模型
    pool.register("provider_a/gpt-4o", alias="gpt4", weight=5.0)
    pool.register("provider_b/claude-3", alias="claude", weight=4.0)

    registry.add_model("provider_a/gpt-4o", "gpt4")
    registry.add_model("provider_b/claude-3", "claude")

    bar = _make_status_bar(registry=registry)

    # 初始模型
    bar.set_last_model("provider_a/gpt-4o")
    before = bar.render()
    assert "gpt-4o" in str(before.renderable)

    # 模拟 /model 命令切换到 claude
    bar.set_last_model("provider_b/claude-3")
    after = bar.render()
    after_content = str(after.renderable)

    # 验证状态栏已更新
    assert "claude-3" in after_content
    assert "gpt-4o" not in after_content


def test_scenario_5_mode_command_updates_statusbar():
    """场景5：/mode命令后状态栏更新。"""
    registry = ModelRegistry()
    bar = _make_status_bar(registry=registry)

    # 初始范式（默认direct）
    initial = bar.render()
    initial_content = str(initial.renderable)
    assert "范式:" in initial_content
    assert "direct" in initial_content.lower()

    # 切换范式到 react
    registry.set_mode("react")
    after = bar.render()
    after_content = str(after.renderable)

    # 验证状态栏显示新范式
    assert "范式:" in after_content
    assert "react" in after_content.lower()

    # 再切换到 plan-execute
    registry.set_mode("plan-execute")
    final = bar.render()
    final_content = str(final.renderable)

    assert (
        "plan-execute" in final_content.lower()
        or "plan_execute" in final_content.lower()
    )


def test_statusbar_refresh_after_state_changes():
    """验证状态变更后状态栏仍能正常渲染。

    注意：StatusBar.refresh() 是占位方法，实际刷新由 prompt_toolkit 自动处理。
    此测试验证各种状态变更后状态栏仍能正常渲染。
    """
    bar = _make_status_bar()

    # 各种状态变更都应该能安全执行
    bar.set_last_model("test/model")
    bar.set_streaming(False)
    bar.set_mode_notification("test-mode")
    bar.add_tool_call()

    # 所有操作后状态栏应该仍能正常渲染
    panel = bar.render()
    assert panel is not None
    content = str(panel.renderable)
    # 验证状态变更已生效
    assert "test/model" in content or "test" in content


def test_statusbar_with_auto_router():
    """验证状态栏与 AutoRouter 集成时的显示。"""
    pool = ModelPool()
    pool.register("provider/model-a", alias="a", weight=5.0)
    pool.register("provider/model-b", alias="b", weight=3.0)

    router = AutoRouter(pool)
    registry = ModelRegistry()
    bar = _make_status_bar(registry=registry)
    bar._auto_router = router

    # 设置最后使用的模型
    bar.set_last_model("provider/model-a")

    panel = bar.render()
    content = str(panel.renderable)

    # 当有 auto_router 且非空时，应显示 auto 标识
    if not router.is_empty():
        assert "auto" in content or "model-a" in content


def test_statusbar_toolbar_fragments():
    """验证 get_toolbar_fragments 返回正确格式。"""
    cm = ContextManager()
    registry = ModelRegistry()
    bar = _make_status_bar(ctx_mgr=cm, registry=registry)

    bar.set_last_model("test/model")

    # get_toolbar_fragments 应该返回 list[tuple[str, str]]
    fragments = bar.get_toolbar_fragments()
    assert isinstance(fragments, list)

    if fragments:
        for item in fragments:
            assert isinstance(item, tuple)
            assert len(item) == 2
            assert isinstance(item[0], str)  # style
            assert isinstance(item[1], str)  # text


def test_statusbar_exception_safety():
    """验证状态栏在异常情况下的降级处理。"""

    class BrokenCtx:
        def stats(self):
            raise RuntimeError("Simulated failure")

        def __getattr__(self, name):
            raise AttributeError(name)

    registry = ModelRegistry()
    bar = _make_status_bar(ctx_mgr=BrokenCtx(), registry=registry)

    # render() 应该返回降级面板而不是抛异常
    panel = bar.render()
    assert panel is not None
    assert "状态不可用" in str(panel.renderable)

    # get_toolbar_fragments() 也应该降级
    fragments = bar.get_toolbar_fragments()
    assert isinstance(fragments, list)
    assert any("状态不可用" in str(frag) for frag in fragments)


def test_model_switch_maintains_other_state():
    """验证模型切换不影响其他状态项。"""
    cm = ContextManager()
    registry = ModelRegistry()
    bar = _make_status_bar(ctx_mgr=cm, registry=registry)

    # 设置一些状态
    cm.add_user_message("test message 1")
    cm.add_assistant_message("test response 1")
    bar.add_tool_call()
    bar.add_tool_call()

    # 切换模型前
    bar.set_last_model("model_a")
    before = bar.render()
    before_content = str(before.renderable)

    # 验证工具调用计数存在
    assert "🔧" in before_content or "tools" in before_content.lower()

    # 切换模型
    bar.set_last_model("model_b")
    after = bar.render()
    after_content = str(after.renderable)

    # 验证工具调用计数仍然存在
    assert "🔧" in after_content or "tools" in after_content.lower()
    # 验证消息数仍然存在
    assert "消息:" in after_content or "message" in after_content.lower()
