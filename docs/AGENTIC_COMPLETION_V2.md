# Agentic Completion Benchmark v2

**目的**：C12 v1 的任务太易（flash/pro 均 5/5），无法区分完成率。v2 在相同确定性评分框架下增加隐藏测试、跨文件 API 变更、多 bug、git 历史与长链路实现。

## 预注册

| 项 | 值 |
|---|---|
| 任务数 | 5 |
| 模型 | `deepseek/deepseek-v4-flash`、`deepseek/deepseek-v4-pro` |
| 引擎 | ReAct，`native_fc=True`，`max_iterations` 24–36 |
| 运行方式 | 每个任务独立临时目录；真实工具执行；隐藏测试只在任务结束后写入 |
| 通过标准 | 文件存在 + `pytest` 通过 + 特定断言（测试文件未改、旧 API 消失、分支/提交/工作树） |
| 预注册 | 2026-10-18，首次完整运行前固定 |

## 任务

| 任务 | 难点 | 通过检查 |
|---|---|---|
| `hidden_edge_cases` | 模型看不到边界断言，只能读 docstring 推断 | 可见测试未改 + 隐藏测试（0 分母、负数、0 分子）通过 |
| `cross_file_api_change` | 改签名并更新 3 个文件/调用点 | 隐藏测试调用 `calculate_total/cart_total/invoice_total` 的 `tax_rate` |
| `multi_bug_diagnosis` | 可见测试只暴露一个 bug；隐藏测试暴露解析器边界 | 可见+隐藏测试全部通过 |
| `git_partial_revert` | 只恢复坏提交中的 TIMEOUT，保留 feature.py | TIMEOUT=30、feature.py 存在、分支 `fix/timeout`、提交、工作树干净 |
| `stats_chain` | 一次实现 3 个函数 + 测试 + 边界 | 隐藏测试（空列表、偶数中位数、众数平局）通过 |

## 运行

```bash
python -m evals.agentic_completion_v2 --models flash pro \
  --output evals/reports/agentic_completion_v2.json
```

## 结果（2026-10-18，修正浮点断言后）

| 模型 | 完成率 | LLM 调用 | 工具调用 | 失败工具 | 总耗时 |
|---|---:|---:|---:|---:|---:|
| flash | **5/5** | 58 | 69 | **4** | 121.7s |
| pro | **5/5** | 73 | 78 | 10 | 203.3s |

分任务：两个模型均通过 `hidden_edge_cases`、`cross_file_api_change`、`multi_bug_diagnosis`、`git_partial_revert`、`stats_chain`。

原始数据：`evals/reports/agentic_completion_v2.json`。

### 基准修正（运行后发现）

第一次完整运行里，`cross_file_api_change` 的隐藏测试用了精确浮点相等：

```python
assert calculate_total([100], 0.1) == 110
```

实际结果是 `110.00000000000001`，两个模型都被误判为失败。归档原始数据后，把隐藏测试改为
`pytest.approx(110)`，并重跑完整 v2。原始数据在
`evals/reports/agentic_completion_v2_original.json`（flash 4/5、pro 4/5，均只挂该任务）。

## 解读

1. **v2 仍是天花板**：两个模型都 5/5；隐藏测试、跨文件 API、多 bug、git 部分回滚、长链路实现都没有拉开完成率。
2. **pro 没有优势**：这个 suite 上 pro 多 26% LLM 调用、多 1.7 倍失败工具、慢 67%。v1 的“pro 更省工具”没有在更难任务上复现。
3. **flash 是更合理的默认**：对本地多步任务，flash 完成率相同且更快/更省；C5 不应把所有 `write_code` 切 pro。
4. **C4 摩擦贯穿两个模型**：`$()` / `python -c` 拦截在两个模型里都出现；C13 让任务继续，但仍有工具浪费。
5. **C11（todo/plan）没有证据支持现在加**：没有观察到“状态丢失”是失败主因；先保留为候选。

## 决策

- **保持 flash 默认**；pro 只在明确需要更强推理的长任务上按需使用，不做全局路由。
- **C12/C16 结论**：v1/v2 都偏容易，下一步需要真实长任务（SmartBench 类）或 v3 设计（多阶段失败恢复、跨仓库约束），而不是继续加同难度小题。

## 限制

- 单次运行，模型随机性存在。
- 隐藏测试规格在提示中给出，避免纯猜测；但仍比 v1 更接近真实修复任务。
- 未统计 token 成本。
