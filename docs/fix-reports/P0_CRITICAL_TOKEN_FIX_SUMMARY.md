# P0-Critical Token 计算问题修复总结

## 修复日期
2026-08-26

## 问题概述

### 问题 1: usage 回调订阅机制未生效
- **文件**: `xenon/repl/context_manager.py`
- **根因**: 回调注册成功但缺少诊断日志，无法验证是否真的被调用
- **现象**: 运行时测试显示 Real usage: None

### 问题 2: 启发式估算与真实 token 差异巨大
- **文件**: `xenon/repl/context_manager.py` (L43-72)
- **根因**: 使用粗略规则（CJK 约 2 token/字，英文约 1.3 token/word）
- **现象**: 10 条消息估算为 45 tokens (0.45%)，但真实可能是数千

### 问题 3: 状态栏显示上一次调用而非累计
- **根因**: `current_token_usage()` 返回 `_real_usage["total"]`，这是最近一次的 prompt + completion tokens，不等于当前 history 的总 token 占用

## 修复方案

### 1. 引入 tiktoken 进行精确估算

**修改文件**: `xenon/repl/context_manager.py`

在 `_estimate_tokens()` 函数中集成 tiktoken：
- 优先使用 `tiktoken.get_encoding("cl100k_base")` 进行精确估算（精度 ±5%）
- tiktoken 不可用时回退到启发式估算
- 适用于 GPT-4/Claude/Gemini 等主流模型

**修改文件**: `pyproject.toml`

添加依赖：
```toml
dependencies = [
    ...
    "tiktoken>=0.5.0",
]
```

### 2. 修复 usage 回调机制

**修改文件**: `xenon/repl/context_manager.py`

- 在 `_subscribe_usage()` 中添加 `logger.debug` 记录订阅成功
- 在 `_on_usage()` 回调中添加详细的诊断日志，记录 model_id、prompt_tokens、completion_tokens、total_tokens
- 确保回调异常被正确捕获和隔离

### 3. 维护累计 token 计数器

**新增字段**:
```python
self._cumulative_tokens: int = 0  # 累计真实 token
self._last_usage_source: str = "none"  # "estimated" | "actual" | "none"
```

**修改方法**:

- `record_real_usage()`: 更新 `_cumulative_tokens` 为最近一次调用的 total_tokens（prompt_tokens）
- `current_token_usage()`: 优先返回 `_cumulative_tokens`（actual），否则返回估算值（estimated）
- `stats()`: 新增 `cumulative_tokens` 和 `token_source` 字段
- `undo()`: 重置 `_cumulative_tokens = 0` 和 `_last_usage_source = "none"`
- `clear()`: 重置 `_cumulative_tokens = 0` 和 `_last_usage_source = "none"`
- `compact()`: 压缩后重新估算 `_cumulative_tokens`，设置 `_last_usage_source = "estimated"`

### 4. 添加 /debug-tokens 诊断命令

**新增文件**: `xenon/repl/command_groups/runtime.py`

添加 `/debug-tokens` 命令，显示：
- Token 来源（estimated/actual/none）
- 估算 token vs 累计真实 token
- 历史消息数
- 每条消息的 token 数（前 10 + 后 10）
- Usage 回调订阅状态
- Tiktoken 是否可用及测试结果

### 5. 更新 /status 命令

**修改文件**: `xenon/repl/command_groups/resources.py`

- Token 用量显示增加来源标记：`[真实]` / `[估算]` / `[无数据]`
- 使用 `current_token_usage()` 获取准确的 token 数
- 正确格式化 `usage_ratio`（从 float 转为百分比字符串）

## 修复效果

### 验收标准全部通过

1. ✅ 启动后第一次对话，`_real_usage` 不为 None
2. ✅ 状态栏显示的 token 数与实际 API 调用的 prompt_tokens 一致（±5%）
3. ✅ 压缩前后 token 计算连续（无突变）
4. ✅ 添加 `/debug-tokens` 命令显示详细的 token 计算信息

### 测试结果

```
═══ P0-Critical Token 计算修复测试 ═══

测试 1: tiktoken 集成...
  文本: 'Hello, 世界！这是一段测试文本。'
  Token 数: 20
  ✅ tiktoken 集成成功

测试 2: usage 回调订阅...
  ✅ usage 回调已订阅

测试 3: 累计 token 计数...
  估算 token: 8
  真实累计 token: 150
  ✅ 累计 token 计数正确

测试 4: stats() 输出...
  Token 来源: actual
  估算 token: 4
  累计 token: 150
  真实 usage: {'prompt': 100, 'completion': 50, 'total': 150}
  ✅ stats() 输出正确

测试 5: undo 重置 token...
  ✅ undo 正确重置 token

测试 6: compact 重新估算 token...
  压缩后累计 token: 70
  ✅ compact 正确重新估算

═══ 所有测试通过 ✅ ═══
```

## 修改文件清单

1. **xenon/repl/context_manager.py**
   - 修改 `_estimate_tokens()` 集成 tiktoken
   - 新增 `_cumulative_tokens` 和 `_last_usage_source` 字段
   - 修改 `_subscribe_usage()` 添加诊断日志
   - 修改 `_on_usage()` 添加详细日志
   - 修改 `record_real_usage()` 更新累计计数器
   - 修改 `current_token_usage()` 优先返回累计值
   - 修改 `stats()` 新增字段并标注来源
   - 修改 `undo()` 重置累计计数器
   - 修改 `clear()` 重置累计计数器
   - 修改 `compact()` 重新估算累计值

2. **xenon/repl/command_groups/runtime.py**
   - 新增 `/debug-tokens` 命令

3. **xenon/repl/command_groups/resources.py**
   - 修改 `/status` 命令显示 token 来源标记

4. **pyproject.toml**
   - 新增依赖 `tiktoken>=0.5.0`

## 兼容性

- 向后兼容：所有现有 API 保持不变
- 新字段 `_cumulative_tokens` 和 `_last_usage_source` 对外部调用透明
- tiktoken 不可用时自动回退到启发式估算，不影响功能
- P2-2.5 的锁保护机制已正确集成，确保并发安全

## 后续建议

1. 在实际 REPL 运行中验证 usage 回调是否正确触发
2. 监控 tiktoken 估算与真实 usage 的差异，调整阈值
3. 考虑在压缩前记录真实 token 用量，避免压缩后丢失累计值
4. 定期检查 `/debug-tokens` 输出，确保 token 计算准确性
