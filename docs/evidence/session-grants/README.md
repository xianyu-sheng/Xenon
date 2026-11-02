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

---

# 规则层（对齐 Claude Code 的 settings）

Claude Code 的模型（本实现跟进）：

- 规则形状 `Tool(pattern)`，例如 `Bash(pytest:*)`、`Read(src/**)`；
- 三个列表 `allow` / `ask` / `deny`，**deny > ask > allow** —— 宽的 allow 永远无法解开被 deny 的东西；
- 规则分层（用户 / 项目），项目文件可以进 git 团队共享；
- "don't ask again" 是**写一条规则**，不是翻一个看不见的开关。

Xenon 保留自己的语义：规则名是**效果能力**（kernel 不认识工具名），匹配目标是路径或命令；规则是**记录在案的人类决定**，永远不是证据。

## 优先级是安全性质（实测）

```text
git log            -> allow  scoped grant: *
rm -rf /tmp/x      -> veto   denied by rule shell(rm *)
```

broad allow（`shell`）与 deny（`shell(rm *)`）同时存在时，deny 胜。

三种优先级都有测试：
- `deny` 胜 `allow`（上面这条）；
- `ask` 胜 `allow`（grant 存在仍强制询问）；
- 无匹配返回 `None`（走原逻辑）。

## 四个选项（对齐 Claude 的流畅度）

```
y=只这次  a=本会话都允许  A=永远允许（写入规则文件）  s=只允许这条命令  n=拒绝  q=取消
```

- `a` → 会话级 grant（`GrantBook`，不落盘）；
- `A` → 写 `~/.xenon/permissions.yaml` 的一条 `allow` 规则，**作用域是这条命令本身**（不是整个能力）；
- `s` → 只覆盖该 target；
- 每次授予都在账本里有 `grant_added`。

`/permissions` 列出会话级 + 规则文件两类，`/permissions remove <selector>` 撤销并可写回文件。

## 实现位置的约束

`_TOOL_CAPABILITIES`（工具名 → capability）**不能放在 kernel**：`check_kernel_purity.py` 明确禁止内核出现具体工具名。它放在 `xenon/repl/permission_map.py`。这条被守卫抓到过一次，是我先放错了地方。


---

# 提交前自查 patch 覆盖（避免 Codecov 邮件）

## 为什么

Codecov 的 patch 门是真门，但它**只在 push 之后说话**。上一轮我先跑了 `pytest tests -q`（3659 全绿）就推了，结果 patch 只有 58.9% —— 因为新代码全在 REPL/命令层，正是全量测试覆盖最少的地方。

## 用法

```bash
python scripts/patch_cov.py                      # 跑测试 + 生成报告 + 判定
python scripts/patch_cov.py --base 3338a0b~1     # 对比指定基线
python scripts/patch_cov.py --from-xml           # 复用已有 coverage.xml（会警告过期）
```

真实输出（本轮）：

```text
patch coverage: 99.35% (305/307 executable added lines, threshold 95%)
  repl/runtime_adapter.py: 2 missing -> 656, 657
```

与 Codecov 的 99.34% 基本一致（差一行是 merge-base 范围差异）。

## 语义对齐

| 关注点 | 做法 |
|---|---|
| 哪些行是"新增" | `git diff --unified=0` 的 hunk 头，先取 `merge-base` 避免把基线漂移算进来 |
| 哪些行"可覆盖" | 只算 coverage.xml 里存在的行（空行/注释不计，Codecov 同样） |
| 路径归一化 | coverage 是 source 相对（`kernel/a.py`），diff 是仓库相对（`xenon/kernel/a.py`） |
| 过期报告 | 有 `xenon/**` 比 xml 新就警告——复用旧报告会让"缺失行"变成假话 |
| 退出码 | 低于阈值 1，无可测行 0（并说明），参数错 2 |

CI 里也加了同一道（`--threshold 90`，只对 PR 生效）：比 Codecov 的 ~80% 更严，但不至于因为一个未覆盖分支卡住 PR。
