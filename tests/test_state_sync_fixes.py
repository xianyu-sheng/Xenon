"""测试模式1的6个状态同步问题修复。"""

from xenon.repl.context_manager import ContextManager
from xenon.repl.auto_router import AutoRouter
from xenon.repl.model_pool import ModelPool
from xenon.repl.status_bar import StatusBar
from rich.console import Console


class TestStateSyncFixes:
    """模式1: 状态同步失效修复测试。"""

    def test_fix1_context_manager_trim_updates_cache_epoch(self):
        """问题1: ContextManager.trim 不更新 cache_epoch - 修复后应更新。"""
        ctx = ContextManager()
        ctx.add_user_message("test user")
        ctx.add_assistant_message("test assistant")
        initial_epoch = ctx.cache_epoch

        # trim_last_assistant 应该更新 cache_epoch
        ctx.trim_last_assistant()
        assert ctx.cache_epoch > initial_epoch, (
            "trim_last_assistant 应该更新 cache_epoch"
        )

        ctx.add_user_message("test user 2")
        epoch_before = ctx.cache_epoch
        ctx.trim_last_user()
        assert ctx.cache_epoch > epoch_before, "trim_last_user 应该更新 cache_epoch"

    def test_fix2_auto_router_clears_last_successful_model_on_circuit_break(self):
        """问题2: AutoRouter._last_successful_model_id 不同步 - 熔断时应清空。"""
        pool = ModelPool()
        pool.register("test/model-a", alias="model-a")
        router = AutoRouter(model_pool=pool)

        # 直接设置 _last_successful_model_id（模拟已记录成功的模型）
        router._last_successful_model_id = "test/model-a"
        assert router._last_successful_model_id == "test/model-a"

        # 重置会话锁应该清空 _last_successful_model_id
        router.reset_session_lock()
        assert router._last_successful_model_id is None

    def test_fix3_context_manager_compact_clears_prompt_lanes(self):
        """问题3: ContextManager压缩后prompt_lanes未失效 - 应清空。"""
        ctx = ContextManager()

        # 添加足够多的消息以触发实际的压缩逻辑
        for i in range(10):
            ctx.add_user_message(f"test message {i}")
            ctx.add_assistant_message(f"test response {i}")

        # 记录压缩前的 lanes 对象
        lanes_before = ctx.prompt_lanes
        ctx.prompt_lanes.prepare(
            "test/model",
            "direct",
            "main",
            ctx.cache_epoch,
            [{"role": "user", "content": "test"}],
            event_cursor=ctx.event_cursor,
        )

        # 确认有 lane 数据
        assert len(lanes_before.snapshots()) > 0, "压缩前应该有 lane 数据"

        # compact 应该创建新的 prompt_lanes 对象（强制压缩）
        ctx.compact(summary="test summary")

        # 验证 prompt_lanes 是一个新对象（不是同一个引用）
        assert ctx.prompt_lanes is not lanes_before, (
            "compact 后 prompt_lanes 应该是新对象"
        )

        # 新的 prompt_lanes 应该是空的
        assert len(ctx.prompt_lanes.snapshots()) == 0, (
            "compact 后新的 prompt_lanes 应该为空"
        )

    def test_fix4_context_manager_compact_cleans_event_log(self):
        """问题4: ContextManager压缩后event_log未清理 - 应清理旧事件。"""
        ctx = ContextManager()

        # 添加大量消息以产生事件
        for i in range(150):
            ctx.add_user_message(f"message {i}")
            ctx.add_assistant_message(f"response {i}")

        # compact 应该清理旧事件（只保留最近100个）
        ctx.compact(summary="test summary")

        events = ctx.event_log.snapshot()
        assert len(events) <= 100, (
            f"compact 后 event_log 应该只保留最近100个事件，实际: {len(events)}"
        )

    def test_fix5_status_bar_refresh_called_on_state_changes(self):
        """问题5: StatusBar.refresh时机不完整 - 所有状态变更点应调用refresh。"""
        ctx = ContextManager()
        console = Console()
        from xenon.repl.model_registry import ModelRegistry

        registry = ModelRegistry()
        status_bar = StatusBar(console, ctx, registry)

        refresh_count = [0]

        def mock_refresh():
            refresh_count[0] += 1

        # 替换 refresh 方法
        status_bar.refresh = mock_refresh

        # 测试各种状态变更都会调用 refresh
        status_bar.set_last_model("test/model")
        assert refresh_count[0] == 1, "set_last_model 应该调用 refresh"

        status_bar.set_streaming(False)
        assert refresh_count[0] == 2, "set_streaming 应该调用 refresh"

        status_bar.set_mode_notification("test")
        assert refresh_count[0] == 3, "set_mode_notification 应该调用 refresh"

        status_bar.add_tool_call()
        assert refresh_count[0] == 4, "add_tool_call 应该调用 refresh"

    def test_fix5_context_manager_notifies_callbacks(self):
        """问题5: ContextManager 应该在状态变更时通知回调。"""
        ctx = ContextManager()
        notification_count = [0]

        def callback():
            notification_count[0] += 1

        ctx.add_state_change_callback(callback)

        # add_message 应该触发通知
        ctx.add_user_message("test")
        assert notification_count[0] == 1, "add_message 应该触发回调"

        # compact 应该触发通知（添加足够多的消息以触发实际压缩）
        for i in range(10):
            ctx.add_user_message(f"test {i}")
            ctx.add_assistant_message(f"response {i}")
        notification_count[0] = 0
        ctx.compact(summary="test")
        assert notification_count[0] == 1, "compact 应该触发回调"

        # undo 应该触发通知
        notification_count[0] = 0
        ctx.save_snapshot()
        ctx.add_user_message("test2")
        ctx.undo()
        assert notification_count[0] == 2, "undo 应该触发回调（add_message + undo）"

        # clear 应该触发通知
        notification_count[0] = 0
        ctx.clear()
        assert notification_count[0] == 1, "clear 应该触发回调"

    def test_fix6_model_pool_notifies_on_removal(self):
        """问题6: ModelPool.remove_model不通知依赖方 - 应添加回调。"""
        pool = ModelPool()
        pool.register("test/model-a", alias="model-a")
        pool.register("test/model-b", alias="model-b")

        removed_models = []

        def on_removal(model_id: str):
            removed_models.append(model_id)

        pool.add_removal_callback(on_removal)

        # unregister 应该触发回调
        pool.unregister("model-a")
        assert "test/model-a" in removed_models, "unregister 应该触发回调"

        # evict_permanently 应该触发回调
        pool.evict_permanently("model-b")
        assert "test/model-b" in removed_models, "evict_permanently 应该触发回调"

    def test_fix6_model_pool_callback_removal(self):
        """问题6: 测试回调的添加和移除。"""
        pool = ModelPool()
        pool.register("test/model", alias="model")

        call_count = [0]

        def callback(model_id: str):
            call_count[0] += 1

        # 添加回调
        pool.add_removal_callback(callback)
        pool.unregister("model")
        assert call_count[0] == 1

        # 移除回调后不应再触发
        pool.register("test/model2", alias="model2")
        pool.remove_removal_callback(callback)
        pool.unregister("model2")
        assert call_count[0] == 1, "移除回调后不应再触发"
