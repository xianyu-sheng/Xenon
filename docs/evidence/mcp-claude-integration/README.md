# Claude Code ↔ Xenon：真实 MCP 握手

本目录记录 **Claude Code 作为 MCP 客户端**连接 **Xenon 作为 MCP server** 的一次真实握手，
不是模拟帧。

| 项 | 取值 |
|---|---|
| 采集时间 | 2026-11-12 |
| 客户端 | Claude Code 2.1.319（`clientInfo.name = claude-code`） |
| 服务端 | Xenon 1.0.0（`serverInfo = {name: xenon, version: 1.0.0}`） |
| 启动参数 | `xenon mcp-serve --workspace D:/Xenon --permission read-only` |
| 传输 | stdio，JSON-RPC 2.0，每行一个对象 |
| 原始记录 | [transcript.txt](transcript.txt)（双向流量，逐行带时间戳） |

## 怎么采的

1. 起一个临时的 stdio 代理（保持 stdout 只承载协议，把双向流量追加到日志文件）；
2. 用 `claude mcp add xenon-logged -s local -- python mcp_log_proxy.py <xenon.exe> mcp-serve …`
   把它注册成一个**临时**的本地 MCP server；
3. 执行 `claude mcp list` —— 这会驱动 Claude Code 真正连接并握手；
4. 把日志复制进本目录，然后 `claude mcp remove xenon-logged -s local`，删除代理与临时文件。

代理只记录协议帧，不读取、不保存任何凭证。

## 记录里能看到什么

```text
client→server  server/discover           （探测，protocolVersion 2026-07-28）
server→client  -32601 method not found
client→server  initialize                （protocolVersion 2025-11-25）
server→client  protocolVersion 2025-06-18, serverInfo {xenon, 1.0.0}
client→server  notifications/initialized
client→server  tools/list
server→client  tools: xenon_run_task, xenon_evidence, xenon_audit
```

要点：

- **协商是按版本表来的**：客户端请求 `2025-11-25`，Xenon 回自己实现的最新版本
  `2025-06-18`，客户端接受并继续；这符合 MCP 的版本协商语义。
- **工具列表真的到达了客户端**：`tools/list` 的响应里三个工具的 `inputSchema` 完整可见。
- **发现式探测会退回**：新版客户端先发 `server/discover`（`2026-07-28`），Xenon 目前返回
  `-32601 method not found`，客户端随后回退到 `initialize`。这是已登记的后续项（见 Issues），
  不影响连接。
- 整个会话只读启动，没有任何写盘或执行请求。

## 复现

```bash
claude mcp add xenon -- xenon mcp-serve --workspace /path/to/repo --permission read-only
claude mcp list          # 应显示 xenon: … ✔ Connected
claude mcp get xenon     # Scope / Type / Status
```

在 Claude Code 会话里，工具名是 `mcp__xenon__xenon_run_task` 等（MCP server 在会话启动时加载，
新注册后需要重开会话或执行 `/mcp`）。
