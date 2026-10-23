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
| C11 | ReAct 缺少 Codex 式显式 todo/plan 状态（`update_plan`），复杂任务只能靠提示词自律 | 执行/状态 | 🟡 收尾仍会过期 | 原生 schema + 周期/两次收尾提醒；#27 第三次 6/6 completed；但 #22 真实 126 工具任务实际完成代码/测试后，计划仍 4 项未更新，最终只能附警告 |
| C12 | 缺少多步任务“完成率”基准，容易凭感觉调提示词/循环 | 度量 | 🟡 v1/v2 完成 | v1、v2 均 5/5；v2 修正浮点断言后仍无区分度；已归档 v2 原始/修正数据；下一步真实长任务或 v3 |
| C13 | 写/实现任务连续 3 次工具失败会直接 mercy 收束（C4 真实复跑 77s、0 实现） | 执行/失败恢复 | ✅ 已修复 | 写任务改为最多 2 次“换方法”提示，不提前收束；查询任务保留 3 次收敛；真实 #26 从 15 次失败恢复完成 |
| C14 | DeepSeek 截断后输出 `<｜｜DSML｜｜ calls>`（带空格），旧解析器不认，整轮判格式错误 | 执行/协议 | ✅ 已修复 | 解析器接受 `calls/tool_calls` 及空白变体；`_structured_response_kind` 同步；单测覆盖 |
| C16 | C12 v1/v2 任务偏易，flash/pro 均满分，无法区分完成率 | 度量 | 🟡 真实长任务已有对照 | #26 两轮 217→147s、安全失败 1→0；#27 两轮均提交（331s/98 工具、478s/125 工具），第二轮暴露上下文截断；仍需更多 issue/PR 样本 |
| C17 | #165 简单查询/报告文档被当成技术文档，内容冗杂并触发无关验证说明 | 输出/上下文 | ✅ 已修复 | `write_report` + 报告模板/策略 + 文档写入不触发代码验证；多轮回放 `PROMPT_HISTORY=[]`，报告 742 字符无旧项目内容，最终回答 303 字符 |
| C18 | 工具完整结果只在内存，会话/压缩无法回查；全局 max_chars 只能截断 | 执行/存储 | 🟡 真实 save/resume 待验 | 缓存/`result_id`/结构化视图/回查/哈希锚定；native、`/compact`、自动归档、非 native in-run 均 0-LLM；#27 0 次上下文截断；真实 `/save→/resume` 未手测 |
| C19 | `/resume` 只能看到最近一次自动保存；启动新对话会覆盖旧 `_auto` | 会话/持久化 | ✅ 已修复 | `conversation_id` + `_auto` 轮转为 `auto-*.json`；旧会话成为普通可恢复会话；`/resume`/启动选择器显示 `[自动保存]`；单元测试覆盖轮转/同名不轮转/显示 |
| C20 | “实现 #22”这类具体 issue 交付被当成 `write_code` 仅回答/只读，且不主动克隆目标仓库 | 执行/权限 | ✅ 交付已合并，产品链路待复验 | `task_delivery` 提升 EXECUTE；write_code 含 clone 指引；PR [#51](https://github.com/xianyu-sheng/SmartBench/pull/51) 已合并、7/7 检查全绿、Issue #22 已关闭；TaskState 真实 #26 收尾已验证 |
| C21 | `继续` 只靠模型记忆，TaskState/交付要求跨回合丢失 | 执行/状态 | ✅ 统一引擎出口已验证 | `xenon/engine/task_runtime.py` 共享 `run_with_delivery`；BaseEngine 与组合引擎 SteeringMixin 都暴露 `run_task`；EngineHost 只调用这一个入口；真实 #26 72 工具/196s、8 文件、53 目标测试、commit `005ba58`、TaskState completed；全量 3453 passed；commit 门只认实际 commit 命令输出的哈希，忽略 git log 旧哈希 |
| C22 | 固定 120 步会误杀长链路任务；重试上限 2 次过早放弃 | 执行/循环 | ✅ 已改 | 默认 `0`=不限步数，由 LoopDetector、无进展检查点、1800s deadline 控制；`XENON_MAX_ITERATIONS` 可显式设上限；交付/验证/格式/计划收尾/写恢复重试统一为 3 次；Xenon 全量 3454 passed；`tests/test_react_loop_exit.py` 证明 max_iterations=0 下高置信度循环由 LoopDetector 终止 |
| C39 | run 内部才创建 ledger 时，交付层仍使用启动前的 None 引用，计划事件没有回放 | 事件/投影 | ✅ 已修复 | run 后刷新 `_evidence_ledger`；真实 calc 计划 5 步全部进入 TaskState；新增“运行中创建 ledger”回归测试 |
| C40 | 模型常写 `git add x && commit -m`（第二个段缺 git） | 执行/命令 | ✅ 窄兼容 | git 链中前一命令成功后，裸 `add/commit` 视为 git 子命令；不改变独立命令语义 |
| C42 | 空测试文件也能被 pytest 的旧测试通过洗白，TaskState 假完成 | 执行/交付 | ✅ 已修复 | ToolExecutor 记录 `content_chars`；TaskState 对“新增/补测试”的任务要求非空 test artifact；空文件进入 `invalid_test_artifacts` 和修复提示；真实 #26 空测试场景会被判未完成，非空测试场景通过 |
| C45 | Plan-Execute 的可用工具列表漏掉 `run_script`/`tool_result_read`，但安全错误仍要求模型改用 run_script | 工具/提示 | ✅ 已修复 | Planner prompt 与 mini-ReAct 工具说明补全；`command` 明确禁止 `python -c`/未加引号 `$`；run_script 执行后清理临时脚本；新增提示词/清理测试 |
| C46 | pytest 归一化把 `2>&1` 留成参数 `2`，并可能截断带引号的 `-k "a|b"` | 工具/命令 | ✅ 已修复 | quote-aware 管道处理；`2>&1` 被剥离；`-k` 内管道保留；新增 parser 测试 |
| C48 | 10 用例结果存在波动，不能把单轮 10/10 当成完成率承诺 | 度量/真实性 | 🟡 已记录 | 最新 9/10；失败样本为 commit 后源码未提交，交付门正确拦截；基准文档保留历史 10/10 与最新 9/10，不粉饰 |
| C47 | 先写有效测试再覆盖为空时，旧 `test_artifacts` 仍保留，空测试门失效 | 交付/测试 | ✅ 已修复 | 空内容写会移除同名 valid test artifact 并加入 invalid；新增回归覆盖 |
| C44 | 步骤修复连续失败仍使用同一执行模型 | 执行/模型 | ✅ 可配置轮换 | 首次修复保持当前模型；第二次修复轮换显式配置的 `executor_model_priority`；第三次且配置了 model_pool 时优先尝试更高 tier 模型；全量 3595 passed |
| C43 | “不要写死”被误判为“需要新增测试”，导致 cli_summary 假缺口 | 判定/交付 | ✅ 已修复 | 测试创建正则排除“写死”；cli_summary/json_report 复跑 2/2；最终 10/10；全量 3594 passed |
| C41 | 交付提示只知道 dirty，不告诉模型具体残留文件，模型反复空说“干净” | 执行/提示 | ✅ 已修复 | worktree 探测记录 `dirty_paths`；状态持久化；delivery continuation 直接列出 `.bak`/未提交路径，要求实际删除或提交；全量 3592 passed；10/10 基准 |
| C37 | `git add -A; git commit` 经 PowerShell 返回 129，造成无效重试 | 执行/命令 | ✅ 窄归一化 | `xenon/utils/git_command.py` 只拆分全由 `cd` + 白名单本地 git 子命令组成的链，直接 subprocess 执行并返回 committed_paths；真实 `git add -A; git commit` 集成测试通过 |
| C38 | 同一步骤连续相同失败会反复走同一修复策略 | 执行/修复 | ✅ 已加短路 | 修复轮检测到相同错误文本即停止同策略重试，交给结构性重规划/后续回合；单测覆盖只调用一次修复 |
| C36 | 计划只在引擎结束后渲染，执行中 UI 仍可能读 callback 状态 | 展示/事件 | ✅ 实时投影已接 | `run_with_delivery` 在 run 前订阅 ledger，TaskStateProjection 的 plan/step 变化发布 `plan_state` EventBus；REPL 订阅后按稳定签名输出执行行；旧版 PLAN 快照不能覆盖新版；全量 3585 passed |
| C34 | 交付 dirty 只依赖模型跑过 `git status`，模型可能漏跑；pytest 后 bytecode 也会制造假 dirty | 执行/交付 | ✅ 真实基准验证 | `run_with_delivery` 在显式 workspace 下直接探测 `git status --porcelain -uall`，忽略 pycache；clean probe 可覆盖“同内容重写导致 revision 过期”的假 dirty；`fix_bug` 基准 3/3；真实 #26 `missing=[]` |
| C35 | 缺少真实任务完成率基准 | 度量 | 🟡 最新 9/10 | `evals/execution_benchmark.py`：最新 9/10，`safe_div` 因 commit 后源码仍有未提交改动被判 active；此前一轮 10/10；覆盖创建、失败修复、JSON、多文件、重命名、双 bug、格式化、边界值、默认配置；真实 #26 同轮通过 |
| C32 | 计划步骤只是自由文本，重规划会替换旧计划，补救步骤无法进入最终计划 | 执行/计划 | ✅ 已结构化 | 新增 `plan_contract`：`kind/requires_write/verify_cmd/target_paths`；TaskState 保存 `plan_version/plan_history`；PLAN 事件携带版本，step started 可 upsert 缺失的补救步骤；计划面板从 TaskState 渲染 |
| C33 | 失败步骤直接重规划整盘，且 pytest 经 PowerShell 容易产生 no tests/退出码歧义 | 执行/修复 | ✅ 真实 #26 通过 | 步骤内 repair（最多 3 次，带失败输出）；写步骤必须新增写调用；verify 必须重跑同一命令；重规划丢写步骤会被拒绝；pytest 窄语法改为无 shell 执行；真实 #26 81 工具/17 失败/334s，目标测试 2 passed、commit、工作区干净、TaskState completed |
| C24 | Plan-Execute 真实回归仍会在空测试/未提交源码时误判完成 | 执行/计划 | 🟡 串行+DAG replanner 已加；same-tool 补救门已修；最小真实任务两次成功，复杂 #26 仍失败但不再假完成 | 2026-10-21 SmartBench #26 切片四轮：首轮 60 工具/18 失败/243s，仅提交空测试文件；第二轮 51/20/341s，只改 JSON；第三轮 64/15/292s，源码改动但测试不存在；最终代码再跑 73 工具/22 失败/383s，TaskState `active`、缺 verify，最终回答明确未完成。已修：计划同步进 TaskState、失败步骤保持未完成、提交快照要求非测试源码、git 多路径/`commit -m`/action list 解析、`search_files pattern` 别名。仍未解决：规划器不生成有效写步骤、测试内容为空，不能判定 Plan-Execute 可用 |
| C25 | `/compact` 后 TaskState 是否随会话恢复 | 执行/持久化 | ✅ 已验证 | `tests/test_compact_task_state_resume.py` 2 passed：compact 只改对话历史，不抹掉 TaskState；`_auto_save_session` → `load_session` 重建 `AgentContext` 后 objective/steps/artifacts/tests/commit/required 全部保留；测试-only commit 仍为 `active` |
| C28 | TaskState 仍依赖命令文本推断测试/交付 | 执行/证据 | ✅ 已结构化 | ToolCall 增加 metadata；ToolExecutor 传递 `changed_paths`/`committed_paths`/`exit_code`/`returncode`；git commit 执行 `git show --name-only HEAD`；TaskState 消费真实路径与 exit code，不再只靠输出文本 |
| C31 | 引擎层没有统一可订阅的事件源，TaskState/plan/UI 各自维护状态 | 架构/状态 | ✅ TaskState/plan 投影已接线，Engine EventBus 已暴露 | EvidenceLedger 是 append-only 哈希链；新增过滤订阅/replay/异常隔离；ToolExecutor 的 tool_observation 携带 structured metadata；EventBus 仅作为临时 UI 通道（Base/组合引擎已暴露，delegate 保持）；TaskStateProjection 已从 task_fact 边界 replay，处理 tool/plan/step 事件，按 result_id 幂等；真实 smoke 7 步全部来自事件投影 |
| C29 | 旧测试通过可能掩盖后续失败/源码改动，最终回答可假完成 | 执行/验收 | ✅ 已加最终门 | `record_tool` 用结构化 exit_code 判定测试；失败测试清空旧通过并记录 failed_tests；写入使 `verified_revision` 过期；commit 后写入触发 dirty；空 commit 的 `committed_paths=[]` 不回落旧 artifacts；`run_with_delivery` 回归验证失败测试强制重跑 |
| C27 | Windows 本地测试集有 42 个失败/错误（symlink 权限、POSIX mode、子进程 GBK、MCP pipe、锁释放竞态、live 网络阻塞） | 测试/跨平台 | ✅ 已清理 | 引入 `tests/platform_utils`、live 测试默认跳过、MCP Windows stdout pump、锁 unlink 重试、条件 DummyOutput；全量 `3516 passed, 98 skipped, 0 failed`；最新 CI 全绿 |
| C26 | CI 连续两次失败：LoopDetector 3 次哈希硬终止抢在纯只读补救前 | 执行/CI | ✅ 已修复 | 根因是 `read_only_streak=3` 时哈希循环先触发；现在只读补救优先、注入后重置循环窗口；`1115f7c` 已推送，最新 CI run 39560491106 全绿；本地 Windows 测试修正 `285b56b` 一并提交 |
| C23 | 可选 HMAC 证据链锚点（PR #160）存在 keyed→unkeyed 降级攻击 | 证据层/安全 | ✅ 已修复并合并 | 删除 `hk` 后整链用 sha256 重算，有 key 校验现在返回 `partial/unkeyed`（新增 `ChainReport.key_expected`）；回归覆盖降级形状；PR #160 以 `8d123a5` 修复合并为 `3d163d7`，7/7 CI 绿 |
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
| 2026-10-18 | 非 native 0-LLM + C11 | 非 native in-run 改结构化摘要；C11 周期提醒 + 两次收尾提醒 + 未完成警告；#27 第三次 125 工具/4 失败/358s/0 安全失败/3 次计划全 completed；SmartBench targeted 35 passed，全量 642 passed/7 既有环境失败；Xenon 3445 passed |
| 2026-10-19 | /resume 自动会话轮转 | `_auto` 按 `conversation_id` 归档为 `auto-*.json`；同名会话继续覆盖；`/resume` 列表显示 `[上次自动保存]`/`[自动保存]`；3 个新测试通过；Xenon 全量 3452 passed、无新增失败；新增测试防止旧 commit 假满足交付；EngineHost fallback 也走统一运行时 |
| 2026-10-19 | issue 交付任务路由 + #22 真实复验 | “实现#22”从 ANSWER_ONLY/只读提升为 EXECUTE；真实任务克隆 SmartBench、写 HTML 报告、`tests/test_html_report.py` 10 passed、相关 103 passed、ruff 通过；126 工具/11 失败/515s；计划仍 4 项未更新、未提交 |
| 2026-10-19 | TaskState + 交付契约 | 新增持久 TaskState、`继续` 恢复、ReAct 交付提醒；Xenon 全量 3450 passed、无新增失败；REPL 集成测试覆盖 `继续` 恢复执行级别/交付要求；真实长任务验收待跑 |
| 2026-10-19 | TaskState 真实 #26 | 干净 clone 调用 Xenon ReAct：72 工具/7 失败/196s；8 文件改动；53 targeted passed、全量 664 passed/7 既有 Windows 失败；commit `005ba58`；TaskState completed，无交付警告；未 push/PR |
| 2026-10-19 | 统一引擎交付出口 + commit 识别修复 | 抽出 `task_runtime.run_with_delivery`；BaseEngine + SteeringMixin 共用；EngineHost `_run_engine` 改调 `run_task`；不再由 ReAct 单独实现；全量 3452 passed、无新增失败；新增测试防止旧 commit 假满足交付；EngineHost fallback 也走统一运行时 |
| 2026-10-19 | 无限步数 + 3 次重试 | 默认步数上限改 0；墙钟默认 1800s；交付/verify/malformed/no-tool/plan/no-write 重试统一 3；全量 3454 passed、无新增失败 |
| 2026-10-20 | unlimited 真实 #27 | `max_iterations=0`；112 工具/327s；正常完成，非步数退出；TaskState completed；commit `0cdf950`（临时 clone，未 push）；首轮 99 工具时出现一次未复现 TypeError，第二轮干净 |
| 2026-10-20 | 工具数值参数 + 循环退出 | `d73d385` 统一强转模型传入的数值参数，`ad9c8e6` 增加 unlimited 下 LoopDetector 终止测试；targeted 23 passed |
| 2026-10-21 | Plan-Execute 真实 #26 切片 | 三轮真实 DeepSeek 运行均未完成：首轮 60 工具/18 失败/243s、空测试提交；第二轮 51/20/341s、只改 JSON；修正后第三轮 64/15/292s，TaskState 保持 active、缺 verify；提交快照门重放显示空测试提交还缺 changes。已加计划状态同步、提交快照源码门、失败测试不计验证、git 参数规范化；Plan-Execute 仍不可宣称完成 |
| 2026-10-21 | compact 后 TaskState 恢复 | 新增 `test_compact_task_state_resume.py`：compact → auto-save → load 后状态字段完整；2 passed |
| 2026-10-21 | PR #160 HMAC 降级修复 | 复现 keyed→unkeyed 重算链；`8d123a5` 增加 key_expected/partial 与回归；PR #160 合并 `3d163d7`；evidence targeted 47 passed，CI 7/7 |
| 2026-10-22 | Plan step 写操作强校验 | 计划步骤任务要求写盘（tool 或 write/edit/插入/补齐语义）时，执行后必须新增成功写工具调用；串行、DAG 串行/并行、补救步骤均覆盖。修复真实 #26 中“声称修改但 git diff 为空”的步骤 status；全量 3565 passed/98 skipped |
| 2026-10-22 | Combined Engine EventBus | `PlanReactEngine`/`PlanReflectionEngine`/`ReactReflectionEngine` 也暴露顶层 `event_bus`，回调 delegate 保持；新增组合引擎测试；全量 3563 passed/98 skipped |
| 2026-10-22 | 失败输出进入修复续跑 | ToolExecutor 记录 stdout/stderr tail；TaskState 保存 verification_failures（命令/exit_code/输出尾部）；`run_with_delivery` 在 verify 缺失时改用“先修复真实错误、重跑同一命令”的 continuation，而不是泛化重试；全量 3567 passed/98 skipped |
| 2026-10-23 | 10 用例复跑 | 最新 9/10；safe_div 测试/commit 成功但源码 dirty，TaskState active；交付门正确拒绝假完成 |
| 2026-10-23 | 工具设计审查 | 全量 3597 passed/98 skipped；plan prompt 暴露 run_script；run_script 清理临时脚本；pytest 引号/重定向修复；空测试重写失效 |
| 2026-10-23 | 修复模型轮换 | 全量 3595 passed/98 skipped；配置多执行模型时第二次修复开始轮换；单模型默认行为不变 |
| 2026-10-23 | 10 用例最终基线 | 全量 3594 passed/98 skipped；10/10 本地真实任务；空测试门、dirty 路径、实时计划同轮验证 |
| 2026-10-23 | 空测试门 + 真实 #26 复跑 | 全量 3593 passed/98 skipped；真实 #26 90 工具/16 失败/634s，非空回归测试、目标测试通过、commit、clean、TaskState completed；10/10 基准 |
| 2026-10-23 | 10 用例基准 | 全量 3592 passed/98 skipped；10/10 真实本地任务通过；真实 #26 同轮通过；dirty 路径进入修复提示 |
| 2026-10-23 | 实时计划回放 + 5 用例基准 | 全量 3591 passed/98 skipped；calc/fix_bug/cli_summary/json_report/multi_file 5/5；TaskState 计划步骤完整；真实 #26 已通过 |
| 2026-10-22 | 实时计划 + git 归一化 | 全量 3589 passed/98 skipped；plan_state EventBus 实时订阅；git add/commit 链无 shell 执行；同错误修复短路 |
| 2026-10-22 | 执行基准 + 真实 worktree 探测 | 全量 3580 passed/98 skipped；`calc/fix_bug/cli_summary` 3/3；真实 #26 目标测试 2 passed、commit、clean、TaskState completed |
| 2026-10-22 | 步骤契约 + 计划版本 + 原地修复 | 全量 3579 passed/98 skipped；真实 #26 目标测试 2 passed；7 个计划步骤全部 completed；TaskState `missing=[]`；源码/测试/commit 均已落地 |
| 2026-10-22 | 失败反馈真实 #26 复跑 | 60 工具/15 失败/278.3s；tests 为空、verification_failures 含 TypeError/assert 输出；步骤 3/4/5/7 failed、8 skipped，TaskState active；最终回答明确未完成，未再假 complete |
| 2026-10-22 | 写操作门真实 #26 复跑 | 63 工具/18 失败/307.4s；真实 commit `48b0e02`，但回归测试仍失败且工作区有 `.bak`；TaskState `active`，缺 verify/dirty；steps 8/9 completed、step 10 经 reconciliation 改为 failed；最终回答明确未完成 |
| 2026-10-22 | 计划步骤写操作强校验 | 写意图步骤（tool=write 或任务含写/插入/补齐语义）必须新增成功写调用；串行/DAG 串行并行/补救步骤均覆盖；新增步骤 delivery reconciliation；全量 3566 passed/98 skipped |
| 2026-10-22 | EventBus 多订阅接线 | `BaseEngine.event_bus` + delegate-preserving `CallbackBusBridge`；UI/日志可独立订阅，原 Console/Silent 回调行为保持；新增桥接/引擎测试；全量 3562 passed/98 skipped |
| 2026-10-22 | 会话恢复事件源 | auto-save/`/save` 持久化 EvidenceLedger snapshot；`/load`/`/resume` 校验并重绑；TaskStateProjection 在恢复后可继续 replay；`tests/test_evidence_resume.py` 覆盖；全量 3559 passed/98 skipped |
| 2026-10-22 | plan/step 事件投影真实 smoke | `D:/tmp/xenon-planexec-smoke`：7 工具/0 失败/8.7s；PLAN + 7 对 STEP_STARTED/FINISHED 事件恢复 7 步 completed；真实 pytest 2 passed、commit facc42a、TaskState completed；全量 3558 passed/98 skipped |
| 2026-10-22 | 事件投影真实 smoke | `D:/tmp/xenon-planexec-smoke`：24 工具/6 失败/101.7s；TaskStateProjection replay 24 个 result_id；真实 pytest 2 passed、commit 875a23a、status completed；全量 3556 passed/98 skipped |
| 2026-10-22 | 引擎层事件源基础 | `EvidenceSubscription` 支持 phase/kind/source 过滤、replay、异常隔离；Tool observation 事件携带 exit_code/changed_paths/committed_paths；新增 `docs/EVENT_SOURCE.md`；全量 3550 passed/98 skipped |
| 2026-10-22 | Plan-Execute 步骤状态按 id 同步 | TaskState steps 保存真实 step id；`_sync_task_results` 不再按列表下标贴结果；重规划后 id=10/20 也能正确标记 completed/failed；旧状态保留无 id 的下标回退；全量 3545 passed/98 skipped |
| 2026-10-21 | Plan-Execute 假步骤成功修复 | 根因：工具步骤 `pytest` 失败后，补救轮只要新增任意成功工具调用（如 read_file）就判定步骤成功；改为原工具必须新增成功调用。`test_plan_structured_failures` 覆盖；全量 3544 passed/98 skipped |
| 2026-10-21 | 多 commit 源码交付 | `committed_source_paths` + `source_commit_tracking`：源码提交后再提交测试/清理文件不丢源码交付；源码重写会移出已提交集合；全量 3542 passed/98 skipped |
| 2026-10-21 | final gate 驱动真实修复 | 同一 smoke reset 后复跑：28 工具/3 失败/211s（含 API ReadTimeout 重试）；dirty gate 连续 3 次要求清理，最终 `git status` 空、commit `fe6300a` 含 `calc.py`/`tests/test_calc.py`、`commit_paths_known=true`，测试 2 passed；TaskState completed |
| 2026-10-21 | Plan-Execute 最小真实闭环成功 | `D:/tmp/xenon-planexec-smoke`：67→7 工具、0 失败、8.2s；`write_file` 建 `calc.py`/`tests/test_calc.py`，真实 `2 passed`，`git commit 09293c7`；TaskState completed，artifacts/verified/tests/commit 一致；证明执行层在简单任务上能闭合 |
| 2026-10-21 | Plan-Execute #26 最新复跑 | `D:/tmp/xenon-planexec-26` reset 到 `02d9eb9`，67 工具/22 失败/346.6s；提交 `a1e9132` 后仍有一次源码 edit，专项测试 2 failed；TaskState `active`、artifacts_revision 10 > verified_revision 9、missing verify/dirty；最终回答明确“任务交付未完成”，最终门未假完成；Plan-Execute 本身仍不可用 |
| 2026-10-21 | command commit 路径结构化 | `tool_node._exec_command` 在 `git commit` 成功后执行 `git show --name-only HEAD`，把真实 committed_paths 交给 ToolCall/TaskState；新增测试；全量 3540 passed/98 skipped |
| 2026-10-21 | DAG 失败重规划 | `run()` 传入 `replan_on_failure`；失败波次在旧依赖变 skipped 前重规划，保留成功结果、清理失败/跳过行、重建 DAG；直接 `_run_dag` 默认仍保持原 skip-cascade 语义；新增测试 64 passed |
| 2026-10-21 | final_answer_checks | TaskState 增加 artifacts/verified/committed revision、failed_tests、commit_paths_known；结构化 exit_code 与失败测试失效门；`tests/test_task_state_final_checks.py` 验证运行时重试；全量 3538 passed/98 skipped |
| 2026-10-21 | 结构化 commit metadata | `3f185b9`；Git commit 结果携带真实 committed_paths，ToolCall/TaskState 消费；targeted 56 passed |
| 2026-10-21 | Windows 测试集清理 | 42 个失败/错误逐项处理：MCP Windows pipe 读取、锁文件 unlink 竞态、POSIX mode/symlink 断言、编码、live 网络测试 opt-in；全量 3516 passed/98 skipped，CI 39583834735 全绿 |
| 2026-10-21 | CI 失败修复 | 两次 CI 均因 `TestReadOnlyParalysisDetection::test_injects_write_prompt_after_4_read_only_rounds` 失败；修复 `1115f7c`，最新 CI 全绿；提交 Windows-safe 测试修正 `285b56b`；丢弃 `repl.py` 的 DummyOutput 坏改动 |
| 2026-10-21 | Plan-Execute 最终真实复跑 | 73 工具/22 失败/383s；源码有真实 edit，但专项测试不存在、无新 commit；TaskState 保持 active，最终回答警告“未完成”；未再假完成 |
| 2026-10-21 | AgentResult + Plan-Execute replanner | 新增 `AgentResult`/`TerminationReason`、串行失败步骤重规划（`max_replans=3`）、DAG 依赖 skip 保持、LoopDetector 3 次哈希/三周期模式硬终止修正 + 参数签名；targeted 109 passed；真实 #26 仍需复跑 |
| 2026-10-20 | PR #160 审查 | 发现可选 HMAC 证据链的无密钥降级攻击；已留可复现评论，暂不合并 |
| 2026-10-19 | SmartBench PR #51 合并 | PR #51 合并提交 `02d9eb9`，7/7 checks 绿；Issue #22 关闭；本地 main 已拉齐，分支已清理 |
| 2026-10-18 | C11 #27 计划复验 | 第一轮 2 次更新、结束仍有 pending（收尾提醒未促成更新）；第二轮 3 次更新、全部 completed；说明计划收尾仍依赖模型配合 |
| 2026-10-18 | SmartBench PR #50 | 已推送 `feat/cli-progress-reporter`；PR open、mergeable、CI 7/7 绿；等待合并 |
