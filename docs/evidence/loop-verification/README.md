# 循环验证（#369）：检测 + 教它打破，再杀

## 机制

1. 命中信号（identical_call / repeated_failure / capability_cycle /
   no_progress）→ 写 `LOOP_DETECTED`（kind/detail/tool 结构化事实）；
2. 同一信号未教满 `max_loop_corrections`（默认 1）次 → 诊断进入下一轮视图
   `Notes:` 区段，模型可纠偏（证据/权限/交付判定不变）；
3. 同一信号重复命中 → `POLICY_DECISION{VETO}` 终止，理由含
   "after 1 correction attempt"；
4. 模型换做法（无信号且产生新信息）→ 计数重置，继续；
5. `max_loop_corrections=0` → 等同旧行为（检测即终止）。

## 验收映射（tests/kernel/test_loop_verification.py，5 条全绿）

- 命中 → ledger 有 `loop_detected`（kind/detail/tool），非直接 blocked；
- 下一轮视图 Notes 有诊断；
- 换做法后继续（4 次同错被教 → 换墙成功，phase != blocked）；
- 同信号再命中 → 终止，reason 含 "after 1 correction attempt"；
- max=0 → 直接终止，无纠偏。

## 修掉的两个真实 bug

- 信号身份用 `window`（kind + 稳定指纹）而非 detail：detail 内嵌计数
  （"4 times"）会让每次命中都像新信号 → 教学永不结束；
- 计数重置只能在"无信号 + 有新信息"时发生：`no_progress_streak==0` 单独
  太宽（新参数也算），会导致同墙无限教学。
