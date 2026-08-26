# Pattern 1 状态同步问题修复计划

## 测试结果分析

### ✅ 已通过 (4/8)
1. ✅ test_fix1_context_manager_trim_updates_cache_epoch
2. ✅ test_fix3_context_manager_compact_clears_prompt_lanes  
3. ✅ test_fix4_context_manager_compact_cleans_event_log
4. ✅ test_fix5_status_bar_refresh_called_on_state_changes

### ❌ 需要修复 (4/8)

#### 问题2: AutoRouter._last_successful_model_id 不清空
**文件**: `xenon/repl/auto_router.py`
**方法**: `reset_session_lock()`
**修复**: 在 `reset_session_lock()` 中添加 `self._last_successful_model_id = None`

#### 问题5: ContextManager 缺少回调机制 (2个测试)
**文件**: `xenon/repl/context_manager.py`
**需要添加**:
- `add_state_change_callback()` 方法
- `_state_change_callbacks` 列表
- 在状态变更时调用回调

#### 问题6: ModelPool 缺少移除回调 (2个测试)
**文件**: `xenon/repl/model_pool.py`
**需要添加**:
- `add_removal_callback()` 方法
- `remove_removal_callback()` 方法
- `_removal_callbacks` 列表
- 在 `remove_model()` 时调用回调

## 修复顺序

1. **问题2: AutoRouter** - 最简单，一行代码
2. **问题5: ContextManager 回调** - 中等复杂度
3. **问题6: ModelPool 回调** - 中等复杂度

## 工作流

每个问题：
1. 读取源文件
2. 理解现有代码结构
3. 实现修复
4. 运行对应的测试
5. 运行全量测试确保无回归
6. 提交

准备好了吗？从问题2开始？
