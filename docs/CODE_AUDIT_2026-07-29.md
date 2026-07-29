# Xenon 整体代码审查：死代码与 Bug

本次审查覆盖 `xenon/engine`、`xenon/repl`、`xenon/utils` 及对应测试，重点追踪
用户反馈的长任务中断、格式重试异常和 Ctrl+O 无响应。

## 已确认并修复

1. `ReActEngine.run()` 在无法解析工具调用时调用了不存在的
   `BudgetManager.on_retry()`，导致 `AttributeError` 直接终止任务。现在该方法
   作为受 2× 上限约束的协议重试奖励实现，并有单测覆盖。
2. 交互式 ReAct 原来固定 `max_iterations=10`。中等长度任务容易在格式重试、
   工具验证和收束阶段耗尽；交互路径提升为 15，仍受 BudgetManager 上限约束。
3. `PlanExecuteEngine._plan()` 对无效/截断/非 JSON 规划只调用一次并直接返回空计划，
   导致 Plan+React 根本不进入工具链。现在执行一次有界的 JSON 契约恢复重试，仍然
   fail-closed，不会把模型文本当成计划。
4. `XENON_NO_PT=1` 或 prompt_toolkit 初始化失败时，原始终端输入路径没有处理
   Ctrl+O。现在 fallback 输入循环也能展开/折叠最近一次执行详情，并在输出后重画
   当前输入；异常引擎退出也会保留 ThinkingPanel。
5. 子 Agent timeout 原来使用 `with ThreadPoolExecutor`，超时后离开上下文仍会
   `shutdown(wait=True)`，所以表面超时、实际继续阻塞主 REPL。现在超时路径使用
   `shutdown(wait=False, cancel_futures=True)`，并明确返回超时结果。
6. ReAct 返回工具数组时，并行执行现在代码级检查 `_PARALLEL_SAFE_TOOLS`；包含
   写入、命令或未知工具的数组改走有序串行路径，不能仅依赖 prompt 约束。

## 已确认但尚未改动的高风险项

- OpenAI 兼容客户端对 `finish_reason=length` 的续写是普通文本拼接。对于 JSON/DSML
  工具协议，截断点可能位于结构中间，拼接后仍不可解析。需要下一阶段做协议感知的
  续写或提高结构化响应预算，并用真实 DeepSeek 长任务回归。
- 原生工具协议消息存在 `role=tool` / `tool_calls` 时，当前 in-run 压缩会跳过，
  长任务消息可能持续增长到上下文上限。不能直接删除消息，必须保留成对的工具协议
  记录后再设计安全裁剪/摘要。
- Plan-Execute DAG 波次目前仍按显式并行配置执行，尚未复用上述 ReAct 工具白名单；
  planner 错误地把写步骤放进同一波次时仍有工作区竞态风险。
- 组合引擎的各子阶段缺少统一的全局 deadline/aggregate budget，长任务可能在多个
  子引擎之间累积超时。需要单独设计跨引擎预算，不用简单增加单个 max_steps。

## 死代码结论

- `NovelEngine`、`NovelManager`、novel 执行模式和命令已移除；当前执行图中没有可达
  的 novel 引擎死代码。
- `prompt_optimizer`、`difficulty_estimator`、`model_pool` 中出现的 novel 字样属于
  通用创作意图分类/权重，不是已删除执行引擎残留，不能误删。
- `_PARALLEL_SAFE_TOOLS` 曾是未接入的安全配置，本次已接入 ReAct 并行分支。

## 验证

- 离线回归：`1711 passed, 38 deselected`（`pytest -q -m 'not live'`）
- 目标回归：Budget、planner recovery、Ctrl+O PTY、ReAct native degradation 共 98 passed
- Ruff：通过
- `git diff --check`：通过

完整 `pytest -q` 的 13 个失败均为 `live` 网络测试，在当前沙箱中统一报
`[Errno 1] Operation not permitted`，不是离线代码回归。
