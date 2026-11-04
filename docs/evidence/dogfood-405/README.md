# Dogfood #5：模型想汇总，内核派它去读文件（#405, #407）

真实会话：`xenon` 0.9.6，cwd = `D:\mybook`（正常项目目录），模式 `plan-execute`，
模型 `deepseek/deepseek-v4-flash`。用户问「这个开源项目可以从哪些地方改进」。

界面输出：

```text
❌ policy veto: stuck: read called with identical arguments 5 times after 1 correction attempt
[4 consecutive calls produced no new information (last progress: none)]
💭 kernel · 14 steps · 12 tools · unverified

● Kernel · blocked
  任务未交付（phase=blocked）：stuck: read called with identical arguments 5 times …
```

用户的判断：「我感觉当前的 Xenon 距离真正可用依然很远。」

## 真实原因（ledger `repl-125425ecc94f.jsonl`）

**不是模型在乱转。** 第 1-8 步都是成功的工具调用（GitHub MCP 6 次 + 本地 read 1 次等），
第 9 步模型想做的事写在 `step_started` 里：

```json
{"step_id": 9, "kind": "think",
 "intent": {"capability": "read",
            "params": {"note": "汇总步骤1-8的可核验证据，区分【事实/推断/未知】输出改进建议…"},
            "targets": []}}
```

模型想**汇总已有证据**。它把 `capability` 填成了 `read`，`targets` 为空。

内核**不看 `kind`**，把 `capability=read` 绑成 `read_file` 去执行：

```text
seq 103 receipt failed  error="ValueError: [exec_read] read_file 需要 file_path"
seq 110 receipt failed  error="ValueError: [exec_read] read_file 需要 file_path"
seq 117 receipt failed  error="ValueError: [exec_read] read_file 需要 file_path"
seq 126 receipt failed  error="ValueError: [exec_read] read_file 需要 file_path"
seq 134 loop_detected   {"kind": "identical_call", "detail": "read called with identical arguments 5 times"}
seq 136 policy_decision {"kind": "veto", "reason": "stuck: read called with identical arguments 5 times…"}
```

## 三个叠在一起的缺陷

1. **绑定不看意图，只看 capability。** 一个没有 target 的 `read` 被派发执行，
   而不是在绑定阶段被拒。
2. **参数错误没有教 schema。** 模型连收 4 次同样的 `需要 file_path`，
   视图里没有告诉它 `read` 需要什么参数、或者它其实该用 `finish`。
3. **判死丢弃全部成果。** 8 步、12 次成功工具调用的证据，换来一句「任务未交付」。

## 修复

**① 绑定守卫（catalog）** —— `ToolSpec.requires_target`（`read` / `search` /
`apply_patch` / `read_file` / `search_files` / `write_file` / `edit_file`），
没有命名目标就不绑：

```text
status: unresolved
reason: read acts on something that must be named; this proposal named nothing
        (expected: file_path). If you already collected what you need, submit
        `finish` with the answer instead of calling another tool.
```

（具体参数名来自 `spec.params_schema`，内核里仍然不出现任何工具名/参数名 —— K005。）

**② 判死前先收尾（kernel）** —— 循环信号达到阈值且已有成功 receipt 时，不杀：
写 `control_decision {decision: "force_finish", receipts: N}`，下一份视图的 Notes 变成

> Tools are now closed (…). You have N successful receipt(s) in this run: answer
> with what they support and submit `finish`. Do not call another tool;
> unsupported statements will be reported as unverified, not silently accepted.

并且**当次动作不执行**（`_SKIP_ACTION`）；模型若仍要调工具，则 veto
`tools are closed after a detected loop` 并结束 —— 但此时它已经有作答机会。

## 行为对比

| | 修复前 | 修复后 |
|---|---|---|
| 无 target 的 read | 执行 4 次、4 条 failed receipt | 绑定阶段直接 unresolved + 教学理由 |
| 检测到循环 | 第 5 次直接判死 | 关闭工具、要求用现有证据作答 |
| 用户拿到 | 「任务未交付」 | 一段带证据标注的回答（或明确 unverified） |

## 测试

- `tests/catalog/test_catalog.py::test_a_targetless_read_is_refused_before_it_executes`
- `tests/catalog/test_catalog.py::test_a_named_read_still_binds`
- `tests/kernel/test_mechanism_parity.py::test_repeated_tool_call_stops_tools_before_it_blocks`
  （原 `test_repeated_tool_call_blocks_the_run` 的契约已被本改动取代，见其中注释）
