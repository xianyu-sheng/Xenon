# #165 dogfood：天气任务不再产出冗杂文档；但暴露了「回合级别」死锁

## 原始报告

用户：`查询一下天气，生成天气文档保存下来`。
0.9.3（旧 engine）实际结果：生成了混入大量无关内容的 `WEATHER_GUIDE.md`——
模型读了 `xenon/utils/weather.py` 源码，把实现细节当成「天气文档内容」写了进去。

## 真实运行复现（0.9.5 kernel，deepseek-v4-flash，真实 ConPTY）

三次独立运行，同一 prompt：

| 运行 | 产出文件 | 结局 |
|---|---|---|
| 1 | 0（只有 baseline `README.md`） | `Kernel · blocked`，模型 abstain 并提问 |
| 2 | 0 | 同上 |
| 3 | 0 | 同上 |

模型回答（运行 3）：

```text
⚠ 查询实时天气需要访问网络……但本轮执行边界明确禁止命令执行和动态工具/网络调用，
我没有任何可用的联网天气数据源，也不愿凭空编造天气数据。请确认两件事：
1) 要查询哪个城市？ 2) 是否授权我在本轮越过边界……
```

**结论 1：原 bug 不可复现。** 冗杂/混入/编造的文档在 kernel 下不会产生——回合被
`blocked`，没有任何文件被写。验收「只是输出天气文档，不含其他冗杂信息」在
「没有多余信息」这一半成立。

## 本轮修掉的真实缺口：模型在猜自己的执行边界

修复前，`build_model_view` 的系统提示里**完全没有权限/边界信息**，模型只能自己编
（真实运行里它编出了「本轮执行边界禁止 command 与动态工具」这种措辞）。

修复：`PermissionPolicy.describe_boundary()`（纯函数）+ `ModelView.boundary` +
系统提示 `Execution boundary:` 行。边界是运行时拥有的事实，由运行时陈述：

```text
[read-only]  reads anywhere are free (credential files excepted); edits are limited to
             the workspace; read-only turn: no edits, no execution; network reads need a
             human approval; push is never approvable
[auto-edit]  … safe test commands, commits and scoped stages run freely; other shell,
             network and verification calls trigger a human approval prompt — proposing
             them is allowed and expected; the harness asks; push is never approvable
```

边界行参与 `prefix_digest`（#372 D1），所以它是可断言、可缓存的前缀字段。

## 明确**没有**做的事（安全语义未动）

一度想放开「read-only 回合也可以申请执行」，但仓库有两条测试专门断言
`allow_approval 不得削弱 read-only`。那是一条安全规则，不是顺手可改的缺口，已回退。

## 结论 2：剩下的是一条独立设计缺口

`查询天气` 这类请求的意图级别是 read（`数据报告`），kernel 把它映射成
`PermissionMode.READ_ONLY` → 策略拒绝一切执行/网络 → 任务必然停在 `blocked`。
模型现在**准确报告**了这一点（不再是猜测），但用户没有「升级本轮级别」的入口。
这需要单独的设计（回合内级别升级 / `request_escalation` 动作），不属于本 issue 的
「文档冗杂」范畴。
