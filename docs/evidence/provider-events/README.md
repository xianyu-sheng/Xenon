# Provider 阶段事件（#320）

真实模型、真实网络。复现：

```bash
cd D:/Xenon
PYTHONIOENCODING=utf-8 python docs/evidence/provider-events/provider_events_probe.py deepseek/deepseek-v4-flash
PYTHONIOENCODING=utf-8 python docs/evidence/provider-events/kernel_turn_probe.py deepseek/deepseek-v4-flash
```

## 修复前：声明了一个从未被调用的钩子

```bash
$ grep -rn "on_provider_request_start" xenon tests evals scripts | grep -v callbacks.py
（无输出）
```

`EngineCallback.on_provider_request_start/end` 只在 `callbacks.py` 里出现：基类是 `pass`，`SilentCallback` 往 list 里记一条。唯一真正发事件的是 `engine/event_bus.py`，它在 #192 的引擎拆卸中已被删除。

- REPL 的 `ConsoleCallback` **从未实现**这两个方法（继承 no-op）；
- 所以即使有代码调用，界面也不会有任何变化。

也就是说这是一个**看起来活着、其实已经死了的 API**。

## 修复后：三层真实证据

### 1. 真实 kernel 轮次（`run_task` → strategy → `_call_model` → `chat_completion` → sink）

```text
phase=finalized accepted=True
provider events: 4
  start deepseek/deepseek-v4-flash attempt=1/3 streamed=False
  end   success=True attempt=1
  start deepseek/deepseek-v4-flash attempt=1/3 streamed=False
  end   success=True attempt=1
```

两次模型调用（读文件 + 收尾），每次一对 start/end。

### 2. 三种真实网络结果

```text
=== real kernel call ===
  start phase=deepseek/deepseek-v4-flash attempt=1/3 timeout=120s streamed=False
  end   success=True attempt=1 retrying=False reason=''

=== bad api key ===
  start phase=deepseek/deepseek-v4-flash attempt=1/3 timeout=120s streamed=False
  end   success=False attempt=1 retrying=False reason='401 http error'
  raised HTTPStatusError

=== unreachable host (2 attempts) ===
  start phase=deepseek/x attempt=1/2 timeout=120s streamed=False
  end   success=False attempt=1 retrying=True reason='502 server error'
  start phase=deepseek/x attempt=2/2 timeout=120s streamed=False
  end   success=False attempt=2 retrying=False reason='502 server error'
```

三个关键区别被如实表达：

- **401 不重试**（`retrying=False`，只出现一次）；
- **502 重试**（`retrying=True` → 第二次 start）；
- **尝试次数递增**（1/2 → 2/2，耗尽后 `retrying=False`）。

### 3. REPL 渲染

```text
· deepseek/deepseek-v4-flash 请求中（第 2/3 次，超时 120s）
· deepseek/x 失败（429 rate limited），将重试
· deepseek/x 失败（429 rate limited），已用尽 3 次
```

单次尝试不显示 `1/1`（那是噪音）；成功的 end 不打印任何东西。

## 设计决定

**不用事件总线。** 总线是全局可变状态，并行子任务会互相串台。内核已经把 per-call 钩子放在 `ModelConfig` 上（`usage_sink` / `should_cancel` / `progress_sink`），这次的 `request_sink` 是同一形状的第四个。

**事件不进账本。** 账本记事实（回执、裁决、债务、用量），provider 阶段是每次尝试的遥测。把它写进账本会让回放被噪音淹没，而且它不参与任何判定——`model_usage` 已经记录了花费。

**遥测永远不能让调用失败。** `emit()` 吞掉 sink 的异常；界面崩了不该赔上一个 run。有测试锁这条。

**流式如实报 1 次尝试。** `chat_completion_stream` 没有重试循环，所以它报 `provider_attempts=1` —— 写 "1/3" 会暗示一个不存在的重试。

## 删掉的死代码

`EngineCallback.on_provider_request_start/end` 与 `SilentCallback` 里的对应记录器已删除。证据：全仓库除 `callbacks.py` 外零引用，且 `ConsoleCallback` 从未实现。留着它等于让下一个读代码的人以为还有第二条通路。
