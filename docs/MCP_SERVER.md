# Xenon 作为 MCP server — 操作手册

`xenon mcp-serve` 通过 stdio 暴露三个工具，让其他 Agent（Claude Code、Codex 等）
在不放弃证据链的前提下调用 Xenon。

它和 `xenon mcp`（管理 Xenon 自己的 MCP **客户端**）是两件事：前者是 Xenon 提供能力，
后者是 Xenon 使用别人的能力。真实握手记录见
[docs/evidence/mcp-claude-integration](../evidence/mcp-claude-integration/README.md)。

## 1. 前置条件

- 已安装 Xenon（wheel 或源码），`xenon --version` 可执行；
- 目标工作区路径存在；
- 若要用 `xenon_run_task`，需要已配置模型（`/setup` 或环境变量）。

`xenon_evidence` 与 `xenon_audit` 不调用模型。

## 2. 启动

```bash
xenon mcp-serve --workspace /path/to/repo --permission read-only
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--workspace` | 当前目录 | 服务器允许触达的根目录 |
| `--permission` | `read-only` | 权限上限：`read-only` / `auto-edit` / `scoped` / `bypass` |
| `--allow-any-workspace` | 关 | 允许工具调用把 workspace 指到根目录之外 |
| `--model` | 配置里的默认模型 | 传给内核的模型 id 或 models.yaml 别名 |
| `--ledger-dir` | `~/.xenon/ledger` | `xenon_run_task` 的账本目录 |

**权限是 fail-closed 的**：服务器以 `read-only` 启动时，任务只能读取；写入/命令会被内核拒绝。
单次调用可以把权限**降低**，不能超过服务器上限。需要写入时由 Operator 显式启动为
`--permission auto-edit`。MCP 没有交互式审批面板，因此需要人工审批的调用会直接失败并返回原因，
而不是挂起等待。

## 3. 工具

| 工具 | 做什么 | 返回 |
|---|---|---|
| `xenon_run_task` | 在工作区跑一轮内核任务 | `task_id` / `phase` / `accepted` / `evidence` / `answer` / `debt` / `ledger` |
| `xenon_evidence` | 读一份账本并生成报告 | phase、evidence、claims↔receipts、实际运行的调用、完整性 |
| `xenon_audit` | 审计另一个 Agent 的 transcript | claims / contradictions / out_of_scope / gaps |

每个 `xenon_run_task` 都经过内核、都写 append-only 账本；返回的 `ledger` 可直接交给
`xenon_evidence` 或 `xenon report`。MCP 不会被当成绕过验证的通道。

## 4. 接入客户端

### Claude Code

```bash
claude mcp add xenon -- xenon mcp-serve --workspace /path/to/repo --permission read-only
claude mcp list        # 期望看到 ✔ Connected
claude mcp get xenon   # Scope / Type / Status
```

Windows 下 `xenon` 需要写成 `xenon.exe` 的绝对路径，或把 Python Scripts 目录放进 `PATH`。

在会话中使用时，工具名形如 `mcp__xenon__xenon_run_task`。**MCP server 在会话启动时加载**：
新注册后需要重开会话（或在支持的产品里执行一次重新加载），否则看不到工具。

移除：

```bash
claude mcp remove xenon -s local
```

### 项目级 `.mcp.json`

```json
{
  "mcpServers": {
    "xenon": {
      "command": "xenon",
      "args": ["mcp-serve", "--workspace", "/path/to/repo", "--permission", "read-only"]
    }
  }
}
```

## 5. 首次联调（30 秒）

1. `claude mcp list` 显示 `✔ Connected`；
2. 在会话里问一句「用 mcp__xenon__xenon_evidence 读一下最近的账本」；
3. 让模型调用 `xenon_run_task` 跑一个只读任务（例如「列出 src/ 下的文件」），
   确认返回里 `phase=finalized` 且 `ledger` 路径存在；
4. 用 `xenon report <ledger>` 复核同一份账本。

## 6. 排错

| 现象 | 原因 | 处理 |
|---|---|---|
| `✘ Failed to connect` | 命令路径不对 / `xenon` 不在 PATH | 用绝对路径重注册；先手动跑一次 `xenon mcp-serve ...` 看是否有输出 |
| 连上了但看不到工具 | 会话启动早于注册 | 重开会话；确认 `tools/list` 返回三个工具 |
| `tools/call` 返回 permission 错误 | 服务器上限是 `read-only`，而任务需要写入/命令 | 以 `--permission auto-edit` 重启服务器，或在只读任务里换一种问法 |
| 调用一直不返回 | 长任务受客户端超时约束 | 缩小任务、拆分调用；`xenon_run_task` 是同步的 |
| 审计结果大量 `out_of_scope` | 声明落在 workspace 之外或没有 VCS | 让 `--workspace` 覆盖被审计的工作区，并在有 git 历史的仓库里审计 |
| 客户端先发 `server/discover` 得到 `-32601` | 新版本客户端的发现式探测 | 客户端会自动回退到 `initialize`；不影响使用，见 Issues |

## 7. 协议

JSON-RPC 2.0，stdio，每行一个 JSON 对象。stdout **只**承载协议消息，诊断信息全部走 stderr。

支持的方法：`initialize`（协商 2025-06-18 / 2025-03-26 / 2024-11-05）、`ping`、
`tools/list`、`tools/call`、`shutdown`，以及通知 `notifications/initialized`。

## 8. 已验证

节点验收（真实 stdio，模型为配置中的 DeepSeek 端点）：

- `initialize` 协商到 `2025-06-18`，`serverInfo = {name: xenon, version: 1.0.0}`；
- `tools/list` 返回三个工具；
- `xenon_run_task("只回复两个字：可以")` → `phase=finalized`，`accepted=true`，
  `evidence=unverified`（纯回答没有可核对的声明，这是预期标签），12.3s，返回账本路径；
- `xenon_evidence` 读取真实账本 → `finalized / verified`；
- `xenon_audit` 读取 Claude Code 会话 → `partial`，177 条声明、0 contradiction、10 条 out_of_scope。

Claude Code 端到端（2026-11-12，Claude Code 2.1.319）：

- `claude mcp list` → `xenon: … ✔ Connected`；
- 真实握手记录：[docs/evidence/mcp-claude-integration/transcript.txt](../evidence/mcp-claude-integration/transcript.txt)。

## 9. 边界

- **单线程**：一个任务运行期间，其他请求排队；长任务受客户端自身超时约束。
- **没有审批 UI**：需要人工授权的调用直接失败（fail-closed），错误信息会说明原因。
- **模型必需**：`xenon_run_task` 使用配置中的模型；`xenon_evidence` / `xenon_audit` 不调模型。
- **证据标签照原样返回**：`verified` / `partial` / `unverified` / `contradicted` 不会被 MCP 层改写。
- **不是沙箱**：MCP 只暴露 Xenon 的能力边界；服务器本身仍应在受控环境运行。
