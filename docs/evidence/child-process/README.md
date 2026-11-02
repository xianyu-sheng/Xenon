# 子任务进程隔离（#354 / #204 切片）

两个脚本都是**真实模型、真实仓库**跑出来的，不是单元测试。复现：

```bash
cd D:/Xenon
PYTHONIOENCODING=utf-8 python docs/evidence/child-process/abandoned_child_probe.py deepseek/deepseek-v4-flash
PYTHONIOENCODING=utf-8 python docs/evidence/child-process/parallel_probe.py deepseek/deepseek-v4-flash
```

## 1. 被放弃的子任务会继续写工作区（修复前）

`run_child_task(..., timeout=6.0)`，父级收到超时之后继续观察工作区指纹：

```text
returned after 6.0s: phase=failed err=timeout after 6s
at return:              calc.py 未变
+5s                     calc.py 被改、finished.txt 被创建
+20s                    还写了 __pycache__/calc.cpython-314.pyc
+60s                    仍在变化
final:  git status → ' M calc.py'  '?? finished.txt'  '?? __pycache__/'
```

父级记下 `failed: timeout` 的那一刻，子级并没有停。它随后完成了写文件。

**为什么这是正确性问题而不是性能问题**：父级随后做 `no_stray_changes` 审计时，会看到自己从没写过的脏路径；或者反过来，把这些半成品当成自己的交付内容提交。子级账本在父级返回之后仍在增长，replay 出的时间线和父级记录矛盾。

根因：子任务跑在同进程线程里，**线程杀不掉**，只能被丢下（`#327` 的取消也是同一约束，那里我们用轮询在主线程侧放弃）。

## 2. 修复后：进程组被杀，工作区冻结

```text
returned after 6.1s: phase=killed err=timeout after 6s; process group killed
at return:              calc.py 已改、finished.txt 已建
changed after return:   none          ← 修前是持续变到 +60s
final:                 与 at return 完全一致
```

（这次子级在 6s 内已经完成了编辑；关键是**返回之后不再变化**。若 6s 内没做完，被杀掉的就是半途状态，父级拿到 `killed` 而不是一个假装成功的 `failed`。）

## 3. 并行子任务是真的并行

两个只读任务、`max_workers=2`、各自独立进程：

```text
wall=3.7s  serial-sum=7.1s  overlap=1.95x
  par-sub1-e54d50 pid=1124  phase=finalized 3.6s tools=1 ledger=par-sub1-e54d50.jsonl
  par-sub2-8201f5 pid=38252 phase=finalized 3.5s tools=1 ledger=par-sub2-8201f5.jsonl
leaked pids: none
git status: (clean)
```

两个子任务各有自己的 pid 与账本，`tasklist` 确认没有残留进程，工作区没有任何改动（任务本身是只读的）。

## 实现要点

| 关注点 | 做法 |
|---|---|
| 隔离单位 | 独立**进程组**（POSIX `start_new_session`；Windows `CREATE_NEW_PROCESS_GROUP`） |
| 通信 | 只有两个文件：`spec.json` 进，`outcome.json` 出。父级不解析 stdout，崩溃不会被当成结果 |
| 杀 | `os.killpg(SIGKILL)` / `taskkill /F /T`，连子级自己起的命令一起杀 |
| 原子性 | outcome 先写 `.tmp` 再 `os.replace`，父级读不到半截 JSON |
| 模型配置 | 只序列化数据字段；`usage_sink`/`should_cancel`/`progress_sink` 是父进程回调，不跨界，子级开销记在自己的账本里 |
| 测试接缝 | 注入 `client`/`gateway_context` 时留在同进程（假客户端无法跨界）；此时超时**不谎报** `killed`，错误文案写明「thread abandoned, not killed」 |
| 父级可见性 | `child_evidence` 增加 `pid` 与 `killed`，父级能证明子级是否真的结束了 |

## 明确没做（避免复活已废弃机制）

- **不做 DAG 波次调度器**（`SchedulerPolicy` per-wave）。`docs/design/mechanism-fit.md` 已判定 DAG 并行延后/不采用，`parallel-branching.md` 的结论是「分支 = 子账本，回流 = 父级指针事件」。
- **不给模型 `spawn` 能力**。把 spawn 加进那 7 个能力里，等于让模型自己扇出，破坏单写者与证据绑定——和已废弃的「LLM 自动切换范式」同类。
- **不做父级 facts 投影**（把子级 receipts 绑进父级 audit）。那需要先定「子级证据算不算父级的证明」，留给下一个切片。

## 已知残留风险

`run_child_tasks` 的多个子任务共享同一个 workspace。两个写同一个文件仍可能互相覆盖，这是委派语义本身的问题（父级只能靠 `GitWorktreeProbe` 看到脏路径）。真正的隔离需要每个子级一棵工作树/分支，属于 #204 后续切片。
