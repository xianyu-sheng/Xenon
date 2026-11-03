# 决策链提取器（#289）：compaction 不丢"为什么"

## 为什么改

compaction 折叠事件时，`plan_accepted` / `control_decision`（含 steering）/
`debt_opened` 的**原因**被压成 `seq=… elided` 一行——模型读到压缩后的视图，
回答不了"为什么任务走到了这一步"。这四个事件恰好承载答案。

原则：折叠决策事件时，**保留它们的一句话原因**（`why` 行），零 LLM、纯函数。

## 真实运行（deepseek/deepseek-v4-flash，3 次）

```text
task phase: finalized  accepted: True
decision events in ledger: 1 -> ['control_decision']
elided entries: 1; why lines kept: 0
decisions kept in the view (not folded): 1
PASS: every decision is visible — folded ones via why, others as events
```

真实任务短，deliver 决策作为最后事件**保留在视图**（模型直接看到完整原因）；
被折叠的是 tool_receipt 等无决策事件 → why 空是正确行为。折叠决策事件的
`why` 内容由单元测试直接断言：

```text
plan_accepted: 2 step(s)
control deliver: all claims verified
debt requirement_met tests/test_x.py: no successful tool receipt
```

## 实现

- `xenon/kernel/decision_chain.py`：`decision_lines(dropped)` —— 纯函数，从
  折叠的 summaries 里提取三类决策事件生成一行原因（原因 clip 到 200 字符）。
- `xenon/kernel/compaction.py::fold_dropped`：把 `why` 行挂到 digest 条目；
  预算紧张时 `why` 按行让步（从后往前删），但至少保留事实（seqs）+ 能放下的
  `why`，绝不静默全丢。
- `xenon/kernel/view/base.py::_summary`：修掉一个**真实 bug** —— `control_decision`
  和 `debt_opened` 的 payload `kind` 覆盖了 `item["kind"]`（事件类型），导致：
  1. 事件类型丢失（`elided_line` 显示 kind=deliver 而非 control_decision）；
  2. **`_priority` 里的 `kind in {"debt_opened"}` 永不命中 —— 债务事件从来没被
     pin，原因随折叠丢**（正是决策链要解决的问题）。
  现在 payload kind 存到 `item["decision"]` / `item["debt_kind"]`。

## 测试

`tests/kernel/test_decision_chain.py`（8）：三类决策的 why 行、非决策事件无 why、
原因截断、折叠保留 why、预算紧张时让步、顺序保持。

全量：3776 passed, 97 skipped；ruff / 三重守卫通过。
