# Xenon 核验清单

> **目的**：每次修改后都可能发现新问题，但**不删除、不遗忘**之前已做的任务。
>
> **规则**
> - 新问题一律追加到“C. Open / 新发现”，不得覆盖或删除旧项。
> - 只有经过**真实模型 / 真实仓库 / 真实测试**复验，才把状态改成 ✅。
> - 每次提交、发布、开 PR 前先扫一遍本表；未复验项不得默认“已完成”。
> - 证据层（L0/L1/L2）与执行层分开记录；执行层的改动不得顺手放宽证据门。
>
> **状态**：✅ 已验证 · 🟡 部分验证 · ⏳ 待验证 · 🧊 暂缓/需单独决策 · ❌ 失败/待修

---

## A. 已完成且已真实复验

| ID | 事项 | 真实证据 |
|---|---|---|
| A1 | SWE-bench 公平性审计、重测、污染验证、归档 | 留出集 52.2% vs 46.7%，污染假说成立；结果已归档 |
| A2 | README 盲测协议、Issue #162、`docs/REPRODUCE_SWEBENCH.md` | 协议冻结表、两套样本 digest、复现步骤已提交 |
| A3 | D-1 拒绝误判、D-2 额外根目录、D-3 clone target、D-4 PR 工具、#163 验证提醒 | 真实场景 + PR #164 端到端（创建/关闭/删远端分支） |
| A4 | clone_repo 缓存根 / Basic 认证 / ≥300s 超时 | D:/tmp 启动，真实浅克隆 81.9s 后 read_file 成功 |
| A5 | 强制署名（commit trailer + PR footer） | 真实 commit 带 `Co-authored-by: Xenon`；PR #49 正文带 footer |
| A6 | 规划 JSON 裸键修复 + 解析失败降级 | 真实 13KB 计划恢复为 31 步；强制失败时写 ok.txt，不返回原始 JSON |
| A7 | 撤销循环“连续 3 次强制收束” | SmartBench 真实任务 83 次工具、73 次 LLM、313s 完成，分支 `feat/cli-progress-reporter`、提交 `c6f6046` |
| A8 | 规划改原生 `submit_plan` tool calling | 真实 DeepSeek：1 次 native 调用、0 次规划文本回退，文件正确落盘 |
| A9 | 执行契约（E-3）进入 ReAct + Plan 提示 | 真实 A/B：旧/新提示均落盘并通过 pytest；新提示收尾更短 |
| A10 | tracker 写类参数显示 `content=<N chars>`（E-1） | 真实总结明确写“content 参数已传入，共 5 字符” |
| A11 | 低置信度循环提示去重（E-2） | 真实同签名循环：提示 6 次 → 1 次；不新增硬停门 |

---

## B. 历史修复：仍需在真实使用中复验

| ID | 事项 | 当前状态 | 复验方式 |
|---|---|---|---|
| B1 | Windows `command` 退出码（PowerShell `$LASTEXITCODE`） | 🟡 单测 + 真实 stderr/管道验证 | 真实跑一次失败命令 + 管道，确认 rc 正确 |
| B2 | `gh auth token` 兜底 GitHub token | 🟡 单测 | 清空 env token，仅靠 `gh auth` 建一次 PR/读 issue |
| B3 | 引擎超时降级（`EngineDeadlineExceeded` 不整轮红错） | 🟡 单测 | 真实长任务触发时限，确认返回已完成步骤 |
| B4 | P1：`git repo_path` / issue 编号 / resume cwd / 循环升级 | 🟡 单测，循环升级已被 A7 取代 | 真实从上级目录 commit、`fetch_issue 27`、换目录 resume |
| B5 | P2：`XENON_ENGINE_TIMEOUT` / 恢复会话路径重映射 | 🟡 单测 | 真实设置 env + 恢复旧会话 |
| B6 | v0.9.5 wheel **不包含**署名 / clone 修复 / 执行层修复 | ⏳ 待下一次发布 | 发版前确认 wheel 含 post-0.9.5 提交 |
| B7 | 全量测试基线中的 Windows 既有失败 + flaky MCP 并发用例 | 🟡 已记录 | 每次改动对比基线，新增失败必须归零 |

---

## C. Open / 新发现（追加，不覆盖）

| ID | 事项 | 层 | 状态 | 下一步 |
|---|---|---|---|---|
| C1 | SmartBench #21 `--json` 未真正接到 CLI（`json_mode` 参数存在但命令未传） | 执行/CLI | ❌ 修复中 | 给 `quick` / 根 `--quick` 加 `--json`，进度走 stderr，stdout 只输出 JSON，并加 CLI 测试 |
| C2 | SmartBench #21 长任务仍出现约 14 次循环提示（循环信号时有时无） | 执行/提示噪音 | ⏳ | E-2 去重只覆盖连续同签名；后续加跨间隔冷却，仍不新增门 |
| C3 | 新文件“盲写”警告（新文件无法先读） | **证据层** | 🧊 单独决策 | 不混入执行层；评估在事实绑定中区分“新建文件”与“修改已有文件” |
| C4 | `command` 安全策略拦截 `$()` / `python -c`，模型绕路浪费步数 | 执行/环境 | ⏳ | 评估放行只读 Python 片段或给替代写法提示，保持安全边界 |
| C5 | `write_code` 仍默认 flash，长任务纪律弱于 pro | 执行/模型路由 | ⏳ | 用同一 SmartBench 任务对比 flash vs pro，再决定默认路由 |
| C6 | SmartBench #21 分支只在临时 clone：`D:/tmp/smartbench-rerun`，未 push / 未开 PR | 交付 | ⏳ | C1 修完后 push + PR（需确认） |
| C7 | SmartBench #21 验收缺口：TTY 单行、`--json` 无噪音、failing reporter 不中断 | 执行/验收 | 🟡 | 补真实 CLI 测试；C1 完成后跑一次 TTY/非 TTY/JSON |
| C8 | 智能体在临时仓库使用 `git add -A` | 执行/工作区安全 | ⏳ | 提示词/工具层引导显式路径；无无关改动时不算错误，但应避免 |
| C9 | 全量测试仍有 7 个 SmartBench Windows 既有失败（symlink/graph snapshot） | 上游既有 | 🧊 非本次引入 | 与基线对比确认；不顺手修无关测试 |
| C10 | 发布节奏：v0.9.5 后未再发版 | 发布 | ⏳ | 等 C1/C6 与用户核验完成后再决定版本号 |

---

## D. 核对记录

| 日期 | 变更 | 结果 |
|---|---|---|
| 2026-10-18 | 执行层 P0：撤销循环硬停 + 原生 `submit_plan` | 8b13ee3；真实复验通过 |
| 2026-10-18 | E-3/E-1/E-2：执行契约 + tracker 展示 + 循环提示去重 | 757c6db；3388 passed，CI 7/7 |
| 2026-10-18 | SmartBench #21 长任务复验 | 83 工具 / 73 LLM / 313s；分支 `feat/cli-progress-reporter`，提交 `c6f6046`；targeted 91 passed，全量对比基线无新增失败 |
