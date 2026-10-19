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

## 当前边界

- 模型视图仍由工具类型选择器生成，尚未接入 0-LLM `StructuredCompactor`。
- 会话恢复还没有加载 result manifest；缓存路径/hash 已存在。
- raw 文件保留完整输出，因此本地磁盘可能包含命令日志中的敏感信息；
  缓存是本机 0600 文件，不是可分享的会话导出。
- 缓存不是证据：provenance、成功/失败状态、原始工具调用仍由证据层负责。

## 下一步

Phase 3：0-LLM `StructuredCompactor`。旧 observation 换成
`结构化摘要 + result_id`，保留失败/计划/产物，丢弃旧 raw 文本；
会话恢复加载 packet + result manifest；需要细节再读缓存。
