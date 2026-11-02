# 真实端到端冒烟（9 场景）

## 为什么要有这个

单测和 CI 只能证明"零件彼此一致"。真实使用仍会被意想不到的东西堵住——实际发生过两次：

1. 授权面板**按键被吞**（rich `input()` 与 prompt_toolkit 抢 stdin），单测全绿；
2. 库调用（SDK）**根本没有授权通道**，连跑测试都做不到，也没人发现。

所以这个冒烟跑的是**用户入口**（`python -m xenon exec` 与 `xenon.run_task_result`），真实模型、真实 git 仓库，并检查用户真正关心的东西：文件变了没、测试过了没、有没有提交、`xenon metrics` 怎么记账。

## 场景与口径

| 场景 | 入口 | 断言 |
|---|---|---|
| `sdk-default-blocks-shell` | SDK，默认权限 | 必须 blocked **且给出原因**（库调用没人能点"批准"） |
| `sdk-bypass-delivers` | SDK，`permission="bypass"` | accepted + 文件已改 |
| `edit-commit` | CLI `exec` | finalized / 文件含 `def sub` / pytest 过 / 产生提交 |
| `plan-execute` | CLI `--mode plan-execute` | finalized / 文件含 `def mul` / pytest 过 |
| `read-only-no-write` | CLI `--permission read-only` | 只读轮次不写任何文件 |
| `no-finalize` | CLI `--no-finalize` | 改动落盘但**不产生提交** |
| `accept-debt` | CLI `--accept-debt` | 文件已改（缺口被接受时可交付） |
| `network-needs-approval` | CLI，默认权限 | 无审批人时必须 fail-closed 且给原因，而不是挂住 |
| `plan-file-executes` | CLI `--plan <file>` | 计划文件必须真的执行（此前无 actor，每步都违规） |
| `stray-change-reported` | CLI，提交后再弄脏 `notes.txt` | 必须拒绝，**且理由点名该路径** |
| `impossible-task` | CLI，不存在的路径 | 有界失败（`--max-steps 3`），不崩 |

## 结果（本次运行）

```text
11/11 scenarios passed
model: deepseek/deepseek-v4-flash
```

原始报告：`report.json`（含每个场景的 checks 与耗时）。产物目录里保留了 `stdout.txt` / `stderr.txt` / `ledger.jsonl` / `metrics.txt`，失败可离线复盘。

## 复现

```bash
python scripts/e2e_smoke.py --model deepseek/deepseek-v4-flash
python scripts/e2e_smoke.py --only edit-commit
```

不进 CI：需要真实 provider，会花 token。

## 这轮冒烟发现的四个真问题（已修）

1. **SDK 无法交付任何需要跑命令的任务**：`_sdk_permission()` 固定 `AUTO_EDIT`，`verify`/`shell` 一律 `requires a grant` → blocked，而调用方没有任何授权入口。已加 `permission=` / `allow_approval=` 参数。
2. **blocked 结果不给原因**：`TaskOutcome` 当初只有 `accepted/phase/evidence`，`blocked` 且 `debt` 为空时调用方什么也看不到。已加 `reason`（从账本读最后一条阻断理由），并把 `blocking_reason()` 提到 `xenon/replay.py` 供 REPL 与 SDK 共用。

一次"假失败"也被纠正：某次 `plan-execute` 报 pytest 失败，实际是模型自建了 `tests/test_calc.py` 与种子里的根目录 `test_calc.py` 同名冲突——**是我的种子仓库不合理**，不是产品缺陷；顺带说明 Xenon 的答复是诚实的（它明确写了只跑了 `tests/test_calc.py`，claim 也只覆盖该文件）。

3. **`no_stray_changes` 永远 abstain**：`worktree contains unverified paths` 明明可判，却报「语义声明需要覆盖绑定器」，白耗 3 轮修复预算。已改为宿主观测 → 账本事实 → 确定性绑定。
4. **整套测试通过绑不上具体目标**：跑 `python -m pytest -q` 时 `TestRun.target` 被记成命令串本身，于是「某文件测试通过」永远无法证明。已改为「无路径 = 整套」，整套通过覆盖任意具体 target。

5. **`xenon exec --plan <file>` 根本执行不了**：`run_plan` 构造的策略没有 step actor，且「跟随计划」模式只能跑带 `capability` 的步骤；自由步骤只会产出 `Think("step 1: edit")`，白耗修复预算后 blocked。已按计划形态选择 profile（全 capability → 确定性跟随；含自由步骤 → step actor），并在缺 capability 时明确 `ReleaseStep` 说明原因，而不是空转。
