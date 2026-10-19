# Tool Result Cache（Phase 1）

## 为什么

当前模型只看到工具结果的前 3000 字符；完整 `ToolResult.raw` 只在调用内存中，
会话 `/save` 只保存摘要，因此“完整过程”并没有真正持久化。

Phase 1 先补基础设施：每次工具调用写一份**完整结果缓存**和稳定
`result_id`，但暂不改变模型看到的文本，也不改变证据层。

## 存储

```text
~/.xenon/sessions/<session_id>/tool_results/
  tr_<execution_id>/
    meta.json      # tool/status/params(redacted)/summary/hash/size
    raw.txt        # 完整结果（默认上限 5 MiB，超出标记 raw_truncated）
```

- 目录权限 0600，原子写入
- 参数中的 credential 键会被替换为 `[REDACTED]`
- 默认 TTL 7 天；REPL 首次使用缓存时做一次 best-effort 清理
- `XENON_TOOL_RESULT_CACHE=0` 可关闭
- `result_id` 只接受生成的 `tr_...` 标识，禁止路径

## Phase 2：模型视图与按需读取

- `ToolExecuteResult.model_observation`：command/read_file/search/list/git
  等工具生成确定性的结构化视图，带 `result_id`；证据层继续使用原
  `observation`，不改变 tracker/ledger。
- `tool_result_read(result_id, mode, query, start_line, max_lines)`：
  只读缓存，不执行命令；支持 summary/excerpt/range/search。
- `ThinkingStep` / `tool/result` 会话事件新增
  `result_id` + `raw_sha256`，让事实链锚定完整缓存。
- `tool_result_read` 自身不重复缓存，避免递归膨胀。

## Phase 3：确定性压缩与 result manifest

- `structured_compactor.py`：native tool block 的丢弃摘要改成
  `工具名 + 状态 + result_id + 关键摘要`，纯正则/分类器，0 LLM。
- `BaseEngine._compact_native_tool_messages` 与
  `_preprocess_tool_observations` 使用该摘要。
- `ContextManager` 维护 `tool_result_manifest`；`/save` 写入
  `extra.tool_results`，`/load`、`/resume` 恢复。
- `tool/result` 事件带 `result_id` / `raw_sha256`，事实链可锚定缓存。

## Phase 4：通用压缩也走结构化摘要

- `ContextManager.structured_summary()`：从用户目标、工具 trace、
  manifest、失败、产物、计划生成确定性摘要，0 LLM。
- `/compact` 无自定义摘要时使用该函数；不再调用 planner 模型。
- 自动压缩仍使用事件归档；新增 `compaction` 事件：
  `packet_hash` + `covers_result_ids`，可沿哈希链核对摘要覆盖范围。
- 归档中的每个工具条目保留 `result_id` / `raw_sha256`。

## 当前边界

- 无自定义摘要的 `/compact`、自动归档、native tool block、非 native
  in-run 压缩均已 0-LLM；只有直接调用 `ContextManager.compact()` 且不传
  summary 的外部 API 仍可能走原 LLM 摘要。
- raw 文件保留完整输出，因此本地磁盘可能包含命令日志中的敏感信息；
  缓存是本机 0600 文件，不是可分享的会话导出。
- 缓存不是证据：provenance、成功/失败状态、原始工具调用仍由证据层负责。

## 真实 #27 复验（结构化上下文开启）

| 指标 | 旧 #27 第二轮 | 本轮 |
|---|---:|---:|
| 耗时 | 478s | 249s |
| 工具调用 | 125 | 109 |
| 失败工具 | 19 | 8 |
| 上下文截断警告 | 3 | **0** |
| 计划更新 | 3 次 | 1 次（**仍过期**） |
| 提交 | `32d62a4` | `c9b6583` |
| 缓存 | 无 | 109 条 / 2.3MB |

测试：SmartBench 全量 647 passed / 6 failed（同一批 Windows 环境失败）。
说明：工具调用下降部分来自模型随机性，不能单独归因于缓存；但
“0 次上下文截断 + 完整结果可回查”是结构化缓存直接带来的可观察差异。

## C11 真实复验

`#27` 第三次复跑（`f86cbcf` + SmartBench `646c6c7`）：

- 125 工具 / 4 失败 / 358s / 0 安全失败
- `update_plan` 3 次，最终 **6/6 completed**
- 无“计划未完成”警告；两次收尾提醒 + 周期性计划提醒生效
- 与上一轮 109/8/249s（1 次计划后过期）相比，工具数上升，
  但计划收尾稳定；两条样本都无上下文截断

## 下一步

用真实 `/save → /resume → tool_result_read` 手测恢复链路；再补更多
长任务样本，避免用单条 #27 代表完成率。
