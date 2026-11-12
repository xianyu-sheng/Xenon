# 并行与分叉：不是 DAG，是事件流

结论先说：**不要做计划图（DAG）执行器**。旧 `plan_dag_executor.py`（781 行）就是那么做的，结果是"一张图 + 一个调度器 + 一套状态机"三套真相。

真正需要的能力只有两个：

1. **同时推进几件事**（并行）；
2. **让它们各自有账，最后合回来**（分叉与回流）。

这两件事在 event-sourced 的设计里有天然答案：**分支 = 子账本，回流 = 父账本里的一个指针事件**。子 runtime（`harness/child.py`）已经给了这个形状：

- 每个子任务有自己的 ledger（不污染父账）；
- 父账只记一条 `child_task{task_id, ledger, status}`（指针，不是内容）；
- 子任务 `finalize=False`（不提交），深度结构性为 1；
- 子任务在账本里记下 `parent_task_id`，调用树只从账本推导（`#356`）；
- 子任务没有终端，它对授权的请求由**父级代办**（`#356`）：显式请求 → 父级 provider → 人；父级没有 provider 就是拒绝；
- 子任务跑在**独立进程组**里（`#354`）。线程杀不掉，被放弃的子任务会在父级记下 `timeout` 之后继续写工作区——实测可见（`docs/evidence/child-process/`），所以隔离不是性能优化，而是交付语义的一部分。

## 并行调度该长什么样

不是"图上的节点并行执行"，而是**批量派发 + 确定性回流**：

```text
父 runtime:
  1. 选出一批互不依赖的子任务（判据：无写冲突、无同一 target）
  2. 并发 run_child_tasks(...)（已实现，输入顺序保持）
  3. 按提交顺序（不是完成顺序）逐条把 child_task 事件写入父账
  4. 单写者不变：父账只有 runtime 在写
```

关键约束：

- **完成顺序 ≠ 记账顺序**。按完成顺序写，账本顺序会随调度抖动，回放不可复现。
- **写冲突必须静态可判**（target 集合不相交），否则并行就是竞态。子任务目标是自然语言，**没有静态 target 集合可判**，所以并行子任务默认各跑在自己的 worktree 里，成果以 patch 显式回流（`#358`）。参见 `docs/evidence/child-isolation/`：共享 workspace 时，真实运行里两个子任务的修改互相覆盖，两边账本都以为成功。
- **合流不做"合并结果"**：子任务的产出是证据指针，父任务需要重新验证（`no_stray_changes` 审计会看工作区实际状态）。patch 由调用方显式 `apply_patch`，冲突就报冲突，不自动合并。

## 和 steering、压缩的关系

- 并行的子任务各自消费自己的 steering，父任务不转发（否则一条约束会同时打到多个分支，语义不清）；
- 压缩按子账本独立进行，父账只看到指针，天然不会爆。

## 现在该做什么

并行派发（`run_child_tasks`）已经可用，条件 1 已满足：真实用例实测 `wall=3.7s` 对串行和 `7.1s`（1.95x 重叠），各自独立进程与账本，无残留进程（`docs/evidence/child-process/`）。

条件 2 已补齐（`#361`）：每次派发生成一个 `batch_id`，写进子任务账本的 `TASK_STARTED`，`xenon replay --children` 按批次分组。—— 注意它是**标签**，不是调度语义：不表达顺序、依赖、并发度，也没有批次级重试。在这一条补齐之前，并行仍然只从 REPL 的 `/agent --parallel` 走，**不给内核加 `spawn` 能力**——那会让模型自己扇出，破坏单写者与证据绑定。

补齐后它仍然是"事件流的分叉与回流"，不是图。

## 已实现口径（2026-11-11）：分支点 + 封存，仍然不是 DAG

实现比上面的指针事件更严格，但语义相同——**分支 = 子账本，回流 = 父链上的一条指认**：

- **分支点**：`child_started.branch = {task_id, ledger, base_seq, base_hash, depth}`，
  即父链在"承诺观察这个子任务"时的位置；
- **封存**：`child_finished` / `child_interrupted.branch =
  {task_id, ledger, head_seq, head_hash, event_count, sealed}`，由父进程**亲手验证子链后**写入；
  读不到或验证失败就 `sealed:false` 且不写 head（不编造观察）；
- **验证**：`xenon/ledger/tree.py::verify_tree` = 父链线性验证 + 每条分支独立验证 + 比对封存头；
  `xenon report` 的 `branches` 段会列出 `sealed / pending / missing / tampered / unsealed`
  （`unsealed` = 封存时父进程读不到或验不过子链，`sealed:false`，不写 head）；
- **单写者**：同一账本路径在进程内只有一个 `Ledger` 实例（`store.open_shared`，弱引用注册表 + 任务 id 校验）；
  `append` 前检测文件是否被别的进程推进，推进了就重新加载（`_refresh_tail_if_moved`），绝不静默分叉。

**为什么坚持没有 `next[]` 后继数组**：每条事件的内容哈希包含自身 `prev_hash`，写入后不可改。把后继指针
放进前驱事件意味着回写历史，直接破坏 append-only 与整条链；放进独立可变索引则等于引入第二份必须与链同步的
真相，修复索引本身不产生证据。"同时推进几件事"是调度问题，不是链结构问题——分支用引用表达已经足够。
