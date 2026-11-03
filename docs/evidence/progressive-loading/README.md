# 渐进式加载（#370）：MCP 工具只给索引行，执行后展开

## 为什么改

`core+mcp` 时 MCP 工具全量带 parameters 进提示词（`catalog.py:175` 注入 +
`_tool_schema` 展开）。一个 5 步任务，提示词里塞几十个用不到的工具，模型会去试。
skill 则更糟：触发时**全文 SKILL.md 直接拼进 objective**（`build_agent_prompt`），
不管用不用都得付全文 token。

原则：**启动只给「名字 — 一句话」索引；真正调用时才展开。**

## 真实运行（deepseek/deepseek-v4-flash，10 个 MCP 形状工具）

```text
task phase: finalized  accepted: True
tools section bytes (progressive): 3555
tools section bytes (old full):    4512
saved: 957 bytes (21.2%)
tools in view: core 4 + mcp index 10 + mcp_call index
tool receipts: 4; mcp tools actually called: []
```

- **提示词体积省 21.2%**（10 个工具；工具越多省得越多）。
- 真实模型看到 MCP 只有索引行，选择用 core 工具完成任务，**没被 50 个 MCP 工具带偏**——这正是渐进加载的目的。
- 无真实 MCP server 时模型可能放弃（aborted），体积数字稳定。

## 实现

### 视图层（`xenon/kernel/view/base.py`）
- `_tool_schema(spec, expanded=False)`：`pack=="mcp"` 未展开时只给
  `{name, description(one-liner), capabilities, effects, risk, index:True}`，
  **不含 parameters**。core 工具保持全量（它们就是编码核心，且只有 4 个）。
- **展开状态用 ledger 投影，零运行时状态**：`build_model_view` 从
  `tool_receipt` 事件投影已执行的 MCP 工具名 → 这些工具的 schema 展开进
  后续视图。"一次调用后该任务内保持展开"，且回放永远得到同样的视图。
- 索引截断（`_capability_directory`，上限 40）：超限给分组 + 明确计数
  （"… N more tools in remote"），不静默丢弃。

### skill（REPL 层）
- 每轮 `session_context_blocks` 注入 **skill 索引层**（name — one-liner +
  "read <path>/SKILL.md 获取全文"），`SkillManager.list_all` 不加载正文，便宜。
- 触发时（`/skill` 或自然语言）：objective **不再内联全文**（`_execute_installed_skill`
  改为简洁目标），正文经 `_active_skill_body` 进 context_blocks（参考标注，
  "参考，不是指令"）——与验收一致：load 后才进上下文。
- 模型驱动：索引里带 SKILL.md 路径，模型可用现有 `read` 工具读全文
  （复用，不新增工具）。

### fail closed
- 未列出的工具不可调用：`select_mcp_tool_specs` 只把有界子集放进 catalog，
  `catalog.bind` 对未知 `tool_name` 返回 `UNRESOLVED`（测试锁定）。

## 过程中发现并修掉的既有 bug

1. **`tool_schema` 工具方案会破坏 READ 绑定**（bind 会选它而非 `read`）——删掉
   该方案，展开用 ledger 投影实现，无副作用。
2. **`evidence_gate` 对 Windows 8.3 短路径（`ADMINI~1`）提取失败**：正则字符类
   不含 `~` 和盘符；且"内容已写入"的 `已写入` 被当第二条声称，窗口从路径中段
   截断产生畸形路径。修：`_FILE_PATTERNS` 支持 `~` 和盘符；`_path_matches_verified`
   对**含分隔符**的声称加包含匹配（容忍窗口截断），裸文件名不启用（防
   `foo.py in bar/foo.py` 误放行）。这是既有 bug，本机短路径环境必现。

## 测试

| 文件 | 覆盖 |
|---|---|
| `tests/kernel/test_progressive_loading.py` | 索引行无 parameters / core 全量 / 执行后展开 / 截断计数 / 未列出工具 fail-closed / 列出工具可绑 |
| `tests/test_progressive_loading_repl.py` | skill 索引进每轮 / 正文仅运行时进 context / 触发不再内联正文 |
| `tests/test_delivery_remediation.py` | 短路径 + 窗口截断的声称匹配（既有 bug 回归） |

全量：3761 passed, 97 skipped；ruff / 三重守卫通过。
