# Xenon 评测闭环

评测报告不再把工具覆盖、最终结果正确性和缓存收益合并成一个分数。

## 两套独立评测

- ReAct / 工具评测：`evals/tasks.yaml`。模型在隔离 fixture 中完成自然语言任务；工具成功必须由 `ToolExecuteResult.success` 和终态确认，不能只按工具名计数。
- Xenon REPL 命令评测：`evals/repl_tasks.yaml`。通过真实 REPL command dispatcher 执行 `/help`、`/mode` 等斜杠命令，单独报告结果断言。

运行 REPL 命令套件（不调用模型，也不写入用户缓存历史）：

```bash
python evals/runner.py --suite repl --workdir /tmp/xenon-repl-eval \
  --output evals/reports/repl_report.md
```

真实模型 ReAct 评测必须使用一次性工作目录和凭证副本：

```bash
python evals/runner.py --mode real \
  --model deepseek/deepseek-v4-flash \
  --workdir /tmp/xenon-real-eval --isolate-tasks \
  --output evals/reports/real_report.md
```

要覆盖全部引擎，使用 `--engines all`。这会为每个引擎生成独立报告和一个矩阵索引；不会计算跨引擎总分：

```bash
python evals/runner.py --mode real --engines all \
  --model deepseek/deepseek-v4-flash \
  --workdir /tmp/xenon-engine-matrix --isolate-tasks \
  --output /tmp/xenon-engine-matrix/report.md
```

当前矩阵覆盖：`direct`、`react`、`plan-execute`、`reflection`、`plan-react`、`plan-reflection`、`react-reflection` 和 `novel`。`direct`/`reflection`/`novel` 的工具期望对文件任务标记为不适用，但仍单独报告最终回答和结果断言，不会伪造工具成功率。

每个真实引擎批次还会在工作目录写入 `checkpoints/<engine>.jsonl`：每完成一个任务立即落盘，包含 task/engine 创建、引擎运行、断言和异常事件。`--request-timeout` 控制单次供应商 HTTP 请求超时，避免一个无响应请求阻塞整批评测。

## 必须分开报告的指标

- Verified Success Rate：工具真实成功且最终结果断言通过的任务比例。
- Tool Execution Success Rate：所有工具执行事件中 `ToolExecuteResult.success` 为真的比例。
- Result Assertion Pass Rate：文件、配置、测试、MCP/记忆状态和最终回答断言的通过率。
- Cache Rails Hit Rate、reusable/hit tokens、estimated tokens saved、estimated cost saved：只使用供应商返回的缓存字段；没有证据时显示 `N/A`，不显示为零。

`commands_pass` 仅允许评测 fixture 自己声明的验证命令，命令在任务隔离目录中运行，不能由模型动态改写。

## 官方 SWE-bench 适配边界

`evals/swebench_xenon.py` 不包含评分逻辑，也不读取官方参考 patch。它把原始
`problem_statement` 和 base-commit 工作树交给 Xenon，收集 `git diff`，并写出
官方 prediction JSONL。之后必须使用官方命令判定：

```bash
python -m swebench.harness.run_evaluation \
  --dataset_name SWE-bench/SWE-bench_Lite --split test \
  --predictions_path predictions.react.jsonl \
  --instance_ids astropy__astropy-12907 \
  --run_id xenon-react-official
```

多引擎预测必须写入不同文件。同一个 prediction 文件里重复 `instance_id` 会被
官方 harness 按键覆盖，所以适配器在 `--engines all` 时生成每引擎独立 JSONL。
空 patch 会由官方 harness 标记为 `empty_patch` 且不执行实例；不能把它报告成
`resolved = false`，也不能把 `0/0` 伪装成成功率。供应商 429、HTTP 超时、工具
参数拦截、错误工作目录和本地依赖缺失都保留在 Xenon trace 中，与官方 resolved
结果分栏报告。
