# Xenon 发布验收清单

这份清单用于在创建 GitHub Release 或提交外部生态收录前，重复验证当前版本。除非特别注明，真实供应商测试不进入默认 CI。

## 本地门禁

在干净工作树中运行：

```bash
ruff check xenon tests
python -m compileall -q xenon tests
pytest tests -m "not live" -q --tb=short --no-header
# 交互前端是唯一产品界面：先构建单文件 bundle，再打进 whl
(cd frontend && npm ci && npm run build:release)
python scripts/build_frontend_assets.py --skip-build
python -m build
```

发行包检查（CI `package` job 自动执行）：

- wheel 必须包含 `xenon/frontend/assets/xenon-ui.js`；缺了它安装后 `xenon` 起不来。
- 干净虚拟环境里只装 wheel（不装 node_modules、不跑 npm）：`xenon --version` 成功，
  `xenon` 能拉起 TS 前端（需要机器上有 Node.js 20+）。

当前基线（2026-11-12，单循环内核 + TS 前端批次）：

- Ruff：通过（`xenon tests evals scripts`）。
- 离线套件（包含确定性 E2E）：`4080 passed / 101 skipped`，6m46s。
- 守卫：kernel purity、engine imports、docs references 全绿。
- 前端：`tsc --noEmit` 与 `npm test`（34 条）通过，`build:release` 产出单文件 bundle。

历史基线（v0.7.3，仅作对照）：

- Ruff：通过。
- 离线套件（包含确定性 E2E）：`1662 passed / 38 deselected`。
- 发行包：wheel 与 sdist 均成功生成。
- 全新 Linux 虚拟环境：安装 v0.7.3 wheel、导入 Xenon 和运行 `xenon --version` 均成功。

`live` 用例需要真实供应商凭证或公网，不能作为离线发布门禁；应在单独的验收记录中保存模型、日期、请求次数、失败原因和费用。

v0.7.3 已完成一次真实任务重跑：`11/20 = 55.0%`。历史 45% 数据仅作为比较基线；后续版本需要在相同任务集和隔离工作目录下重复执行，并将提交、费用和失败分类绑定到报告。

## 用户路径验收

每次准备新版本时，至少验证以下路径：

1. 全新 Python 3.10、3.11 或 3.12 虚拟环境（机器上有 Node.js 20+）安装并运行 `xenon --version`；
   只装 wheel、不跑 npm 时 `xenon` 能启动 TS 界面。
2. `/setup` 配置 Provider，启动一次普通对话。
3. ReAct 任务完成一次只读操作和一次需要确认的写入操作。
4. 拒绝权限请求、按 `Ctrl+C` 中断当前运行，界面仍可继续使用。
5. 保存会话后重新启动并 `/resume`，历史和执行状态可恢复。
6. 配置模型池后模拟首选模型失败，确认回退模型和实际模型记录正确。
7. 配置 MCP 时验证未连接、握手失败和工具列表为空等降级路径。

## 平台矩阵

GitHub CI 在 Linux 上运行完整测试，并在 Windows、macOS 上运行安装、导入、编译和 CLI 版本冒烟测试。这不等于完整验证了 Windows/macOS 的终端键位、PTY、剪贴板及全局热键；这些仍需要独立 runner 扩展或人工验收。

| 平台 | 自动化 | 发布前最低要求 |
|------|--------|----------------|
| Linux | CI 全套离线测试、E2E、构建 | CI 全绿 |
| Windows | CI 安装/导入/编译/CLI 冒烟 | PowerShell 启动、路径、Ctrl+C、基础 `/setup` |
| macOS | CI 安装/导入/编译/CLI 冒烟 | 安装、启动、TS 界面与 `/setup` |

## 发布记录

Release 记录至少包含：版本、提交、Python/OS、测试结果、构建文件名、已知限制、真实供应商验收（如有）和回滚方式。发布后再验证：

- GitHub Release 标签指向预期提交；
- PyPI（如发布）中的版本与 `xenon.__version__` 一致；
- README 安装命令能指向该版本；
- 外部 Awesome List 或社区提交使用同一项目简介和仓库地址。

### v0.9.4 — 2026-10-14

- 发行提交：tag `v0.9.4`（指向本版 CHANGELOG/版本号提交）。
- 门禁：CI main 全绿（Linux 全套离线 + 确定性 E2E + 三平台 smoke + package）；本地 `ruff check`、`compileall`、`python -m build`（wheel + sdist）与 `twine check` 通过。
- 产物：`dist/xenon_harness-0.9.4-py3-none-any.whl`、`dist/xenon_harness-0.9.4.tar.gz`。
- 已知限制：本机为 Windows，本地离线全量存在既有平台基线失败（symlink/编码等，见 #119），故以 CI Linux 全量结果为准；`python -m xenon` 自本版起在发布产物中可用。
- 回滚：`pip install` 回退 v0.9.3 wheel（README 历史提交可查）；或 `git checkout v0.9.3`。
- 真实供应商验收：本版未重跑，沿用 v0.9.3 记录。
