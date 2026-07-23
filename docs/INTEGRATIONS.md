# Xenon 外部集成 CLI

Xenon 为 Ark CLI、安装脚本和其他 Agent 管理器提供独立于交互式 REPL 的稳定命令。
这些命令不启动模型、不探测网络；传入 `--json` 后，stdout 只输出一个 JSON 对象，
错误说明和普通日志不会混入结构化结果。

## 能力发现

```bash
xenon integrations describe --json
```

输出包含契约版本、Xenon 运行版本、用户/项目 Skill 目录、MCP transport 能力和
建议命令模板。外部工具应先读取 `schema_version`，再决定如何安装；不要直接修改
Xenon 的私有 YAML。

退出码约定：

| 退出码 | 含义 |
|---|---|
| `0` | 成功，结构化结果可用 |
| `1` | 请求合法，但安装、校验或持久化失败 |
| `2` | 命令或参数用法错误 |

## 安装 Agent Skill

```bash
xenon skill install ./my-skill --json
xenon skill install ./my-skill --scope project --json
xenon skill install ./my-skill --scope shared-user --force --json
xenon skill list --json
xenon skill doctor --json
```

作用域为 `user`、`shared-user`、`project`、`shared-project`。默认写入
`~/.xenon/skills`；共享用户层写入 `~/.agents/skills`。项目作用域要求 Xenon 能
确定当前项目边界。

安装源可以是技能目录或其中的 `SKILL.md`。Xenon 会先验证 frontmatter、文件数、
总大小和符号链接边界，再复制到目标旁的临时目录并原子改名。已有同名技能不会被
静默覆盖；只有显式 `--force` 才会替换，并在 JSON 回执中返回 `replaced: true`。

## 配置 MCP

不含密钥的简单本地 MCP 可以直接添加：

```bash
xenon mcp add filesystem npx --json -- -y @modelcontextprotocol/server-filesystem .
```

包含 token、环境变量或认证头时，应通过 stdin 传 JSON/YAML，避免密钥进入 shell
history 和进程参数列表：

```bash
printf '%s' "$MCP_CONFIG_JSON" | xenon mcp add dataPro-search --config - --json
```

stdio 配置形态：

```json
{
  "transport": "stdio",
  "command": "uvx",
  "args": ["some-mcp-server"],
  "env": {"SERVICE_API_KEY": "<secret>"}
}
```

HTTP 配置形态：

```json
{
  "transport": "http",
  "url": "https://example.com/mcp",
  "headers": {"Authorization": "Bearer <secret>"}
}
```

也可以用 `--config /protected/path/config.json`。配置写入
`~/.xenon/credentials.yaml`，使用 `0600` 权限和跨进程锁；服务器在 Xenon 下次
启动或首次调用时惰性连接。

```bash
xenon mcp list --json
xenon mcp doctor --json
xenon mcp remove dataPro-search --json
```

`list` 和写入回执只显示 env/header 的键名、参数数量与脱敏 URL；不会回显值或 URL
query。`doctor` 只做本地格式、命令可用性与文件权限检查，不会连接远端服务器。
