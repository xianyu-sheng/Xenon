# 会话级授权（#373）：一次弹窗、决策入账、不再重复问

真实模型、真实仓库。复现：

```bash
cd D:/Xenon
PYTHONIOENCODING=utf-8 python docs/evidence/session-grants/grant_probe.py deepseek/deepseek-v4-flash
```

## 为什么改

Xenon 的立身之本是**证据校验**；权限是**安全边界**。但实现变成了"每次都问"，真实使用里我被迫 `--permission bypass` —— **被绕过的安全措施等于没有**。

## 真实运行（修后）

两个任务，同一个需要 `shell` 授权的操作；父级 provider 第一次回答"本会话都允许"：

```text
  prompt#1 for capability=shell
turn 1: phase=finalized prompts=1 grant=yes
  remembered: shell scope=*
  grant_added in ledger: True
turn 2: phase=finalized prompts=1 grant=no

total prompts across 2 turns: 1 (expected 1)
```

- 第一个任务：**一次**弹窗，决策写入账本（`GRANT_ADDED`）；
- 第二个任务：**零**弹窗，任务照样 `finalized`。

## 语义边界（这些都是测过的）

| 性质 | 说明 |
|---|---|
| 不是证据 | grant 不进 claims、不改 verdict、不把 `UNVERIFIED` 变 `VERIFIED` |
| 工作区绑定 | 换仓库自动失效；同一路径的不同写法（MSYS vs Windows）必须归一化 |
| 能力绑定 | `verify` 的授权不覆盖 `shell` |
| 破坏性命令不可授权 | `rm -rf /` 仍是 `destructive shell command` VETO |
| push 不可授权 | 仍必须走 `TaskSpec.allow_push`（#201） |
| 只有人能创建 | 模型与子任务只能"请求"，不能"记住" |

## 两个被测试抓出来的真 bug

1. **wildcard grant 绕过了 destructive 检查**。原 `check_intent` 先查 grant 再查安全规则，于是 `scope="*"` 的会话授权让 `rm -rf /` 变成 `ALLOW`。修法：抽出 `_hard_denial()`，**硬安全规则先于任何 grant**。
2. **同一路径两种写法导致 grant 失效**。前端传 MSYS `/tmp/x`、内核记的是解析后的 Windows 路径，存下来的 grant 永远不生效。修法：`_path()` 先 `resolve()` 再比较。

另外一个既有测试（`test_scoped_grant_expires`）原本断言 scoped grant 可以授权 `git push` —— 这与 #201 的原则冲突。已改为 `git fetch`，并新增"grant 不能授权 push"的测试。

## 还没做（下一步）

- REPL 的四选一交互（`y` 一次 / `a` 本会话 / `s` 只这条 / `n` 拒绝）；
- `/permissions` 列出与撤销；
- 会话授权写入 session `extra`，`/save` + `/resume` 后仍在；
- 子任务继承父级 grant 但不新增。
