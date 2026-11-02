# 循环检测（#366）：为什么"步数预算"不该是第一个被发现的东西

## 你的判断是对的，我的实现窄了

引擎层原本有一个 `LoopDetector`，4 个信号：

1. 输出哈希重复
2. 工具调用序列模式重复
3. 错误消息重复
4. 思考内容哈希重复

引擎删除时（#322），我只迁了**最窄的一个**：完全相同 `(tool, args)` 签名。当时的理由是"模糊文本匹配会杀死正在推进的 run"。但原实现的 2、3 都是**精确哈希 / 精确模式比较**，不是模糊匹配——我的理由用错了对象，顺手把确定性的信号一起扔了。

## 真实运行证明了代价

干净安装后跑「给 calc.py 加 sub()，然后跑 pytest」：

```text
工具调用 18 次，失败 6 次
相同签名最多重复: 3 次 (limit=3)
→ guard 触发: 否 —— 因为每次命令都不同
```

模型依次发了 `dir /b`、`dir /s /b`、`python -c '...'`、`Get-ChildItem ...` —— **每个都不同，每个都失败**。完全相同签名永远等不到。最后是步数预算把它截断，报 `step budget exhausted`：真话，但对排查毫无用处。

## 修复 1：根因（shell 到底是什么）

`description="Run a shell command and observe its effects."` 没有告诉模型任何东西，于是它在 Windows 上发 cmd 语法。现在描述是平台感知的：

```text
Run a shell command and observe its effects. The shell is Windows PowerShell
(`powershell -Command`), NOT cmd.exe: `dir /b` and `dir /s` are cmd syntax and
will fail — use Get-ChildItem, e.g. `Get-ChildItem -Recurse -File`. Inline
interpreters (`python -c`, `perl -e`, `node -e`) and `<shell> -c` wrappers are
refused by policy: write a script file and run it instead, or use the
structured read/search/edit tools.
```

**同一任务、默认 20 步预算、修复后：**

```text
finalized accepted=True sha=bf8b8ba1 debt=0
工具调用 7 次（原 18 次）
失败回执 1 次（原 6 次）
模型实际发的命令: Get-ChildItem -Recurse -File | Select-Object -ExpandProperty FullName
                  python -m pytest tests/test_calc.py -q  (×2)
```

模型用的正是新描述推荐的 `Get-ChildItem`，19 步的白烧消失了。**步数预算没改**（仍是 20）。

## 修复 2：循环检测补齐，但只对"确定是循环"的停

| 信号 | 判定 | 停机？ |
|---|---|---|
| `identical_call` | 同一 `(tool, args)` 超过 N 次 | **是**，且在执行前拒绝（没有信息可学） |
| `repeated_failure` | 同一错误文本超过 N 次（不同命令、同一堵墙） | **是** |
| `capability_cycle` | A→B→A→B 连续 3 轮 | **是** |
| `no_progress` | 连续 N 次回执没有新信息 | **否**，只做诊断 |

### 为什么 `no_progress` 不停机

用它跑真实那次失败的账本：

```text
第 58 个事件 → 信号: no_progress
  诊断: 5 consecutive calls produced no new information (last progress: search:ok)
```

但那次运行在 **seq74 就成功写出了 patch**。如果在这里停，就把一次即将成功的运行杀掉了。

所以 `no_progress` 变成**诊断标签**：预算停机时把它附在原因上——

```text
step budget exhausted [5 consecutive calls produced no new information
(last progress: search:ok)]
```

预算仍然决定停不停，检测器只负责解释。这样既没有误杀，又不再出现"真话但无用"的停止原因。

### 为什么 `capability_cycle` 排除周期 1

"连续读三个不同文件"是一个能力用了三次，不是循环。周期 1 的模式只有和"没有新信息"结合才有意义，而那已经是 `no_progress` 的职责。原实现没有这个区分，会误杀正常的探索。

## 实现要点

- `xenon/kernel/loop_signals.py`：`StuckDetector`，取代 `xenon/kernel/guard.py::RepeatedCallGuard`（已删除，唯一权威）；
- **调用级事实** `note_call(tool, params)`：完全相同签名，执行前判定；
- **回执级事实** `observe(receipt)`：同一错误 / 周期 / 无进展，执行后判定；
- 两套身份空间**不混用**：签名只来自模型请求，回执参数不参与计数（否则同一次调用会有两个身份、两个计数器）；
- `_advance_step()` 时 `reset()`：新步骤读不同东西是合法的，不能背上一阶段的账。

## 回归

```text
tests/kernel/test_loop_signals.py   18 条，含：
  - 健康 trace 全程不触发（读→读→跑测试失败→成功重试→再读→再跑）
  - 重试后成功不触发
  - 三个不同文件连续读不触发
  - 同一错误 ×4 通过 runtime 停机并写 ledger
  - 预算停机时原因里带诊断
```
