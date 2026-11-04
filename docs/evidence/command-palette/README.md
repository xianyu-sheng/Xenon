# 命令面板（#183 第一步）：样子先对齐

## 做了什么

`/` 打开的不是纯文本列表，而是**从命令注册表自动生成**的可搜索面板：

```text
  /permissions    权限 · [default|accept_edits|bypass|plan]    查看/切换工具执行权限模式
  /plan           规划 · [on|off|status|approve|reject]
  /clear          会话
```

- **单一来源**：行来自 `COMMANDS`（名称 + 描述 + usage），不存在第二份菜单；
  新增一条命令自动出现（测试锁定）。
- **分组**：显式前缀表（最长匹配优先），未知命令落「其他」——仍可发现，只是未归类。
- **参数提示**：从 `usage` 的 `[...]` 提取，作为右侧 meta 列。
- **模糊搜索**：名称前缀 > 名称子序列 > 描述命中，确定性打分；
  `perm` → `/permissions`（测试锁定）。
- **最近使用置顶**：`~/.xenon/command_recents.json`（MRU，上限 12，坏文件/不可写一律忽略）。
- **Enter 执行选中项**：否则面板只是装饰——输入 `/perm` + Enter 会跑一条不存在的命令。
  绑定刻意收窄：只在「输入是裸 `/前缀`」时改写，文件路径补全保持「提交我写的」。

## 真实 TTY（真实 ConPTY）

| 场景 | 结果 |
|---|---|
| 输入 `/` | 面板出现（分组 + 参数提示列） |
| 继续输入 `perm` | 收敛到 `/permissions` 一行 |
| Enter | **执行 `/permissions`**，输出权限面板 |
| `/` + 方向键下两下 + Enter | 执行高亮行 |

transcript：`palette-and-enter.txt` / `arrow-navigation.txt` / `search-filter.txt`。

## 降级

面板是纯数据（`CommandCatalog`）+ prompt_toolkit 补全，没有自绘 UI：
- 无 PTY / `XENON_NO_PT=1` → 没有补全器，`/help` 仍是可发现的路径（既有测试覆盖）；
- `NO_COLOR` → meta 是纯文本，无 ANSI（测试断言 `"\x1b" not in meta`）；
- 窄终端 → 行内容 ≤ 60 字符。

## 还没做（本 issue 的第二步）

- 模式 / 模型 / 会话 / 权限的**统一选择器**（现在各自有参数，但不是选择器）；
- **文件引用选择器**（选中后插入路径，不让用户手打）；
- plan/claim/delivery 状态在面板中的固定位置。
