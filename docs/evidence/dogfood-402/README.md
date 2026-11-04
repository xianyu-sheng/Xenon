# Dogfood #4：模型问了问题，界面说「未交付」（#402, #403）

真实会话（Windows Terminal，`xenon` 0.9.6，cwd = `C:\Windows\System32`，
模型 `deepseek/deepseek-v4-flash`，模式 `direct`）。

用户的判断是「我让他处理一个任务，他直接显示未交付了」。
ledger 显示的事实完全不同。

## 原始 ledger（节选）

`~/.xenon/ledger/repl/repl-2609b56d9eee.jsonl`

```text
  1 task_started      {"constraints": ["runtime:kernel", "model:deepseek/deepseek-v4-flash", ...]}
  6 state_transition  {"from": "running", "reason": "propose action", "to": "thinking"}
  7 state_transition  {"from": "thinking", "reason": "action proposed", "to": "running"}
  8 user_question     {"question": "我这边没有任何关于 P0 的上下文：当前工作区是 C:\Windows\System32
                        （不是代码仓库），会话里也没有 P0 对应的 issue、分支或文件。请提供一条能定位任务的信息：…"}
  9 state_transition  {"from": "running", "reason": "user input required", "to": "blocked"}
```

模型没有失败，也没有空转：它 emit 了 `ask_user`，要求补上下文。**问题文本没有到达用户。**

## 界面实际显示

```text
💭 kernel · 1 steps · 0 tools · abstained
● Kernel · blocked
  任务未交付（phase=blocked）。
```

## 根因

`xenon/replay.py::blocking_reason` 与 `xenon/repl/runtime_adapter.py::_blocking_reason`
是**两份重复实现**，取原因的 kind 白名单都只有
`policy_decision / user_approval / control_decision / binding_failed`。

`user_question` 不在白名单里，且它的 payload 字段是 `question` 而不是 `reason`，
两处都返回空字符串 → 落到兜底的「任务未交付（phase=blocked）。」

`runtime_adapter.py` 里还有**两份 `_result_summary`**，前一份从未执行（死代码）。

## 修复

1. `replay.blocking_reason` 处理 `user_question`：返回 `等待你的回答：<question>`，
   空文本则继续向下找真实原因（不遮蔽更早的 veto）。
2. 删除 adapter 里的重复实现（40 行）与死的 `_result_summary`，只从 `xenon.replay` 取。
3. 等待中的回合不再说「未交付」：`_result_summary` 直接返回问题文本，
   面板标题为 `Kernel · 等待回答`（而不是 `Kernel · blocked`）。

## 修复后（同一份真实 ledger）

```text
blocking_reason -> 等待你的回答：我这边没有任何关于 P0 的上下文：当前工作区是
C:\Windows\System32（不是代码仓库）…请提供一条能定位任务的信息：(1) 目标 GitHub
仓库 owner/repo；(2) P0 具体指什么…

_result_summary -> 等待你的回答：我这边没有任何关于 P0 的上下文：…
```

## #403：进度噪音

同一会话里一次 19 步的回合把 `· 模型生成中 Ns · M 字符` 刷了 200+ 行，
把答案顶出屏幕。

修复：`kernel_progress_reporter` 不再逐秒打印；进度交给状态栏（transient），
控制台只在**单次调用超过 15s** 时提示一次「仍在生成」（`seconds` 变小视为新调用）。

```text
0.2s / 1.0s / 2.5s / 9.0s / 14.9s  -> 不打印
15.0s                              -> 1 行
16.0s / 17.0s / 25.0s              -> 仍然是那 1 行
（下一次调用）1.0s / 15.5s         -> 再 1 行
```

## 测试

- `tests/kernel/test_approval_seam.py::test_blocking_reason_surfaces_an_unanswered_user_question`
- `tests/kernel/test_approval_seam.py::test_a_waiting_turn_is_titled_waiting_not_blocked`
- `tests/kernel/test_approval_seam.py::test_an_empty_question_does_not_hide_a_real_reason`
- `tests/test_repl_kernel_runtime.py::test_a_blocked_turn_that_asked_a_question_says_so`
- `tests/kernel/test_cancel.py::test_progress_reporter_stays_quiet_until_a_call_looks_stuck`

`blocking_reason` 原有 4 条测试改为指向 `xenon.replay`（单一来源），不再测重复实现。
