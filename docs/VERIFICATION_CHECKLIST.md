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
| B1 | Windows `command` 退出码（PowerShell `$LASTEXITCODE`） | ✅ 已验证 | 真实 stderr+管道 rc=0；`sys.exit(3)` rc=3 |
| B2 | `gh auth token` 兜底 GitHub token | ✅ 已验证 | 清空 env token、空 credentials，仅靠 `gh auth` 加载 token 并真实 fetch issue #21 |
| B3 | 引擎超时降级（`EngineDeadlineExceeded` 不整轮红错） | ✅ 已验证 | `XENON_ENGINE_TIMEOUT=0.01` 的 PlanReact 返回“引擎时限”；非法值回退 600 |
| B4 | P1：`git repo_path` / issue 编号 / resume cwd / 循环升级 | ✅ 已验证 | 父目录下 `repo_path` git status 成功；issue #21 拉取；cwd notice + 路径重映射 |
| B5 | P2：`XENON_ENGINE_TIMEOUT` / 恢复会话路径重映射 | ✅ 已验证 | env=123 生效；旧/新目录同相对路径重映射成功 |
| B6 | v0.9.5 wheel **不包含**署名 / clone 修复 / 执行层修复 | ⏳ 待下一次发布 | 发版前确认 wheel 含 post-0.9.5 提交 |
| B7 | 全量测试基线中的 Windows 既有失败 + flaky MCP 并发用例 | 🟡 已记录 | 每次改动对比基线，新增失败必须归零 |

---

## C. Open / 新发现（追加，不覆盖）

