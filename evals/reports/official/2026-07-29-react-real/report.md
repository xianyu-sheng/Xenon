# Xenon Eval Report

> Scoring: real 模式跑 ReAct 多轮闭环，按**实际执行**的工具评分
>（`expected_tools ⊆ executed` 且 final_answer 非空）；配置了 `assertions` 的任务还会执行结果断言。`success_criteria` 仍为人工复核提示。

- Mode: `real`
- Evaluation suite: `ReAct/tool`
- Engine: `react`
- Model: `deepseek/deepseek-v4-pro`
- Run date: `2026-07-29 09:52:51 UTC`
- Tasks: 20
- Success Rate: 65.0%
- Average Tokens: 116.5
- Tool Calls: 112
- Tool Failures: 8
- Verified Tasks: 20/20
- Verified Success Rate: 65%
- Verified Success Rate (all tasks): 65%
- Tool Execution Success Rate: 95.09%
- Result Assertion Pass Rate: 90%

| Task | Category | Success | Verified | Tokens | Tool Calls | Tool Failures | Notes |
| --- | --- | --- | --- | ---: | ---: | ---: | --- |
| `edit-python-function` | file_edit | no | failed | 96 | 17 | 1 | 任务已完成。  **src/main.py** — `greet` 函数已修改为接受可选的 `name` 参数： ```python def greet(name=None):     if name:         return f"Hello, {name}"     re |
| `add-unit-test` | file_edit | no | failed | 35 | 12 | 1 | []` (empty list) and `None` inputs. The source function `normalize_items` in `src/utils.py` correctly returns `[]` in both cases. No further |
| `refactor-duplicate-code` | file_edit | no | yes | 34 | 8 | 2 | Refactoring already complete. `src/parser.py` has `_parse_parts` handling `strip()` + `split(':', 1)`, with `parse_left` and `parse_right` b |
| `update-readme-command` | file_edit | yes | yes | 24 | 3 | 0 | Updated README.md: replaced `--old` with `--verbose` in the usage instruction. |
| `code-search-entrypoint` | code_search | no | yes | 50 | 3 | 1 | **CLI Entrypoint:** `main()`  **How `start_chat` begins chat mode:** It doesn't implement real chat. It's a stub that just returns the strin |
| `code-search-tool-node` | code_search | yes | yes | 104 | 3 | 0 | Shell command parameters are validated before execution in `tooling.py` at **line 2**, within the `validate_tool_params` method of the `Tool |
| `code-search-model-router` | code_search | yes | yes | 144 | 3 | 0 | ## Model Parsing & Endpoint Routing in `router.py`  The file `router.py` contains two key components:  ### 1. `class ModelEndpoint` - A stub |
| `code-search-context-injection` | code_search | yes | yes | 85 | 5 | 0 | ### How Injection Works  1. **`ProjectContext`** — A marker/placeholder class (currently empty via `pass`). It represents the project-level  |
| `run-focused-tests` | tool_call | yes | yes | 19 | 1 | 0 | All tests passed: 1 passed in 0.00s, no failures. |
| `inspect-git-status` | tool_call | no | yes | 113 | 9 | 1 | ## Git Status — README Task  **Command output (`git status`):** ``` 位于分支 master 尚无提交  要提交的变更： 	新文件：   README.md ```  | File | Status | |---- |
| `generate-diff-preview` | tool_call | yes | yes | 55 | 4 | 0 | **Diff preview shown** for `notes.txt`: the proposed change would replace `"before change"` with `"after change"`. The file has **not been m |
| `call-weather-tool` | tool_call | yes | yes | 96 | 1 | 0 | 北京当前天气：**阴天**，气温 **36°C**（体感 40°C），今日温度范围 27°C ~ 36°C，湿度 44%，南风 23 km/h。属于酷暑天气，建议穿轻薄透气的短袖、短裤，注意防晒防暑，多补充水分。 |
| `remember-user-preference` | context_memory | yes | yes | 52 | 2 | 0 | 已更新 `.xenon/memory.md`，记录了用户偏好：最终回答使用简洁的中文摘要。 |
| `use-project-rules` | context_memory | no | yes | 154 | 15 | 1 | ✅ 任务完成。  根据 `.xenon/rules.md` 规则（keep Python changes small and add a focused test）：  | 步骤 | 操作 | 结果 | |------|------|------| | 读取规则 | `.xeno |
| `load-saved-session` | context_memory | yes | yes | 36 | 1 | 0 | Session restored successfully.  - **Session ID**: `fixture` - **AgentContext.recent**: `["hello"]`  The session history and context variable |
| `compact-long-context` | context_memory | yes | yes | 31 | 2 | 0 | Created `.xenon/compact_summary.md` with a concise summary that preserves the recent user request ('recent user request') while compressing  |
| `revise-after-test-failure` | multi_turn_revision | yes | yes | 127 | 7 | 0 | ## 执行总结  ### 1. 初始测试运行 ``` tests/test_buggy.py::test_add FAILED >   assert add(2, 3) == 5 E   assert -1 == 5 ``` **失败原因**：`add(2, 3)` 返回 `-1 |
| `revise-after-review` | multi_turn_revision | no | yes | 111 | 10 | 1 | `src/abstraction.py` 已成功简化。当前内容为：  ```python def run(value):     return value ```  验证结果： - 移除了 `LargeAbstraction` 类包装器 - 全项目搜索确认无 Python 文件引 |
| `handle-missing-api-key` | multi_turn_revision | yes | yes | 280 | 2 | 0 | ## Model Call Failure Due to Missing API Key  ### Why the Failure Occurs  When a model call (e.g., to OpenAI, Anthropic, or a similar LLM pr |
| `mcp-tool-flow` | multi_turn_revision | yes | yes | 683 | 4 | 0 | ## MCP 服务器注册与工具调用说明  ### 1. 当前 `.xenon/mcp.yaml` 配置  已添加一个名为 `fetch` 的 MCP 服务器：  ```yaml servers:   - name: "fetch"     command: "npx"     a |

