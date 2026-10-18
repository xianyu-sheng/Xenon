# Agentic Completion Benchmark

**目的**：度量 Xenon 是否真正完成多步编码任务，而不是只看最终回答是否像样。
这是 C12 的第一版基准，任务、提示和通过标准在首次运行前固定。

## 协议（预注册）

| 项 | 值 |
|---|---|
| 任务数 | 5 |
| 模型 | `deepseek/deepseek-v4-flash`、`deepseek/deepseek-v4-pro` |
| 引擎 | ReAct，`native_fc=True`，`max_iterations=30`（git 任务 24） |
| 运行方式 | 每个任务独立临时目录；真实工具执行，无人工审批 |
| 通过标准 | 文件存在 + `pytest` 通过 + 特定断言（测试文件未被改、旧符号消失、分支/提交存在） |
| 预注册 | 2026-10-18，运行前写入 `evals/agentic_completion.py` |

## 任务

| 任务 | 要求 | 通过检查 |
|---|---|---|
| `create_file_and_test` | 创建 `greet.py` + `test_greet.py`，运行 pytest | 两文件存在 + pytest 通过 |
| `fix_existing_bug` | 修复 `calc.py` 的加法 bug，不改测试 | pytest 通过 + 测试文件哈希不变 |
| `multi_file_package` | 创建 `mathx` 包（`__init__.py`/`ops.py`）+ 测试 | 三文件存在 + pytest 通过 |
| `refactor_rename` | 将 `compute_total` 全量重命名为 `total_price` | pytest 通过 + 旧符号不再出现 |
| `git_branch_commit` | 创建 `notes.txt`、分支 `feat/agentic`、提交 | 文件内容正确 + 分支/提交存在 + 工作树干净 |

## 结果（2026-10-18，首次运行）

| 模型 | 完成率 | LLM 调用 | 工具调用 | 失败工具 | 总耗时 |
|---|---:|---:|---:|---:|---:|
| flash | **5/5** | 58 | 60 | **11** | 120.5s |
| pro | **5/5** | **46** | 49 | **4** | 128.2s |

分任务：

| 任务 | flash | pro |
|---|---|---|
| `create_file_and_test` | PASS（10 LLM / 3 fail） | PASS（6 LLM / 0 fail） |
| `fix_existing_bug` | PASS（5 LLM / 1 fail） | PASS（7 LLM / 0 fail） |
| `multi_file_package` | PASS（12 LLM / 3 fail） | PASS（8 LLM / 1 fail） |
| `refactor_rename` | PASS（10 LLM / 1 fail） | PASS（14 LLM / 2 fail） |
| `git_branch_commit` | PASS（21 LLM / 3 fail） | PASS（11 LLM / 1 fail） |

原始数据：`evals/reports/agentic_completion.json`。

## 解读

1. **这一版任务太 easy**：两个模型都是 5/5，完成率没有区分度。C12 第一版只能证明流程可跑通，不能证明模型路由的收益。
2. **pro 的工具纪律更好**：在相同完成率下，pro 少 12 次 LLM 调用（-21%）、少 7 次失败工具调用（-64%），耗时相近。作为“减少绕路”的证据有效，但还不足以单独决定默认路由。
3. **flash 也能完成**：对短任务，flash 是可用且更便宜的选择；C5 不应一刀切把 `write_code` 全部切到 pro。
4. **C4 摩擦仍在**：两个模型都撞到 `$()` / `python -c` 拦截；C13 的恢复提示让它们继续完成，但浪费了工具调用。

## 决策

- **暂不改变默认模型路由**（C5）：需要 v2 难度更高的任务或真实长任务（如 SmartBench）来验证完成率差异。
- 保留 **pro 作为复杂/多文件任务的候选**；在 v2 中加入隐藏测试、跨文件 API 变更、长链路任务。
- C11（ReAct 显式 todo/plan）也等 v2 数据，避免凭感觉增加工具。

## 复现

```bash
python -m evals.agentic_completion --models flash pro \
  --output evals/reports/agentic_completion.json
```

需要 `~/.xenon/credentials.yaml` 中配置 `deepseek` key。每次运行使用新的临时目录，不修改仓库。

## 限制

- 单次运行、真实模型有随机性；任务难度偏低。
- 未统计 token 成本；只记录 LLM 调用次数和工具失败数。
- 通过标准是确定性的，但没有覆盖代码风格/可维护性。
