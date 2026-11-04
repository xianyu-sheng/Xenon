# Dogfood #7：复杂任务（11 次 receipt）与一个「声称 vs 实际」不一致

按用户要求，这轮**不用简单任务**：给一个没有提交的仓库三个相互关联的改造要求。

## 任务

```text
calc.py 三处改造：
1. div(a,0) 抛自定义 DivByZero（同步更新测试断言，不删测试）
2. mean([]) 抛 EmptySequence
3. add("1","2") 先转 float 再相加，非数字 str 抛 ValueError
要求：改完自己跑 pytest 并把真实输出作为证据；失败就修到全绿；最后说明改了哪几处。
```

## 第一次运行：交付是「verified」，但账本里没有任何可核验路径

```text
accepted: true   phase: finalized   evidence: verified
finalize_started: {commit: null, verified_paths: [], unverified_paths: []}
tool_receipt: apply_patch ok  effects=[fs_write paths=[calc.py, test_calc.py] changed=false]
```

文件确实被写了、测试确实全绿（5 passed），模型的叙述也没有撒谎。
但**交付那一刻，finalizer 认定的「已验证路径」是空集**——它把一次真实的写入当作"什么都没发生"。

### 根因

`FinalizeGuard.check` 只收 `FsWrite` 里 `changed=True` 的写入：

```python
if isinstance(effect, FsWrite) and effect.changed:
```

工具在**新建文件**时报 `changed=false`（没有旧内容可 diff），于是两个路径被完全丢弃，
`verified_paths` 为空，交付仍然 `accepted=True / verified`。

这正是 Xenon 存在的意义所在的那类失败：**声称与实际对不上，而系统说"验证通过"**。

## 修复（#418）

1. 记录 receipt **声明过**的所有写入路径（`declared`），不再依赖 `changed`；
2. 若「声明过写入」但「一条都没验出来」——**不交付**，理由写明：

```text
2 file(s) were reported as written but none could be verified in the workspace
```

这是校验层的行为，不阻塞执行：模型已经做完了，系统只是拒绝为它背书。

## 第二次运行（同一任务、同一仓库形态）

```text
finalize_started: {commit: null, verified_paths: [calc.py, test_calc.py], unverified_paths: []}
finalized: {commit: null, evidence_status: verified}
实测: 7 passed        （agent 说 7 passed，账本与实测一致）
```

## 顺带验证的两件事

- **失败即教学**（#417）：这轮里出现过一次工具失败，下一份视图拿到了可执行的一行建议，
  模型换了参数而不是重试同一个调用（`loop_detected` 只出现 1 次）。
- **A 类拒绝计数**（#417）：`result.metrics["refusals"]` 与 `status_snapshot()["refusal_total"]`
  都可读，便于判断"是模型笨还是目录约定有问题"。

## 仍然已知的局限

- 仓库**没有任何提交**时，finalizer 不会创建交付 commit（`commit: null`），
  只给出 `verified_paths`。要不要强制"必须有 commit 才算交付"是另一个产品决定，
  本轮没改。
