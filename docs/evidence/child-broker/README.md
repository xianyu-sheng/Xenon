# 父级可见子级（#356 / #204 切片）

真实模型、真实仓库跑出来的证据。复现：

```bash
cd D:/Xenon
PYTHONIOENCODING=utf-8 python docs/evidence/child-broker/tree_probe.py deepseek/deepseek-v4-flash
PYTHONIOENCODING=utf-8 python docs/evidence/child-broker/broker_probe.py deepseek/deepseek-v4-flash
```

## 1. 修复前：父级链接**在任何地方都不存在**

`parent_task_id="demo-parent-ccc052"` 跑一个真实子任务：

```text
child ledger task_started fields: ['constraints', 'mode', 'objective', 'user_objective', 'workspace']
child ledger mentions parent in: NOTHING
parent ledger exists: False
```

- 父任务账本不存在（子任务从 REPL 起，REPL 没有任务账本）；
- 会话事件日志是**纯内存 list**，REPL 退出即消失；
- 子任务账本里没有 `parent_task_id`。

唯一线索是 `child_id` 的字符串前缀，那是命名约定，不是数据。所以 #204 的验收「父级链接事件可重建调用树」当时**没有任何数据能支撑**。

## 2. 修复前：子任务改完文件后无法验证

同一个子任务的时间线结尾：

```text
38  policy_decision   veto: verify requires a grant
39  state_transition   running -> blocked
```

子进程的 stdin 是 `DEVNULL`（#354 故意的），没有审批通道，而 `AUTO_EDIT` 下 `verify` 需要授权 → 必然被拒。REPL 的 `/agent` 从不传 `permission`，永远落在 `AUTO_EDIT`，**任何想跑 `verify` 的子任务都会 blocked**，而它已经把 `note.py` 改了。

## 3. 修复后：链接落在账本里，调用树可重建

`TaskSpec.parent_task_id` → `TASK_STARTED.parent_task_id`。调用树只从账本推导，不引入任何索引或会话状态：

```text
=== allow ===
children of broker-allow: 1
- broker-allow-sub1-ff145d  phase=finalized tools=1 debt=0
    ledger: \tmp\brl-e7a943\broker-allow-sub1-ff145d.jsonl
```

测试锁死了一条关键性质：**名字不能决定树**。一个叫 `p-sub1-zzzz` 但账本里写着 `parent_task_id="q"` 的子任务，只会出现在 `q` 下面。

## 4. 修复后：审批由父级代办，子级无法自我提权

三条真实运行（同一个需要授权的 `shell` 请求）：

| 父级 provider | 请求数 | 子任务结果 |
|---|---|---|
| 批准 | 1 | `finalized accepted=True` |
| 拒绝（"operator refused: do not touch tests"） | 1 | `blocked accepted=False` |
| 没有 provider | — | `blocked accepted=False`（fail closed） |

父级看到的请求内容：

```json
{"id": "14536-1", "capability": "shell", "tool": "shell",
 "params": {"command": "git log --oneline -n 3"}, "risk": "approval"}
```

传输是两个文件（`req-<n>.json` / `resp-<n>.json`），不是管道：子进程的 stdout 是 `DEVNULL`（子任务的输出不是它的结果），再加一条带帧协议的通道只会漂移。

**权限上限**：`parent_permission` 显式给出时，子级权限必须是它的子集，否则抛错不启动进程——否则只读会话里 `run_child_task(permission=BYPASS)` 就凭空提权了。

## 5. 真实运行顺带抓到的内核 bug

broker 探针中途，子进程直接挂了：

```text
ValueError: illegal task transition: running -> running
  runtime.py:827 _release_active_step
    self._transition(TaskPhase.RUNNING, "step released")
```

`_release_active_step` 在「释放的步骤没有任何违规需要修复」时不进 `REPAIRING`，然后无条件 `_transition(RUNNING)`——而它已经在 `RUNNING` 了。状态机正确地拒绝了，代价是整个任务崩掉。

修法：`_transition` 把「已在目标态」当作 no-op（不记事件、不改状态）。重述当前阶段是冗余，不是非法。

## 语义决定

**子任务的 tool receipt 永远不算父级的证据。**

receipt 是「子级声称自己做了什么」。把它导入父级审计，等于把未验证的子级动作洗成父级 `VERIFIED`。父级关于工作区唯一合法的证明是**它自己的 `GitWorktreeProbe`**（客观观察，谁写的都算数）。

所以本切片做的是「看得见、追得到、管得住」，不是「导入子级 facts」。这也是 `#204` 里我明确不做的部分。
