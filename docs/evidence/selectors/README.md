# 统一选择器 + 两种真 bug（#183 第二步）

## 交付

### 1. 一个选择器，被三条命令共用

`/mode`、`/model`、`/permissions` 无参数时打开同一个选择器：

```text
选择思考范式:
  1) direct             直接对话，不使用特殊阶段（默认模式）（别名：react） ←
  2) plan-execute       规划阶段 + 按计划逐步执行（follow_plan）
  3) reflection         执行 + 一轮反思审查（reflect_rounds=1）
  ...
输入编号或名称（Enter = direct）:
```

三条设计约束：

- **选项写在提示文本里，不只靠补全菜单。** winpty 会把方向键的 ESC 拆成两次事件，
  菜单会被 ESC 关掉——选项不可见的"选择器"比没有更糟。编号/名称/空回车都可用。
- **空回车 = 当前项**，不是第一项。
- **无终端就返回 None**，调用方继续打印原来的文本列表——零回退风险。

### 2. `@file` 文件引用

输入 `@alp` + Tab/补全 → 插入 `alpha.py`，拼成 `@alpha.py`。
`PathCompleter` 只给"差异后缀"（`start_position=0`），按它插入即可，
自己再算偏移会吃掉真实路径字符（第一版就是这么错的）。

## 真实 TTY

| 场景 | 结果 |
|---|---|
| `/mode` | 列出 6 个阶段配置 + 当前标记 |
| 输入 `2` + Enter | 切换到 `react`（`✅ 已切换到范式: react`） |
| `/permissions` | 同时回答「当前权限模式」与「记住的授权」 |
| 空回车 | 保留当前项 |

transcript：`mode-picker.txt` / `permissions-merged.txt`。

## 修掉的两个真 bug

### A. `/permissions` 被静默覆盖（#373 的功能对用户不存在）

`commands.py` 先导入 `command_groups/permissions.py`（#373：查看/撤销记住的授权），
**后再导入 `workspace.py`，后者又注册了一次 `/permissions`（切模式）并胜出**。
结果：#373 的"记住的授权，看得见、撤得掉"从来没有到达过用户。

现在只有一个 owner（`permissions.py`），命令合并为「模式 + 授权」：
- 无参 → 选择器 + 列出授权；
- `<模式名>` → 切模式；
- `remove <selector>` / `clear` → 撤销/清空授权。

测试用**处理器模块标识**锁住唯一 owner：
`_HANDLERS["/permissions"].__module__ == "xenon.repl.command_groups.permissions"`。

### B. `react` 已和 `direct` 完全等价（7 引擎时代的残留名）

用户问："现在还有 react 吗？我们不是单循环吗？" —— 对，这是残留。

实测：`MODE_CATALOG` 有 7 个名字，只有 **6 种不同的 `LoopProfile`**；
`react` 与 `direct` 的 profile 完全相同（都是 `_ACT_ONLY`）。

处理：
- `react` 标记为 `alias_of="direct"`，**名字保留**（`--mode react` 仍然可用）；
- 面板与 `/mode` 只列 6 个**真正不同**的阶段配置，别名写进说明列；
- 描述改成"阶段配置"而不是"循环/引擎"（`reflect_rounds=1` 这类事实）；
- `/mode` 标题写明「单循环的阶段配置，不是不同的引擎」。

测试锁定：`react` 的 profile == `direct` 的 profile、`distinct_modes()` 两两不同、
面板里 `react` 不占独立行但仍在 `direct` 的说明中出现、当前模式即使是别名也会被显示。