## Xenon-Specific Value

> These metrics are separate from task success rate. `N/A` means the signal was not observable in this run; it is never treated as zero.

### Cache Rails and Cost

- Provider cache telemetry: **provider_reported**
- Cache field coverage: 100%
- Cache hit rate: 97.35%
- Reusable prefix / hit tokens: 1152256
- Cache rails: 3; rail forks: 14; context compactions: 0
- Estimated actual cost: ¥0.2567
- All-cache-miss baseline: ¥3.6847
- Estimated savings: ¥3.4280 (93.03%)
- Cost evidence: `provider_cache_fields`

### Routing, Governance and Recovery

- Models observed: deepseek/deepseek-v4-pro
- Fallback calls observed: 0
- Fallback success rate: N/A
- Permission telemetry: `observed_tool_governance`
- Permission denied/cancelled: 0/0; invalid-parameter blocks: 0; path blocks: 0
- Memory/recovery telemetry: `not_instrumented_in_this_eval`

### Efficiency

- Observed LLM calls: 131
- Tokens per successful task: 92762.62
- Cost per successful task: ¥0.0197

## Failure Summary

- `edit-python-function`: 任务已完成。

**src/main.py** — `greet` 函数已修改为接受可选的 `name` 参数：
```python
def greet(name=None):
    if name:
        return f"Hello, {name}"
    return "Hello"
```

**tests/test_main.py** — 测试已更新：
```python

- `add-unit-test`: []` (empty list) and `None` inputs. The source function `normalize_items` in `src/utils.py` correctly returns `[]` in both cases. No further changes are needed.
- `refactor-duplicate-code`: Refactoring already complete. `src/parser.py` has `_parse_parts` handling `strip()` + `split(':', 1)`, with `parse_left` and `parse_right` both calling it. No further changes needed.
- `code-search-entrypoint`: **CLI Entrypoint:** `main()`

**How `start_chat` begins chat mode:** It doesn't implement real chat. It's a stub that just returns the string `"chat"`. `main()` calls `start_chat()` and returns that v
- `inspect-git-status`: ## Git Status — README Task

**Command output (`git status`):**
```
位于分支 master
尚无提交

要提交的变更：
	新文件：   README.md
```

| File | Status |
|------|--------|
| `README.md` | ✅ new file, staged (tracked cha
- `use-project-rules`: ✅ 任务完成。

根据 `.xenon/rules.md` 规则（keep Python changes small and add a focused test）：

| 步骤 | 操作 | 结果 |
|------|------|------|
| 读取规则 | `.xenon/rules.md` | "keep Python changes small and add a focused t
- `revise-after-review`: `src/abstraction.py` 已成功简化。当前内容为：

```python
def run(value):
    return value
```

验证结果：
- 移除了 `LargeAbstraction` 类包装器
- 全项目搜索确认无 Python 文件引用旧类
- 函数签名和行为与原来的 `run` 方法保持一致