| ID | 事项 | 层 | 状态 | 下一步 |
|---|---|---|---|---|
| C1 | SmartBench #21 `--json` 未真正接到 CLI（`json_mode` 参数存在但命令未传） | 执行/CLI | ✅ 已修复 | `quick` / 根 `--quick` / `diagnose` 已加 `--json`；进度走 stderr，stdout 只输出 JSON；CLI 测试 55 passed，全量 669 passed、无新增失败 |
| C2 | SmartBench #21 长任务仍出现约 14 次循环提示（循环信号时有时无） | 执行/提示噪音 | ✅ 已加冷却 | 12 迭代冷却 + 签名去重；真实复跑从 14 次降到 6 次，仍无硬停门；如需更低可再调大冷却 |
| C3 | 新文件“盲写”警告（新文件无法先读） | **证据层** | 🧊 单独决策 | 不混入执行层；评估在事实绑定中区分“新建文件”与“修改已有文件” |
| C4 | `command` 安全策略拦截 `$()` / `python -c` / 未加引号 `$变量`，模型绕路浪费步数 | 执行/环境 | 🟡 已显著改善 | `run_script` + PowerShell 自动识别 + `lang` action 级别名；#26 长任务安全失败 1→0；#27 修复后 `run_script` 14/14 成功；仍有 1 次变量误拦截，错误文案已改为指向 run_script |
| C5 | `write_code` 仍默认 flash，长任务纪律弱于 pro | 执行/模型路由 | ✅ 数据结论：保持 flash | v1 pro 略省工具，v2 pro 更慢/更多调用；两者完成率相同；不做全局 pro 路由，长任务按需使用 |
| C6 | SmartBench #21 分支只在临时 clone：`D:/tmp/smartbench-rerun`，未 push / 未开 PR | 交付 | ✅ 已推送 | PR [#50](https://github.com/xianyu-sheng/SmartBench/pull/50) open、mergeable、CI 7/7 绿；等待维护者合并 |
| C7 | SmartBench #21 验收缺口：TTY 单行、`--json` 无噪音、failing reporter 不中断 | 执行/验收 | 🟡 单元/CLI 已覆盖 | `--json` 已接 CLI 并有测试；TTY 单行用 forced-TTY 单测覆盖；仍建议在真实终端手测一次 |
| C8 | 智能体在临时仓库使用 `git add -A` | 执行/工作区安全 | ⏳ | 提示词/工具层引导显式路径；无无关改动时不算错误，但应避免 |
| C9 | 全量测试仍有 7 个 SmartBench Windows 既有失败（symlink/graph snapshot） | 上游既有 | 🧊 非本次引入 | 与基线对比确认；不顺手修无关测试 |
| C10 | 发布节奏：v0.9.5 后未再发版 | 发布 | ⏳ | 等 C1/C6 与用户核验完成后再决定版本号 |
| C11 | ReAct 缺少 Codex 式显式 todo/plan 状态（`update_plan`），复杂任务只能靠提示词自律 | 执行/状态 | 🟡 工具可用，收尾不稳定 | 原生 schema + 跨文件/长任务提示 + 收尾一致性提醒；#26 长任务全部 completed；#27 第一轮 2 次更新后仍留 pending，第二轮 3 次才全部 completed；计划 UI/跨会话未验证 |
| C12 | 缺少多步任务“完成率”基准，容易凭感觉调提示词/循环 | 度量 | 🟡 v1/v2 完成 | v1、v2 均 5/5；v2 修正浮点断言后仍无区分度；已归档 v2 原始/修正数据；下一步真实长任务或 v3 |
| C13 | 写/实现任务连续 3 次工具失败会直接 mercy 收束（C4 真实复跑 77s、0 实现） | 执行/失败恢复 | ✅ 已修复 | 写任务改为最多 2 次“换方法”提示，不提前收束；查询任务保留 3 次收敛；真实 #26 从 15 次失败恢复完成 |
| C14 | DeepSeek 截断后输出 `<｜｜DSML｜｜ calls>`（带空格），旧解析器不认，整轮判格式错误 | 执行/协议 | ✅ 已修复 | 解析器接受 `calls/tool_calls` 及空白变体；`_structured_response_kind` 同步；单测覆盖 |
| C16 | C12 v1/v2 任务偏易，flash/pro 均满分，无法区分完成率 | 度量 | 🟡 真实长任务已有对照 | #26 两轮 217→147s、安全失败 1→0；#27 两轮均提交（331s/98 工具、478s/125 工具），第二轮暴露上下文截断；仍需更多 issue/PR 样本 |
| C17 | #165 简单查询/报告文档被当成技术文档，内容冗杂并触发无关验证说明 | 输出/上下文 | ✅ 已修复 | `write_report` + 报告模板/策略 + 文档写入不触发代码验证；多轮回放 `PROMPT_HISTORY=[]`，报告 742 字符无旧项目内容，最终回答 303 字符 |
| C18 | 工具完整结果只在内存，会话/压缩无法回查；全局 max_chars 只能截断 | 执行/存储 | 🟡 Phase 4 + 真实复验 | 缓存/`result_id`/结构化视图/回查/哈希锚定；native 与 `/compact` 已 0-LLM；#27 复验 0 次上下文截断、109 工具/8 失败/249s；非 native in-run 仍可能走 LLM |
| C15 | 只读任务写 `.xenon_tmp/*.ps1` 触发“改完代码要验证”，模型偏离任务去解释没有测试 | 执行/提醒 | ✅ 已真实验证 | 只读 issue 列表任务：无验证提醒，最终正确列出 25 个 issue；仍会写临时脚本/尝试 `$()`（C4） |

---

## D. 核对记录

