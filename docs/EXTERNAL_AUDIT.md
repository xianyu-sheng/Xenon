# 审查外部 Agent 的产出

Xenon 能验证自己的运行，是因为每个工具调用都会变成账本里的回执。别的 Agent
（Claude Code、Codex、任何其他实现）没有这样的账本，而它的会话日志由**被审查方
自己写**。日志里一条 `12 passed` 是声明，不是证据：它可能记错，也可能根本没跑。

所以 `xenon audit-external` 把两个角色分开：

| 来源 | 提供什么 | 可信度 |
|---|---|---|
| Agent 的会话日志 | **声明**——它说改了哪些文件、跑了哪些命令 | 只是说法 |
| Xenon 自己探测的工作区 | **事实**——磁盘上实际变了哪些文件 | 实测 |

工作区没有佐证的声明，会被如实标成未兑现。被审查的 Agent 无法自证清白，这就是
这个命令存在的全部意义。

## 用法

```bash
xenon audit-external ~/.claude/projects/<project>/<session>.jsonl --workspace .
```

```bash
xenon audit-external <session>.jsonl --workspace . --base HEAD~3
```

```bash
xenon audit-external <session>.jsonl --workspace . --json
```

## 它判什么、不判什么

**判**：日志声称写过的文件，工作区里是否真的变了。

**不判**：这个改动是否正确。文件变脏只能证明编辑落到了磁盘，不能证明编辑是对的。

| 证据标签 | 含义 |
|---|---|
| `verified` | 每条声明都在工作区找到了对应改动，且没有未声明的改动 |
| `partial` | 声明都对得上，但存在日志没有提及的改动 |
| `contradicted` | 有声明在工作区找不到对应改动 |
| `unverified` | 日志里没有可核查的写入声明 |

## 基线

默认基线取「会话第一条事件之前最近的一次提交」。没有基线的话，已提交的工作在
`git status` 里是干净的，每一个提交过的文件都会被误判成未兑现——这正是第一版
实现犯过的错。

基线解析不出来时（没有 git、历史不可读），命令会明确说明「所有声明均未被核实」，
而不是退回信任日志。

## 已知边界

- **只支持 Claude Code 的 JSONL 格式。** Codex 的 `rollout-*.jsonl` 结构不同
  （`response_item` / `custom_tool_call`），适配器尚未实现。
- **只看文件级改动。** 不比较内容，因此「改了这个文件」可证，「改对了」不可证。
- **未声明的改动不是指控。** 格式化工具、构建脚本、另一个会话都可能造成它；
  报告只陈述事实，由人判断。
