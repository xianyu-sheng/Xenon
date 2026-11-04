# Dogfood #6：为什么"在别的目录里就做不了事"（#410, #411）

真实会话：`xenon` 0.9.7，启动目录 `D:\mybook`（一个 Electron **打包产物**目录），
用户要求「接受外部贡献，帮我加 CI 和社区文件」。

## 用户看到的

```text
● Kernel · 等待回答
  等待你的回答：当前工作区 D:\mybook 只是一个打包产物目录（locales/、resources/app.asar…），
  没有 package.json、README.md 或 .github/，我无法在这里创建 CI 与社区文件…请给我源码仓库的绝对路径
```

用户随后给了路径，模型报告：

```text
D:\mybook-repo 路径本身不可读：PermissionError: [Errno 13] Permission denied: 'D:\mybook-repo'
```

用户的判断：「感觉这个限制还是比较大，我没有在这个路径就没法执行，这是因为 Xenon 的产
品特点吗？」

## 三件事，性质不同

| # | 现象 | 性质 |
|---|---|---|
| 1 | 启动目录是产物目录，模型只能在里面工作 | **产品设计**（写入围栏），但缺少补救路径 |
| 2 | 读 `D:\mybook-repo` 得到 `PermissionError` | **缺陷**：`read_text()` 读目录在 Windows 抛 Errno 13，被误读成"没权限" |
| 3 | 「具体产物在这个路径」被改写成「资料调研」并继承上一轮的调研要求 | **缺陷**：提示词优化器串味（另开 issue） |

## 修复

**① 目录不是文件（#410）**：`read` 命中目录时不再让 OS 抛权限错，返回

```json
{"is_directory": true, "entries": ["..."], "error": "这是一个目录，不是文件：… 读取目录请用 search，或读取目录内的具体文件。"}
```

**② 围栏可以在会话内扩（#411）**：

- 新增 `xenon/nodes/sandbox_roots.py`（进程级、不落盘、只有 REPL 能调用，模型没有任何
  工具能触及）；
- `/workspace` 查看当前围栏：工作区根 + 默认临时目录 + `XENON_EXTRA_ROOTS` + 本会话添加；
- `/workspace add <目录>` / `remove` / `clear`；
- 越界时的错误信息不再只提环境变量，而是直接给出可执行的命令：

```text
路径越界: … 不在允许的目录 … 下。写入限制在工作区内。放行另一个目录：在提示符输入
`/workspace add <dir>`（仅本进程有效），或在启动前设置 XENON_EXTRA_ROOTS=;分隔的路径。
```

读取不受限（#378）保持不变：只有写和命令执行受围栏约束。

## 测试

- `tests/test_workspace_sandbox_roots.py`（6 条）：兄弟目录默认被拒且错误信息含
  `/workspace add`；加入后放行；读不受限；不存在的目录拒绝加入；remove/clear；
  env 变量仍然生效；目录读取返回 `is_directory` + `entries`。

> 写这些测试时踩到一个坑：仓库根 `conftest.py` 的 autouse fixture 把
> `ToolNode._validate_path` 整个换成了宽松版，断言围栏的用例必须声明
> `real_path_validation` fixture 才能拿到真实实现——否则测的是替身。已在用例里注明。
