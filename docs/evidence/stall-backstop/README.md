# 停滞兜底 + 步数上限（#368）：1024 是优雅的大数，停滞才是真闸

## 验收逐条

| 验收 | 证据 |
|---|---|
| 默认 `max_steps=1024`；`None`/`0` → 无限 | `DEFAULT_MAX_STEPS = 1024`（runner.py）；`Budget.max_steps=None` 时 `_check_limits` 跳过步数检查 |
| 停滞 180s 无新信息 → `BLOCKED` 理由 `stalled: ...` | `_stalled()` + `_stall_reason()`（kernel/runtime.py），`signal="stalled"` 入账；`tests/kernel/test_stall_timeout.py` 5 条 |
| 有新信息不触发 | `_last_progress_at` 在「无信号 + 有新信息」时重置；测试构造每步新路径 5 分钟不误停 |
| token 优先于步数 | `_check_limits` 顺序：cancelled → steps → **tokens** → deadline → stalled |
| 真实运行 finalized + 实际步数远小于 1024 | 本目录 probe.py：完整 kernel 链路（read → edit → bash → finish → evidence verified → finalize），`steps=4`，`accepted=True` |

## 与 loop detector 的分工（防重叠）

- `no_progress` 只是**诊断**（附加到停止原因），不停机——真实运行证明它会在成功前误杀；
- `_stalled` 是**时间闸**（wall-clock 无新信息 180s）——同一个「新信息」定义
  （`_last_progress_at` 由 loop detector 的 progress 判定维护），两个触发器、一个原则。
