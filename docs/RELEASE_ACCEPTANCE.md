# Xenon 发布验收清单

这份清单用于在创建 GitHub Release 或提交外部生态收录前，重复验证当前版本。除非特别注明，真实供应商测试不进入默认 CI。

## 本地门禁

在干净工作树中运行：

```bash
ruff check xenon tests
python -m compileall -q xenon tests
pytest tests xenon/tests -m "not live" -q --tb=short --no-header
uv build
```

当前 v0.7.3 基线：

- Ruff：通过。
- 离线套件（包含确定性 E2E）：`1662 passed / 38 deselected`。
- 发行包：wheel 与 sdist 均成功生成。

`live` 用例需要真实供应商凭证或公网，不能作为离线发布门禁；应在单独的验收记录中保存模型、日期、请求次数、失败原因和费用。

## 用户路径验收

每次准备新版本时，至少验证以下路径：

1. 全新 Python 3.10、3.11 或 3.12 虚拟环境安装并运行 `xenon --version`。
2. `/setup` 配置 Provider，启动一次普通对话。
3. ReAct 任务完成一次只读操作和一次需要确认的写入操作。
4. 拒绝权限请求、按 `Ctrl+C` 中断当前运行，REPL 仍可继续使用。
5. 保存会话后重新启动并 `/resume`，历史和执行状态可恢复。
6. 配置模型池后模拟首选模型失败，确认回退模型和实际模型记录正确。
7. 配置 MCP 时验证未连接、握手失败和工具列表为空等降级路径。

## 平台矩阵

GitHub CI 当前覆盖 Linux 与 Python 3.10–3.12。Windows 和 macOS 的终端键位、PTY、剪贴板及路径行为需要独立 runner 或人工验收后，才能在发布说明中声称完整跨平台验证。

| 平台 | 自动化 | 发布前最低要求 |
|------|--------|----------------|
| Linux | CI 全套离线测试、E2E、构建 | CI 全绿 |
| Windows | 待独立 runner | PowerShell 启动、路径、Ctrl+C、基础 `/setup` |
| macOS | 待独立 runner | 安装、启动、基础 TUI 和 `/setup` |

## 发布记录

Release 记录至少包含：版本、提交、Python/OS、测试结果、构建文件名、已知限制、真实供应商验收（如有）和回滚方式。发布后再验证：

- GitHub Release 标签指向预期提交；
- PyPI（如发布）中的版本与 `xenon.__version__` 一致；
- README 安装命令能指向该版本；
- 外部 Awesome List 或社区提交使用同一项目简介和仓库地址。
