# 组件真实任务矩阵（component matrix）

这个目录回答一个问题：**Xenon 的每个组件，面对真实任务能不能用，代价是多少？**

与单测的区别：这里不调用内核 API，也不让测试直接构造契约。每个场景都启动
**真实发布入口**——`node frontend/dist/cli.js`（TypeScript/Ink 前端）在
ConPTY 里跑起来，像用户一样键入任务、按审批面板的按键，然后只从
**账本 + 工作区**判断结果。模型是真实的 `deepseek/deepseek-v4-flash`。

## 运行

```bash
PYTHONIOENCODING=utf-8 python docs/evidence/component-matrix/run_matrix.py
PYTHONIOENCODING=utf-8 python docs/evidence/component-matrix/run_matrix.py --only plan
```

产物（每次运行覆盖）：

- `report.md`：通过/失败清单 + 每场景用时/token/工具/证据状态；
- `<场景>.transcript.txt`：终端录像（已压缩：去 ANSI、去连续重复行、只留尾部）；
- `<场景>.ledger.jsonl` / `<场景>.result.json`：该轮账本与内核结果。

运行环境隔离：每个场景一个临时 git 仓库；MCP 场景用临时 `USERPROFILE`/
`HOME`，不碰用户自己的 `~/.xenon/models.yaml`、`credentials.yaml`。

## 场景

| 场景 | 真实任务 | 断言的组件 |
|---|---|---|
| `read` | 读一个文件并引用其中的 token | `read` 工具、证据角标、无审批 |
| `write` | 创建脚本 → 运行 → 确认输出 | `apply_patch`、审批面板、`test` 回执、相对否定范围 |
| `bugfix` | 修 `calc.py`、跑 pytest、不动测试文件 | 多文件修改、`test` 工具、受保护路径 |
| `commands` | `/cost /cache /context /mode /tools /memory lessons /permissions` | 斜杠命令、交互 prompt、退出省钱报告 |
| `plan` | `/plan on` → 计划只读落地 → `/plan status` → `/plan approve` | 计划模式、写盘延迟、计划审批与续跑 |
| `session` | `/save` → 新进程 `--resume` → 从历史回答 | 会话持久化、跨进程恢复 |
| `mcp` | `/mcp add` 本地 stdio server → 调用 → 重启 | MCP 注册/发现/注入/调用/持久化 |
| `cancel` | 批准慢命令 → Ctrl+C → 继续用 REPL | 取消语义、审批阻塞、REPL 可用性 |
| `dashboard` | `--dashboard` → URL/token → 401/state/query | 本地面板服务、token 校验、只读查询帧 |

## 最近一次结果

见 `report.md`（表头带时间、模型、入口与通过数）。最近一次
（2026-11-16 14:17，`deepseek/deepseek-v4-flash`）为 **54/54 PASS**，
9 个场景合计墙钟 **213.8s**、真实计费 **88,695 tokens**。用时/token 是真实
墙钟与真实计费，不是模拟值；`dashboard` 场景不产生内核轮次，因此不出现在
token 表中。

## 已知限制（如实记录）

- **证据门的覆盖面**：`verified` 覆盖的是文件事实与工具回执。若用户目标还要求
  「运行某条命令」而模型只在回答里承认没跑，claim 层可以只有 `file_changed`
  这一条事实，回合仍可能 `evidence=verified`。这是“claims 与 objective 的
  执行要求不是同一集合”的已知缺口，未在本目录的修复中改掉。
- **计划模式的审批分工**：`/plan on` 的回合只提交计划、不写盘；真正的执行发生在
  `/plan approve`（或 `submit_plan` 被批准）之后。脚本按这个真实语义断言，
  不再把“计划回合里出现写审批”当作成功。
- **命令场景的期望文本**以当前前端措辞为准（如 `/mode` 的 `当前循环`）；
  文案改动需要同步更新脚本。
