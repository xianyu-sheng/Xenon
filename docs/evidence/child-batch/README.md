# 批次标签：一次派发 vs 多次派发（#361 / #204 收尾）

真实模型、真实仓库。复现：

```bash
cd D:/Xenon
PYTHONIOENCODING=utf-8 python docs/evidence/child-batch/batch_probe.py deepseek/deepseek-v4-flash
```

## 为什么需要它

没有批次标识时，账本里无法区分这两种情况：

- 一次派发三个子任务；
- 先后三次各派发一个子任务。

`batch_id` 就是那个标签。它不表达顺序、依赖、并发度——**不是调度器**，也不是 DAG 节点的替代品。

## 真实运行

一次派发两个、再一次派发一个：

```text
--- dispatch 1 (two children) ---
  batch-parent-sub1-f87245  batch=batch-parent-batch-b63d9b  phase=finalized
  batch-parent-sub2-62c299  batch=batch-parent-batch-b63d9b  phase=finalized
--- dispatch 2 (one child) ---
  batch-parent-sub1-502a7d  batch=batch-parent-batch-ce7c50  phase=blocked
```

每个子任务**自己的账本**里都记着批次：

```text
batch-parent-sub1-502a7d: batch_id='batch-parent-batch-ce7c50'
batch-parent-sub1-f87245: batch_id='batch-parent-batch-b63d9b'
batch-parent-sub2-62c299: batch_id='batch-parent-batch-b63d9b'
```

`xenon replay --children` 因此能按批次回答「这一批几个、几个交付」：

```text
children of batch-parent: 3

[batch batch-parent-batch-ce7c50] 1 child(ren), 0 delivered
- batch-parent-sub1-502a7d  phase=blocked tools=0 debt=0
    Report how many .py files are here.

[batch batch-parent-batch-b63d9b] 2 child(ren), 2 delivered
- batch-parent-sub1-f87245  phase=finalized tools=1 debt=0
    Report the constant in a.py.
- batch-parent-sub2-62c299  phase=finalized tools=1 debt=0
    Report the constant in b.py.
```

## 实现

| 关注点 | 做法 |
|---|---|
| 记录位置 | `TaskSpec.batch_id` → `TASK_STARTED.batch_id`（账本，不是会话状态） |
| 生成 | `run_child_tasks` 每次派发一个；`run_child_task` 未给时也生成（长度为 1 的批次） |
| 读取 | `children_of()` 返回 `batch_id`；`render_children()` 按批次分组 |
| 旧账本 | 没有 `batch_id` 的显示为 `[batch (no batch)]`，**不被悄悄并进任何一批** |
| 写进会话事件 | `child_evidence` 带 `batch_id`，REPL 侧可按批次聚合 |

## 明确不做

- **不加调度语义**：批次不表达顺序、依赖、并发度；
- **不做批次级重试/回滚**：那会重新引入一个编排层，正是 #192 拆掉的东西；
- **不改变 `parent_task_id`**：父级是「谁派的」，批次是「哪一次派的」。