| 日期 | 变更 | 结果 |
|---|---|---|
| 2026-10-18 | 执行层 P0：撤销循环硬停 + 原生 `submit_plan` | 8b13ee3；真实复验通过 |
| 2026-10-18 | E-3/E-1/E-2：执行契约 + tracker 展示 + 循环提示去重 | 757c6db；3388 passed，CI 7/7 |
| 2026-10-18 | SmartBench #21 长任务复验 | 83 工具 / 73 LLM / 313s；分支 `feat/cli-progress-reporter`，提交 `c6f6046`；targeted 91 passed，全量对比基线无新增失败 |
| 2026-10-18 | SmartBench #21 `--json` 接线 | 提交 `527c1ab`；CLI/progress 55 passed；全量 669 passed，与基线相同 7 个既有失败、无新增 |
| 2026-10-18 | C2/C4/C13/C14/C15 执行层加固 | Xenon `1b3522a`；全量 3400 passed、无新增失败；真实 SmartBench 99 工具/86 LLM/272s，选 #26、分支 `fix/26-resource-leak-visibility`、提交 `fa7a50b`、91 tests |
| 2026-10-18 | C15 真实只读复验 | 无验证提醒；`github_fetch(list_issues, limit=100)` 返回 27 条，最终正确列出 25 个 issue |
| 2026-10-18 | C12 v1 完成率基准 | 5 任务 × flash/pro 均 5/5；pro LLM 46 vs 58、失败工具 4 vs 11；任务偏易，进入 C16 v2 |
| 2026-10-18 | C12 v2 完成率基准 | 修正浮点断言后 flash 5/5（58 LLM/69 tools/4 fail/121.7s）、pro 5/5（73/78/10/203.3s）；v2 仍偏易，pro 无优势 |
| 2026-10-18 | B1–B5 真实复验 | 5/5：退出码 rc=0/3、gh token 兜底、deadline 降级、repo_path/issue/remap、env timeout |
| 2026-10-18 | C11 update_plan | 真实跨文件任务 4 次更新；SmartBench 长任务 3 次更新、计划全 completed、分支 `fix/issue-26-resource-leak-visibility`、提交 `9dbc648`、18 tests；Xenon 全量 3406 passed、无新增失败 |
| 2026-10-18 | C4 run_script | 真实 JSON 任务 0 次安全失败，sum=10；`run_script` 原生 schema 要求 code；Xenon 全量 3412 passed、无新增失败 |
| 2026-10-18 | C4 run_script 显式验证 | `run_script(language='python', code='print(123*456)')` 成功返回 56088；自然 JSON 任务 0 安全失败；Xenon 全量 3412 passed |
| 2026-10-18 | C4+C11 SmartBench 长任务 | 首轮 217s/85 工具/4 plan 全 completed/1 安全失败；修复 PowerShell 自动识别后 147s/50 工具/3 plan 全 completed/0 安全失败；提交 `2ea99f9`，5 passed，全量 642 passed/7 既有失败；Xenon 全量 3413 passed、无新增失败 |
| 2026-10-18 | #27 长任务两轮 | 均完成三项并提交：`e0f8988`（331s/98 工具/21 tests/2 安全失败）、`32d62a4`（478s/125 工具/14 tests/1 安全失败）；修复 `lang` 别名后 run_script 14/14；两轮全量均 7 个既有失败 |
| 2026-10-18 | #165 修复 | 天气+文档从 `write_doc` 改为 `write_report`；文档 556 字符无安装/参数；最终回答 219 字符；全量 3424 passed、无新增失败 |
| 2026-10-18 | #165 历史投影 | 旧项目文档/工具结果存在时，天气报告注入历史 0 条；报告 742 字符、无 DEVELOPER/安装/参数；最终 303 字符；全量 3429 passed、无新增失败 |
| 2026-10-18 | 工具结果缓存 Phase 1 | 每次工具调用写 `raw.txt`+`meta.json`，`result_id` 进入 `ToolExecuteResult`/tracker；Xenon 全量 3434 passed、无新增失败 |
| 2026-10-18 | 工具结果缓存 Phase 2 | 模型视图/按需读取/哈希锚定完成；5+5 新测试；Xenon 全量 3438 passed、无新增失败 |
| 2026-10-18 | 工具结果缓存 Phase 3 | native block 结构化摘要（0 LLM）；manifest 随 `/save` 恢复；Xenon 全量 3441 passed、无新增失败 |
| 2026-10-18 | 结构化通用压缩 | `/compact` 无参数走 0-LLM 结构化摘要；自动压缩事件带 `packet_hash`/`covers_result_ids`；Xenon 全量 3444 passed、无新增失败 |
| 2026-10-18 | 结构化上下文 #27 复验 | 109 工具/8 失败/249s/0 次上下文截断；缓存 109 条 2.3MB；提交 `c9b6583`；SmartBench 全量 647 passed/6 既有失败；计划仍 1 次后过期 |
| 2026-10-18 | C11 #27 计划复验 | 第一轮 2 次更新、结束仍有 pending（收尾提醒未促成更新）；第二轮 3 次更新、全部 completed；说明计划收尾仍依赖模型配合 |
| 2026-10-18 | SmartBench PR #50 | 已推送 `feat/cli-progress-reporter`；PR open、mergeable、CI 7/7 绿；等待合并 |
