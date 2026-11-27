# 更新日志

本文件记录 Xenon 各版本变更。版本号遵循语义化版本。

## [Unreleased]

### UX：执行过程默认只显示「结论性事件」，记账事件折叠（#441）

一次真实任务（D 盘扫 .exe，111 条事件）里，`/dashboard` 的「执行过程」表把每条事件都
列成一行，其中一半是 `state_transition`（running→thinking→running…）和 token 用量这类
纯记账事件——账本需要它们，读的人不需要。这不是跨轮次的泄漏，而是单轮内没有投影：

- `DashboardQueries.ledger_events(fold=True)`：把连续的记账事件折成一行
  `⋯ N 条记账事件（state_transition, …）`，并给出 `total / shown / folded`；
  折叠集合是 `state_transition / model_usage / thought / session_context /
  workspace_probed / workspace_layout / intake_recorded / step_verified / step_closed`；
- `step_verdict` **只折 satisfied**：验证失败的裁决正是看这张表的原因，必须留着；
- 面板默认折叠，显示「本轮 111 条事件 · 显示 35 行 · 已折叠 76 条记账事件」，
  一键「显示全部事件」；账本一条不少（`fold=False` 原样返回）。

实测同一份 76 事件的真实账本：62 → 35 行。

### 修复：`xenon_evidence` 返回体不说清 scope（#441）

`xenon_evidence` 直接把 `build_report()` 的整份 JSON 吐回去，外部 harness（
Claude Code 等）拿到后容易当成「整个会话」的证据：既觉得信息太多，又以为
其它轮次丢了。现在返回体第一个字段是 `scope`：

```json
{"scope": {"kind": "task", "task_id": "task-…", "events": 32, "receipts": 4,
           "note": "One ledger = one task (one turn). …"}}
```

一份 ledger 就是一轮任务（`run_task` 每次新 mint task_id），计数让这句话可核对。

### 修复：`test_cached_startup_is_fast` 在 Windows 上除零（测试）

`time.time()` 在 Windows 上分辨率约 15.6ms，两次读可能落在同一 tick，热启动耗时
被量成 `0.0`，`cold / warm` 直接 `ZeroDivisionError`。改用 `time.perf_counter()`；
并规定低于计时器分辨率的耗时不再做除法（那不是「更快」，是量不出来）。

### 修复：MCP 的 SSE 传输没有 `endpoint` 发现，也没有 id 关联（#427 / #103）

现象：`/mcp add web http://host/sse` 之后，握手成功但工具列表为空或调用串台；
换一个 SSE server 就完全连不上。文档一直写着「简化实现」，实际是**没走协议**。

根因：旧 `SSETransport` 把请求 `POST` 到 SSE 地址本身，每次调用现开一条 `GET`
流、读到第一条 `data:` 就当答案返回。于是：没有 `event: endpoint` 解析（不知道该
POST 到哪）、没有 JSON-RPC `id` 关联（两个并发调用拿到的响应可能互换）、遇到
`202 Accepted` 的服务器直接读不到结果、断线后要么悬挂要么静默丢帧。

修复（`xenon/mcp/transport.py` 重写 `SSETransport`）：

- 保持一条 `GET` 流，解析 `event: endpoint` 得到会话专属 POST 地址，并回带
  服务器下发的 `mcp-session-id`；
- 每条请求按 JSON-RPC `id` 注册待回表，响应在流上按 id 关联后唤醒对应调用
  —— 并发不串台，迟到的旧响应找不到主就丢弃；
- `202 Accepted`（答案走流）和「内联 JSON 响应」两种服务器都认；
- 流断开时**立刻失败所有在飞请求**（"侧效应未知，不自动重试"），并按上限重连
  重新握手，而不是等超时；
- SSE `id:` 去重（重连重放不会二次 resolve）；
- 新增 `status()` / `MCPClient.transport_status()`，`/mcp list` 如实显示
  `sse: 已握手 endpoint=…` 或 `未连接（原因）`、重连次数、丢弃的重复事件数。

测试：`tests/test_mcp_sse_transport.py` 用真实 HTTP server 覆盖握手、endpoint
发现、乱序并发、缺 endpoint 的 fail-closed、断线失败、重连恢复、重复事件去重，
以及 `/mcp list` 的如实显示（7 条）。

### 修复：search 的命中结果不进回执，模型只能回答「执行成功」（#432）

现象：`xenon exec "用 search 工具查找 mcpServers" --permission read-only` 结束后，
账本里只有 `{"tool": "search", "status": "ok", "effects": []}`，模型说不出命中
多少条、在哪些文件，只好转用 `shell`（grep/find），而只读边界会否决 shell，
任务就此打转。

根因是三层同时丢数据：`search` / `search_files` 的 `ToolSpec.effects` 是空集、
`gateway._effects` 没有搜索分支（`matches` 只写给了前端面板）、
`build_model_observation` 也不认识 `search`（只认 legacy 的 `search_files`），
于是到模型的只有一段没有总数的 `file:line` 文本。

修复（契约 → 回执 → 视图一次打通）：

- `EffectKind.SEARCH` + `SearchFact(query, path, matches, count, truncated, scanned, note)`；
- `gateway._effects` 按 `Capability.SEARCH`（结构信号，不是工具名枚举）构造有界命中
  （最多 50 条、单条 300 字符），`count` 保留工具报告的全量命中数；没有 pattern 时的
  目录列举同样落成一条 `SearchFact`，并在 `note` 里说明「这是列举，不是搜索」；
- ledger 的 effect payload 双向序列化补齐 `search`；
- 模型视图 `_effect_summary` 透出 `query` / `scanned`（`matches` / `count` / `truncated` 原本已有通道）；
- 模型观察改为 `[search] matches=N returned=M scanned=S truncated=?` 头部 + 命中行，
  并把「扫描达到文件/时间上限」「跳过超限文件」写进正文，而不是静默截断。

验证：只读边界下用真实 workspace 跑 `xenon exec --plan`，账本回执为
`effects: [{"type":"search","count":2,"matches":[...],"scanned":3}]`，模型可见
工作集里能看到 `count` 与两个命中文件；新增网关用例 4 条、模型视图 1 条、模型观察
2 条（撤回修复后全部失败），离线全量 4232 passed。

## [1.1.1] — 2026-11-16

CI 修复版。v1.1.0 的推送在 3.10/3.12 矩阵上超时，根因不在测试本身，而在
`extract_execution_signals` 的写入正则退化成平方级；另外把 README 与个人主页
的两张 Dashboard 截图改成等高显示（同一比例重新截取）。功能行为与 v1.1.0 一致。

### 修复：`_WRITE` 正则在大段重复文本上退化成 O(n²)（CI 超时）

现象：CI 的 `test_repetitive_flood_not_hard[x-100k]` 在 Python 3.10 / 3.12 上
超过 120s 超时（3.11 侥幸通过），全量套件因此不绿。本地复现：100k 个 `x` 走一遍
`extract_execution_signals` 需要 94.8s，cProfile 指向 `_WRITE`。

根因：分支 `(?<!…)\w+\.[A-Za-z0-9]+` 没有左边界，`finditer` 会从**每个**位置开始
吃掉整段 `\w+` 再回溯找点；在 100k 长的单词串上就是 10¹⁰ 次回溯。方向判定点
（`\w` 不出现在文件名中间）本该由边界表达，而不是交给回溯器试出来。

修复：两处文件命名分支都加上负向后顾——
`(?<!\w)\w+\.[A-Za-z0-9]+` 与 `(?<![\w./\\-])[\w./\\-]+\.[A-Za-z0-9]{1,10}`，
扫描恢复线性。实测 0.2s（signals）/ 0.54s（estimate），与长度成正比。

测试：`tests/test_input_boundary_validation.py` 新增
`test_a_long_word_run_does_not_backtrack_quadratically`（10s 上限守住「不许再
退化」）；相关 183 条用例通过，ruff 干净。

### 修复：README / Profile 的两张截图不等高（2026-11-16）

README 里 `dashboard-grid.png`（1600×1100，`width=900`）与 `dashboard-tabs.png`
（原 1440×900，`width=640`）并排时高度差出一截，看起来像布局 bug。把标签页截图
改按网格图同一比例截取（`capture_dashboard.py` 改为 `1024×704`，仍在 1400px
断点以下），两处 README 都用 `width="900"`，个人主页用 `width="820"`，等高。

## [1.1.0] — 2026-11-16

本版把交互入口换成 TypeScript/Ink 前端并新增本地 Dashboard，自进化收敛到
`LearningGateway`；同时在「用户约束 → 执行策略 → 验证层 → 工具边界」与测试
隔离上做了系统性修复：点名的读不再被改判成闲聊，只读轮不再为被禁写的文件
开债务，交付后的改动仍能在面板 Diff 里看到。真实组件矩阵
（`docs/evidence/component-matrix/`，真实前端 + ConPTY +
`deepseek/deepseek-v4-flash`）**54/54 PASS**；后端 4231 passed / 100 skipped，
前端 tsc 干净、102 tests passed，ruff 干净。面板截图由
`capture_dashboard.py` 从活页面截取（`docs/evidence/component-matrix/img/`）。

### 修复：点名的读被分类器改判成闲聊，凭记忆作答（2026-11-16）

真实前端复测：输入「读 README.md 和 calc.py，告诉我 add 函数有什么 bug，
先不要改任何文件。」，LLM 分类器给出 `chat_only`，合并层按规则 4 清空了全部
操作 → 本轮 `ANSWER_ONLY` → 走直接回答，模型回「我没有文件读取工具可用」
（另一次直接报「模型违反仅回答约束」），而 `README.md` / `calc.py` 就在工作区。

根因是「用户的结构化读请求」在合并层没有下限：`chat_only` 可以撤销分类器
自己发明的写/执行，也能顺带撤销用户点名的读。

修复（跨两层）：

- `ExecutionSignals.read_targets`：读动词（读/查看/打开/read/inspect…）在 16
  字符内统辖一个具体路径时置位。只提到文件名不算——「pyproject.toml 通常有
  哪些配置段？」仍是闲聊，不许被拉进工具循环；
- 合并层规则 4：`read_targets` 为真时只保留 `_READ_OPERATIONS`，`chat_only`
  仍然不能把凭空提议的写变成权限。

测试：`tests/test_turn_contract.py` 新增 3 条（chat_only 不能抹掉点名读、
仍能压掉同句里的凭空写、只提路径不算读请求）。复测截图见
`docs/evidence/component-matrix/img/dashboard-grid.png`：第一轮 4 次 `read`
回执、答案引用文件原文、证据角标 `partial`（它明确说没跑 pytest）。

### 修复：只读轮替被禁写的文件开债务（2026-11-16）

「先不要改任何文件」的回合仍会从任务文本推导出 `file_changed(calc.py)`，
内核于是为「遵守指令」开出无法清偿的债务。修复把写入范围送进验证层：
`TaskSpec.write_scope`（`xenon/kernel/contracts.py` 新增 `WriteScope`）由
`runtime_adapter` 从本轮 `ExecutionPolicy` 构造，`harness/intake.py` 的
`filter_obligations_for_scope` 在推导后剔除被禁止的写义务；读/测试义务保留。
`INTAKE_RECORDED` 事件记录 `write_scope`，账本可回放这一判断。

### 修复：已交付（worktree 干净）的改动在 Diff 面板消失（2026-11-16）

交付 commit 之后工作区是干净的，`git diff` 为空，Dashboard 的 Diff 面板因此
显示「0 个文件」——恰好把最该看的已验证改动藏了起来。`queries.diff()` 现在在
`verified/attributed` 路径未被工作区覆盖时回退读交付 commit 的
`--name-status` 与补丁，并标注 `origin`。回归用例
`tests/frontend/test_dashboard_queries.py::test_diff_falls_back_to_the_delivery_commit_when_the_worktree_is_clean`
用真实 git 仓库断言。

### 修复：测试隔离泄漏污染操作员全局入口（2026-11-16）

全量回归里 `tests/test_project_context.py::test_load_rules` 失败，根因不在
`ProjectContext`：`MemoryBackendRegistry` 只给测试隔离了 `user_data_root`
（记忆存储），`user_config_root` 仍落到真实的 `~/.config/xenon`。每个写入
USER 作用域的用例都往操作员的 `XENON.md` 追加一个
`xenon-memory-index` 块；积到十几个后，3,000 字符的 rules 预算被索引块占满，
项目 `.xenon/rules.md` 被截断，测试看不到「Python 3.12」。

修复：显式 `user_data_root`（测试/嵌入式宿主）现在把 config 入口放在数据根
旁边（`Path(user_data_root).resolve().parent`），不再触碰真实配置目录；
仍可用 `user_config_root` 显式覆盖。回归用例
`tests/test_home_boundary.py::test_explicit_data_root_keeps_the_config_entrypoint_isolated`
断言隔离后写入落在临时目录、真实 `XDG_CONFIG_HOME` 不被创建。

已清理操作员 `XENON.md` 中 12 个 `pytest-of-Administrator` 临时索引块，
保留合法的用户记忆索引。

### 修复：用户约束被读成写意图／全局禁令（2026-11-16）

真实前端矩阵复测暴露两个同源缺陷，都出在「用户约束 → 执行策略 → 工具边界」
这条数据流上：

- `创建 plan_out.txt，内容为 X；不要修改其他文件。` 被判成 `no_write=True`：
  否定词所在的从句里有「文件」，命中全局禁令词表。结果是最常见的一类受限
  任务整轮只读，写被否决，`/plan on` 也永远进不了 plan-only。
- `不要修改 plan_probe.txt` 里的「修改」又被 `_WRITE` 当成写意图
  （`write_verb`），单句约束反过来授权写盘。

修复把「约束」与「意图」拆开（`xenon/repl/execution_policy.py`）：

- 相对量词（其他/其它/别的/其余/remaining/other）先于全局词判定，产生
  `no_write_others` + `no_write_allowed_paths`（按整段请求取路径，不受
  5 条提示上限截断）；
- 扫描写意图前先按 `_NO_WRITE` 匹配区间把被否定的动词抹掉（`_strip_negated_mutations`），
  约束不再变成请求；
- 英文 `don't touch other files` 进入同一套相对否定。

约束随后沿同一条链路到达执行点：`ExecutionSignals` → `TurnContract` →
`ExecutionPolicy` → `store_write_scope()` 同时写入交互 REPL（`turn_flow`）
与协议宿主（`host.run_turn`）的上下文；`tool_classify.execution_policy_denial`
是唯一执行点——点名路径一律拒绝；相对否定下只允许本轮点名的路径；请求没有
点名任何路径时不做“其余全禁”，避免又退化成全局禁令。

测试：`tests/test_execution_policy.py` 新增 9 条回归（三种相对否定句式、
只拒绝未点名目标、无名目标不变成全局禁令、scope 发布）；全量见下方矩阵。

**真实组件矩阵**（`docs/evidence/component-matrix/`，真实 TS 前端 + ConPTY +
`deepseek/deepseek-v4-flash`，9 个场景）：**54/54 PASS**，合计 213.8s /
88,695 tokens。其中 `write` 在修复前曾用 637s 才完成（读成只读后反复被
策略否决），修复后 23.2s；`plan` 场景改为真实语义——计划回合只读落计划
（0 次写审批、未写盘），`/plan status` 显示待确认，`/plan approve` 续跑并
写入 `plan_out.txt`，账本出现 `plan_proposed/plan_accepted/plan_approved`。

### 重构：自进化唯一入口 `LearningGateway` + 审查修正（2026-11-16）

自进化的学习链路原先散在 `runner._apply_lessons` / `runner._learn_from_ledger`、
`LessonStore` 和 kernel proposer 三处，`run_task` 与 `resume_task` 各自复制
一遍调用。现在任务只对 `xenon/harness/learning.py` 的 `LearningGateway`
说话：

- `recall(available, limit)`：按 catalog subject 做确定性相关性打分，最多 3 条；
- `apply(spec, ledger, runtime)`：追加 `[lesson <id>]` 约束、写
  `LESSON_APPLIED`；同一账本内已应用过的教训不再重复计数（plan + resume 是
  一个用户任务）；
- `commit(ledger)`：存储提案、复发计数、聚类、确认；收尾阶段的所有账本写入
  都做失败隔离。

分类仍是 `LessonShape`，聚类仍按结构族，打分仍是"subject 在 catalog 中"，
召回仍读存储；LLM 不参与任何学习判断。kernel 只提案、`LessonStore` 只落盘、
门面只编排，分层没有上移。

**代码审查同时修正的确定性缺陷**（全部有回归用例）：

| 问题 | 修正 |
|---|---|
| `loop_detected` 事件写 `kind`，规范载荷只读 `signal`，同一失败两个指纹，循环教训永不复发 | 规范载荷回退读 `kind` |
| 教训文案写 `capability ?` / `循环信号 loop`，与指纹实际匹配的 subject 不一致 | proposer 的措辞也从规范载荷取 subject/reason |
| plan + resume 重复 apply/commit；复发扫描把 plan 阶段失败再数一次 | 单入口 + 已应用去重 + 只数最近一次应用之后的事件 |
| 读取合并 USER/PROJECT_SHARED/SESSION，可能跨项目泄漏 | 读写都限定 PROJECT_LOCAL；坏 metadata 只跳过该 scope |
| 30 天 stale 只存在于无人调用的 `match()` | `match_for_task` 同样执行 stale 截止 |
| 进程内缓存过期 + 应用不检查 retired，归档教训被复活 | 每次读后端；`record_application` 拒绝已归档 |
| 应用教训时从默认值重建记录，`/memory pin`、expiry、检索计数被覆盖 | 只改教训拥有的字段；`counterfactual` 随 `cf:` 标签持久化 |
| 退役规则的成员教训重新逐条出现 | 退役规则同样抑制成员 |
| 聚类 `source_task` 依赖后端插入顺序 | 成员按 id 排序 |
| 规则状态可因外部计数重置而回退 | 状态只前进 |
| `claim_fact_mismatch` 提案无 proposer 分支，静默丢弃 | 删除死调用；`DEBT_OPENED` 才是教训来源 |
| 内部 `TypeError` 被误读为旧 store，绕过相关性过滤 | `inspect.signature` 判断 `available` 参数 |
| plan+resume 的应用计数去重集合在写事件后才算，计数全被跳过 | 先读账本去重，再写事件与计数 |

测试：新增 `tests/test_learning_gateway.py`（门面契约、禁用/损坏 store、
评审回归）、`tests/test_lessons.py` 增加复发窗口、stale、scope、归档、pin、
退役规则抑制等用例；`tests/harness/test_llm.py` 增加 resume 携带教训用例；
`tests/kernel/test_lesson_proposer.py` 增加 `scope`/`kind` 真实事件载荷用例。

**真实埋坑回放**（`docs/evidence/self-evolution/tty_lesson_pitfall_replay.py`，
真实 ConPTY + TS 前端 + `deepseek/deepseek-v4-flash`，三个独立进程）：项目 A
拒绝 shell 审批 → 杀进程 → 新进程执行"列文件 + 读 README"埋坑任务，3 条
教训从磁盘加载并全部进入 `TASK_STARTED.constraints`（`lsn-3ffd045bb4383d75`、
`lsn-5edf0e60348b279f`、`lsn-6c351d7d4c44ec49`），`需要授权` 0 次，回答来自
`read` 与工作区布局探测；项目 B 的阴性对照无教训、无约束且任务照常完成。
7 项断言全过；账本 `repl-7486403ef783` / `repl-38f5d2d51f55` /
`repl-1cc3ce9dbf45`。全量：4214 passed, 100 skipped。

### 修复：协议/终端边界吞掉方括号标识（2026-11-16）

真实 ConPTY 验收 `/memory lessons` 时发现：输出写着「共 3 条教训」，但每条
教训都**没有 id**。

- **根因**：命令边界用 rich 渲染返回文本，而 rich 把任意 `[word]` 当样式
  标签，遇到不认识的标签（`[lsn-3ffd…]`）**静默丢弃**。受影响的不仅是教训
  id，还有 `[fact-…]`、`[claim-…]` 和 Markdown 链接。
- **修复**（`xenon/repl/rich_markup.py`，交互 REPL 与协议 host 共用）：只把
  rich 真正拥有的语法当标签——`Style.parse` 可解析、`DEFAULT_TOKEN_STYLES`
  里的语义色、`[/…]`、`[link=…]`、`[@…]`；其余转义为字面量。交互 REPL 的
  命令面板（`xenon/repl/repl.py`）与协议边界（`xenon/frontend/host.py`）
  同一次修复；没有 rich 时不再删除文本，原样返回。
- **回归**：`tests/test_rich_markup.py` 与 `tests/test_protocol_host.py`
  覆盖「字面 id / Markdown 链接保留，真实标签照常剥离」。

### 优化：会话稳定工具契约 + miss 计费（L4，2026-11-16）

L3 结论之后仍能看到两类浪费：**工具块在任务边界被重写**、**miss token
没有进账本**。按同一条数据流（选择 → 注入 → 计费 → 观察）一次性修掉。

- **会话稳定工具契约**（`xenon/repl/session_tools.py`）：MCP 工具的
  `extra_specs` 以前按任务重选，新任务第一调用只能命中 system + 工具块前段。
  现在每个会话维护一个**只追加、首次出现顺序**的并集（上限 32，淘汰最久
  未选且不是本任务刚选中的工具），同一工具在会话内保持它第一次被展示的
  schema；执行时展开过的 schema 通过 `expanded_tools` 在后续任务继续展开，
  不再退回索引行。
- **契约只在有证据时增长**（`xenon/catalog/specs.py`）：`select_mcp_tool_specs
  (..., relevant_only=True)` 要求目标里真的出现工具名/名称词干（阈值 8 分）；
  一个偶然共享的描述词（2 分）不再算证据，无证据时返回空集而不是用目录顺序
  填满预算。逐任务 prompt 仍保留补位（它是每次重建的）。
- **证据只取用户输入**（`xenon/repl/runtime_adapter.py`）：`mcp_extra_specs()`
  此前用完整 objective 打分，而前端第 2 轮起 objective 是**冻结请求信封**
  （常驻上下文 + 学到的教训 + 本轮请求）。信封里的
  `{"session_created_files": [...]}` 这类记账 JSON 键不是用户词汇，却让
  `github:push_files` 等 13 个 PR 工具过了阈值，「运行 python -c print(11*12)」
  的工具块从 2581 涨到 3228 token。现在相关性一律用
  `user_objective`（用户原话）；信封仍是模型读到的内容，但不再当作任务需求
  的证据。
- **恢复路径同一条链**（`xenon/repl/runtime_adapter.py`、`xenon/harness/runner.py`）：
  `resume` 以前不传 `extra_specs`，已批准计划里点名真实 MCP 工具的步骤无法
  绑定；现在 `run` 与 `resume` 共用一个 `mcp_extra_specs()`，恢复的任务也
  携带会话契约与已展开 schema。
- **miss 计费**（`xenon/utils/llm_usage.py`、`xenon/utils/deepseek_cache.py`、
  `xenon/kernel/usage.py`）：DeepSeek 只回 `prompt_cache_hit_tokens`，miss 必须
  由 `prompt − hit` 推出。此前 `/cost`、`/cache` 与账本都用「存在嵌套 cache
  字段」判断，网关只回命中份额时 miss 恒为 0；现在三处统一按「响应里出现
  任一 cache 字段」推导，账本 `model_usage` 带 `cache_miss_tokens`。
- **turn 内按生命周期排序**：任务稳定层（brief / earlier）在前，阶段协议
  其次，只有事件是每次调用新增的，finisher 复用 planner 已付费的字节。

真实入口（`xenon.exe` ConPTY，直连 `deepseek/deepseek-v4-flash`）两轮实测，
第 2 轮使用**同一任务文本**（`用 shell 运行 python -c "print(11*12)"`）：

| 观测 | 改动前 | 改动后 |
|---|---|---|
| 第 1 轮 `tools_tokens`（目标不含 MCP 工具） | 3371 | **2581**（不再追加无关 MCP 工具） |
| 第 2 轮 `tools_tokens` | 3228（信封里的记账键选中 13 个 GitHub PR 工具） | **2581**（与第 1 轮逐字节一致） |
| 第 2 轮首调用命中 | 8192/13806 = 59.3%，miss 5614 | **9600/12983 = 73.9%，miss 3383** |
| 两轮 `system_tokens` | 7223 | 7223（不变） |

注意：上表的「改动后」是同一会话两轮（`repl-529a50d22cfa` /
`repl-5a505d780d03`），命中份额只说明**第一调用少付的 miss**；一个任务
后续步骤的 miss 取决于模型轨迹，不在本项优化范围。

**看成本，不看命中率**：命中 token 仍计价（flash ¥0.02/M vs miss ¥1/M），
把历史垫进前缀可以把命中率做到 99% 而账单更高。目标指标是「每任务 miss
token」，不是 hit%。

回归：`python -m pytest tests/ -q` → **4192 passed, 100 skipped**。

### 新增：自我进化的确定性总结（2026-11-16）

自进化原先只做到「记下教训」：失败 → 提案 → 存储 → 下次携带 → 复发达阈值
退役。用户指出这还不够——要能**自主吸取并总结教训**。实现只增加确定性
聚类，LLM 不参与任何学习判断（详见 `docs/design/self-evolution.md`）：

- **结构族**（`xenon/kernel/lessons.py`）：`LessonShape = event|subject`，
  subject 取代码字段（capability / obligation kind / loop signal / tool），
  不是自由文本。策略否决此前没有任何 subject（gate 写的是 `scope`），现在
  回退到 `scope[0]`，且 `_check_policies` 的自动否决也会提案教训——它曾是
  唯一「永远不会成为教训」的失败类别。
- **总结**（`xenon/harness/lessons.py`）：同一族 ≥2 条失败 → 一条 `rule`
  （`derived_from` 保存全部成员 id，文本由模板生成）；规则覆盖的成员不再
  逐条进入约束区，摘要替代枚举。规则按 `hypothesis → active → proven`
  推进，证据是「被应用后同族失败未复发」；被应用的规则若同族**仍然复发**，
  达阈值即退役，并写 `DESIGN_DEBT_OPENED`——此时缺的是机制（工具设计 /
  策略 / 验证），不是又一句提示；已退役的族不会被重新聚类复活。
- **相关性**（`xenon/harness/runner.py`）：`match_for_task(available=
  runtime.tool_subjects())` 不再盲选「最近应用的三条」：`policy_decision` /
  `user_approval` 教训只有在 subject 出现在本任务 catalog 时才携带；
  `debt_opened` / `loop_detected` 是通用教训。catalog 缺失时失败开放。
  `parameter` 教训改为作为建议文本携带（此前存储后永不再见）。
- **可见性**：新增 `/memory lessons`（`--all` 含退役）展示状态、结构族、
  应用/复发次数与 `derived_from`；新增事件 `LESSON_CLUSTERED` /
  `LESSON_CONFIRMED` / `DESIGN_DEBT_OPENED`，三者都在
  `_INTERNAL_EVENT_KINDS` 中（规则经约束区进入提示，不从事件回放）。

测试：新增结构族/聚类/幂等/状态机/族复发/设计债/相关性用例，以及
「两次同族失败 → 一条规则 → 下一任务只携带规则」的真实 runner 链路。

真实入口验收（2026-11-16，TS 前端 + ConPTY + `deepseek/deepseek-v4-flash`）：
`tty_lesson_double_run.py` 扩展 `/memory lessons` 断言，8 项全过；账本
`repl-50cd26e287cd`（提出/存储）与 `repl-bf5d5cbd1c88`（应用）覆盖
`user_approval|shell` 与 `debt_opened|requirement_met` 两族，下一轮逐条
携带并显示 id、状态、计数与 `族:`。该验收同时暴露了下面的方括号 id 丢失
问题（已修）。

### 优化：前缀缓存的三个阶段（2026-11-15）

真实任务暴露三个断点，按「数据流 → 统一契约」一次性修掉，并用直连
DeepSeek 的真实任务验收。厂商按 **system → tools → messages** 序列化，
所以任何排在工具 schema 之前的可变字节都会让整块工具定义重新计费。

- **L1 归因**：`model_usage` 此前只有 token 数，无法回答「这次调用最多能
  命中多少」。现在每次调用都带 `model`、`phase`、`system_tokens`、
  `tools_tokens`、`cacheable_tokens` 与真实 hit/miss
  （`xenon/utils/llm_usage.py`、`xenon/kernel/usage.py`、`llm_client` 统一
  标注）。`/cache explain` 因此能区分「前缀本来就只有 640 token」和「前缀
  有 3344 token 但没命中」。
- **L2 阶段不变式**：planner / finisher / reflector / reviewer 都把各自的
  阶段协议**追加到 system**，导致 tools 块整块失效；同时这些阶段完全不发
  tools，`cacheable` 上限只有 system 本身。现在协议进入 turn 消息，所有
  6 个 phase builder 共用同一份 system 字节，非执行阶段也携带工具 schema
  （`tool_choice=auto`；真出现工具调用时回退 `none` 重试一次），
  `session_section` 也移到 turn 里。实测同一任务的 finisher 命中率从
  **19%**（640/3344）升到 **77–95%**。
- **L3 结论：不做追加式事件日志**。原计划的「call k+1 = call k 消息 +
  新回执」经测量没有收益：delta 设计下同一阶段的协议与 preamble 本来就是
  两次调用共有的前缀，唯一新增的字节就是事件本身；再追加旧事件只增长
  prompt、稀释注意力。按约定只保留 L1+L2，并补上下面的跨回合修复。
- **跨回合：system 改为会话稳定**。目标、约束、执行边界（含逐回合变化的
  `Mode:`）、检索记忆、工作区布局、本任务选中的 MCP 工具名此前都在 system
  里，于是同一个会话的**每一次新任务**都把 8–9k token 的 system 和整个
  tools 块重算。现在这些任务级文本组成 **task brief**，位于 turn 消息的
  开头；system 只保留人格、workspace、常驻工具目录和会话稳定上下文
  （项目规则、技能索引）。`session_context_layers()` 按
  `context_manager` 的 stable 标记分层下发（`TaskSpec.stable_context`），
  volatile 记忆仍走任务 brief。

真实入口（`xenon.exe` ConPTY，直连 `deepseek/deepseek-v4-flash`）验收：

| 场景 | 改动前 | 改动后 |
|---|---|---|
| 任务第 2 次调用（finisher） | 19% | 77–95% |
| 同会话第 2 轮首调用（planner） | 1024/14025 = **7.3%** | 8704/13758 = **63.3%**；另一轮 8320/13725 = 60.6% |
| 第 2 轮的 `system_tokens` | 9030（含上一轮目标/记忆） | 7223（与第 1 轮逐字节相同） |
| 同任务内 `step_reviewer` | 仅 system 可达 | 94.8% |
| 整会话累计命中 | 40–44% | 58–84% |

回归：`python -m pytest tests/ -q` → **4152 passed, 100 skipped**。同步
更新了 harness 与 kernel 的契约测试（阶段协议进入 turn、目标/约束/边界/
上下文的落点从 system 改为 task brief、会话投影进入 turn，属于有意的契约
变更，不是回归）。

文档：`docs/deepseek-guide.md` 补记内核请求的字节顺序与「任务级文本不得
进入 system」的规则。

### 修复：dashboard 的提问、审批反馈、重绘与滚动（2026-11-15）

真实使用一次暴露四个断点，各自追到数据流断点后一并修掉：

- **提问被当成新任务**（「切换模型后无法直接提交」）：内核阻塞在
  `prompt.request` 时（`/set_model` 的编号选择器、`/setup`、`/skill create`），
  dashboard 的提交走的是 `turn.start`，于是选择器一直开着、"11" 作为一轮任务
  在旧模型上运行并 401。现在 `SessionController.submit()` 在 `state.prompt`
  存在时把文本路由到 `prompt.response`；`xenon/frontend/host.py` 在命令线程
  存活时拒绝 `turn.start`（报「a command is in progress」）；输入框占位符在
  等待回答时变为「回答上方的问题（Enter 提交）」。
- **审批点完没有状态**：点击后卡片立即消失，换成绿色「✓ 已应答：本次允许 —
  已发送给内核，等待它继续本轮…」；POST 失败则回滚卡片。清除由
  `state.turn.approval` 归零驱动，页面不伪造内核状态。
- **侧栏刷新让主对话空白、终端一直闪**：查询应答与相同内容的 `status` 轮询
  此前每次都产生新状态对象 → SSE 广播 → 页面整块 `innerHTML` 重写 + Ink 整屏
  重绘。现在 reducer 对相同 facts 返回同一对象，`status` 的两个时钟按状态栏
  显示的**整秒**归一（内核给 0.1 秒，此前每 500ms 一次轮询都算「变化」）；
  `SessionController.updateState` 无变化不发布；服务端 16ms 合并并按快照签名
  去重；页面所有写入走 `paint()`（同内容不碰 DOM），面板按 key 刷新且不再先
  清空成「读取中…」，宫格只在关键帧变化时重载。
- **滚动串台**：`body`/`main` 不再整页滚动；`.transcript`、`.panel`、`#grid`
  各自 `overflow-y:auto` + `overscroll-behavior:contain`（窄屏恢复整页滚动），
  滑侧栏不再带动主对话。

文档：`docs/design/dashboard.md` 补记「提问期间的 submit 就是答案」、
「只有快照真的变化才广播」与状态时钟的整秒语义。

回归：`python -m pytest tests/ -q --ignore=tests/test_graceful_restart_integration.py`
→ **4139 passed, 99 skipped**；`ruff check xenon tests` 干净；
`cd frontend && npm run build && npm test` → **102 passed**（新增：相同状态
保持同一对象、亚秒时钟不算新事实、提问期间输入路由、审批确认卡、面板刷新不
清空、提交轮询去重、主对话与侧栏各自滚动）。

真实入口（`xenon.exe` ConPTY + `--dashboard`，浏览器同一套 HTTP）：

- `verify_dashboard_loop.py` → PASS：`/set_model` 的提问由
  `/api/action submit` 回答后提示清除、命令打印「模型已设置并切换」、**没有开启
  任何一轮**；6 轮只读查询后 `rev` 不变；空闲 3 秒 SSE 仅有首包；审批 POST 后
  0.03 秒内 `turn.approval` 清空、随后 2.4 秒不再复活，回合以 `Python 3.14.4`
  正常结束；页面确实来自本次构建（`paint()`/`answeredApproval`/整秒契约都在）。
- `verify_dashboard_variants.py` → PASS：空闲时 20 次面板查询 → 终端
  **0 字节**；终端里输入 `/set_model`、由 dashboard 回答（同样无新回合）；
  dashboard 选择**拒绝** → 卡片 0.002 秒清除，本轮账本恰一条
  `approved=false`，shell 命令未执行。

### 清理：删除模式路由的内部遗留；前端/协议/文档与单循环实现对齐（2026-11-15）

上一轮把"一个循环"收口到了界面，但**内部**仍留着整套按任务切换模式的路由
机制（模块、配置、CLI 参数、命令、会话状态），只是不再被调用。留着停用的
路由器和留着停用的开关一样，会让下一位读者以为某个回合还会用到它。

- **删除模块**：`xenon/repl/intelligent_router.py`、`paradigm_selector.py`
  （规则 R1–R9 按 7 个范式名分派）、`feature_extractor.py`。`routing_history.py`
  保留 `RoutingRecord` / `RingBuffer` / `RoutingHistory`（模型路由器
  `auto_router.py` 仍在使用），删除 `RoutingDecisionRecord` / `DecisionHistory` /
  `ParadigmPreferences` / `OverrideRecord` ——这四个类只服务于已删的模式路由器。
- **删除装配与入口**：`REPL` 不再构造 `intelligent_router`，会话状态不再暴露它；
  `start_repl()` 去掉 `auto_route` 参数与启用分支；`xenon --auto-route` 参数删除
  （不再接受，避免一个"接受但无行为"的开关继续误导）；`/auto-route` 命令及其
  `stats`/`export` 一并删除（命令表由自动发现生成，`/help` 随之消失）。
- **删除配置面**：`system_config.py` 的 `AutoRouteConfig`、`auto_route` 配置段、
  `XENON_AUTO_ROUTE*` 五个环境变量、项目配置可覆盖段与示例配置中的对应块。
- **删除死代码**：`selector.mode_choices()`（只有测试调用，界面早已不提供模式
  选择器）及其 4 条测试；`difficulty_estimator` 的范式推荐（`TaskProfile.recommended_engine` /
  `engine_confidence` / `engine_reason` 与 `_recommend_engine()`）——它只服务于已删的
  模式路由器，模型选择本身只需 `TaskProfile` 的复杂度与意图字段。删除后 203 条
  相关用例（模型路由 / 缓存路由 / 队列 / 意图分类）全绿。
- **改名 ReAct 时代符号**：`_run_react_engine` → `_run_loop_turn`，
  `_escalate_for_react` → `_escalate_to_loop`（正文一直是 `run_kernel_turn`），
  调用方与 6 个测试文件同步；`direct_chat.py` / `kernel_host.py` 的 ReAct 措辞
  改为"交给循环执行"。
- **前端与协议对齐实际实现**：`sessions` 帧不再发送旧预设名 `paradigm`
  （`frontend/host.py`、`queries.py`、`protocol.ts`），dashboard 已按"单循环"
  显示；测试夹具里的 `mode: 'react'` / `'切换模式'` / `/auto-route` 示例改为
  真实值；`frontend/README.md` 说明没有模式选择器可做。
- **终端一致性**：`/sessions` 表的"模式"列改为"循环"并显示 `单循环`（旧会话
  保存的预设名只是阶段配置）；`/setup` 描述去掉"选模式"。
- **文档**：`docs/OPERATION_GUIDE.md` 的 §4.2 与 §7 改写为"单循环 + 阶段配置"
  （含旧的 7 引擎说明及其删除原因）；`docs/COMPARISON.en.md` 的"多范式核心
  差异化"与英文版对齐中文版（一个循环 + 6 个 profile）；`docs/ARCHITECTURE.md`
  的项目配置可覆盖段、`docs/design/dashboard.md` 的 `sessions.list` 字段同步；
  删除已完全失效的 `docs/智能路由配置指南.md`。

回归：`python -m pytest tests/ -q --ignore=tests/test_graceful_restart_integration.py`
→ **4138 passed, 99 skipped**（减少的用例来自已删除的模式路由测试）；
`ruff check xenon tests` 干净；`cd frontend && npx tsc --noEmit && npm test` →
91 passed。

真实入口复验（同一个 `xenon.exe`）：`integration_phase_h.py` → PASS（H4 改为
断言 `/help` 中不再出现 `/auto-route`，其余 H1–H7 不变）；`integration_phase_g.py`
→ PASS（成功工具回合：读目录后回答 25 个 `.py`，无审批卡；终端与 dashboard 的
`/mode` 逐字一致）。H7 的 `python -c` 仍被宿主预检拒绝，模型给出带回执的诚实
回答；harness 现在把"有回执的拒绝"和"证明 42"都视为合格，只拒绝无证据的数字。
`xenon --help` 已无 `--auto-route`。

### 修复：把"单一循环"收口到每一个界面（2026-11-15）

README 与架构文档早已声明"7 引擎已删除，模式只是同一个循环的阶段配置"，内核也
确实只有 `LoopRuntime`；但**表面**仍像有多个模式和多个引擎，用户看到
`/mode` 回一句 `direct`，自然会问"我是不是还在 direct 模式"。

- **一处描述，所有界面共用**（`xenon/modes.py`）：新增 `loop_phases()` /
  `describe_loop()`，按 `LoopProfile` 的字段推导阶段名（设计文档 §2.2 的
  Plan → React → Reflect），而不是按模式名查表。`/mode`、`/status`、欢迎卡、
  状态栏、dashboard 快照与内置编辑器状态行全部调用它，五处不可能再各说各话。
- **`/mode` 只读且说清运行规则**（`xenon/repl/command_groups/model.py`）：不再
  打印内部预设名；额外一行给出真实刹车——外层最多 3 轮（`LoopRuntime.MAX_OUTER_ROUNDS`）、
  单轮步数上限 1024（`DEFAULT_MAX_STEPS`）、停滞 180 秒让位（`Budget` 默认值），
  数字全部 import，不手抄。
- **`/auto-route` 退役切换语义**（`xenon/repl/commands.py`、`xenon/repl/repl.py`、
  `xenon/main.py`）：`turn_flow` 早已不再询问智能路由，但 `/auto-route on` 仍
  承诺"自动选择最合适的模式"，`--auto-route` 仍会把它打开。现在 `on/off` 不再
  改任何状态，只解释"模式自动路由已停用"；`stats/export` 保留，明确标注为
  停用前的历史决策；CLI 参数仍接受（避免脚本启动失败）但只打印同一说明。
- **界面措辞与键位**（`frontend/src/ui.tsx`、`frontend/src/App.tsx`、
  `frontend/src/dashboard/page.ts`、`xenon/repl/status_bar.py`）：欢迎卡
  `MODE direct` → `LOOP 单循环 Plan → React → Reflect`；底栏显示 `单循环`；
  `Shift+Tab` 从 `/mode next` 改为 `/mode`（落到解释，而不是空键）；
  dashboard 的"模式"列改为"循环"，已保存会话一律显示 `单循环`（旧预设名只是
  同一循环的阶段配置）。
- **对话文案不再声称切换引擎**（`xenon/repl/direct_chat.py`）：直答升级到循环时
  的"自动切换到 ReAct 模式"改为"交给循环执行"；失败提示同步改词。
- **文档对齐**（`docs/TUI.md`、`docs/ARCHITECTURE.md`、`docs/design/frontend-protocol.md`）：
  写明一个循环的三个阶段、外层次数与刹车，`ready.mode` 与 `status.mode` 的语义
  改为"循环标签"。

真实复现（`integration_phase_h.py`，同一个 `xenon.exe` + `--dashboard`）：
H1 欢迎卡显示 `LOOP 单循环 Plan → React（逐步） → Reflect（每步校验）`，全屏
无 `MODE` / `direct`；H2 `/mode` 与 H3 dashboard 转写逐字一致；H2b `/mode next`
回答"忽略"；H4 `/auto-route on` 回答"已停用"且不声称启用；H5 `/status` 只有
`循环:` 行；H6 dashboard 快照与实时状态帧同标签；H7 真实工具回合
（`python -c` 被拒 → 写临时脚本 → 执行）以 42 结束，底栏 `· 单循环 ·`。

回归：`python -m pytest tests/ -q --ignore=tests/test_graceful_restart_integration.py`
→ **4249 passed, 100 skipped**；`ruff check xenon tests` 干净；
`cd frontend && npx tsc --noEmit && npm test` → 91 passed；
`integration_phase_h.py` → PASS。

### 修复：不向人索要"工具必然拒绝"的授权；阻断回合不得空回答（2026-11-15）

真实入口 `xenon.exe`（ConPTY）+ `--dashboard`，复现「数一下当前工作目录下有
多少个 .txt 文件」：

- **工具边界预检**（`xenon/catalog/gateway.py`、`xenon/kernel/runtime.py`）：
  账本记录显示，人类为 `python -c ...` 批准了一次升级，回执却是"被宿主安全过滤
  拦截"——这次"同意"什么也没买到；模型随后两次重发同一命令，第 3 次策略拒绝触发
  上限，回合 `running → blocked`，`turn.end.answer` 为空。现在
  `LegacyToolGateway.preflight()` 用与工具**同一套**规则集（`ToolNode` 的模块级
  `_shell_refusal` / `shell_command_refusal`）在提问前判断"这个调用就算批准也
  跑不了"；`RequestEscalation` 命中时不再弹卡，改为一次**可继续**的 `refuse`
  （附"不要重问，用拒绝文本里点名的替代写法"），循环因此还能改走别的路。规则是
  结构性的（`shell` 能力 + 命令参数），不是工具名枚举。
- **阻断/失败回合必须有话说**（`xenon/frontend/host.py`）：`turn.end.answer` 为空
  时改为复用 REPL 的 `_result_summary`（债务、账本里的阻断原因、阶段），取消的
  回合保持沉默（那是用户自己按的停止）。
- **步内预期必须能被该步证明**（`xenon/kernel/runtime.py`）：原账本的
  `plan_accepted` 把 `shell` 步骤绑到 `test_passed`；shell 回执按构造是 `Opaque`，
  V1 永远绑定不上，修复循环于是不断重发命令。现在计划接受时按"命题种类 ← 回执
  效果 ← 该步能力"的结构规则丢弃不可证明的预期（`drop_unprovable_expects`），
  记入 `CONTROL_DECISION` 并给模型一条提示；`file_changed` / `file_created` /
  `no_stray_changes` 由工作区探针结算，任何路线都保留。

真实复现（同一入口）：G3 计数回合中，模型先被 `read-only` 拒绝，再发起
`request_escalation`；预检判定"工具必然拒绝"后**没有打扰用户**，循环改走
`search`。变体 V1（`.txt` 计数）仍然弹出授权卡，终端裸按 `1` 应答，答案正确。

回归：`python -m pytest tests/ -q --ignore=tests/test_graceful_restart_integration.py`
→ **4239 passed, 100 skipped**；`ruff check xenon tests` 干净；
`integration_phase_g.py` / `integration_phase_g2.py` → PASS。

### 验证：审批双端语义 + 跨端复杂任务（Phase E/F，2026-11-15）

真实入口 `xenon.exe`（ConPTY）+ `--dashboard`，同一会话内混合驱动：

- **浏览器审批**（终端不动）：shell 真实执行，答案含 `Python 3.14.4`；该轮账本
  恰好 1 条 `user_approval`（`approved: true`）。
- **终端按键审批**：按 `1` 后、任何浏览器 POST 之前 `dashboard.snapshot` 的待批
  列表即清空（证明按键到达内核）；该轮账本同样恰好 1 条；随后迟到的浏览器审批
  POST 返回 202 no-op，账本仍为 1 条（双端幂等）。
- 之后普通追问正常（`1+1 → 2`）。
- **跨端复合任务**：浏览器提交“写文件 + shell 读回”，终端按 `1` 批准；磁盘上
  `e2e_scratch/bridge_note.txt` 内容为 `bridge-ok`；终端追问读取该文件正确复述；
  浏览器执行 `/status` 与纯问答后，终端仍渲染同一会话。

**联调中排除的两个“假缺陷”**（记录以免再犯）：

- `ledger.events` 只回答**最近一轮账本**（与 `diff.get` / `evidence.chain` /
  `plan.get` 同口径），不是会话累计。把它的长度当“会话总数”会误判“终端无法
  应答同一会话的第二次审批”——实际账本里该轮 `user_approval` 恰好一条且
  `approved: true`。已在 `docs/design/dashboard.md` 的查询契约写明「按轮次取数」。
- 审批卡文字在应答后可能仍留在终端单元格中，**屏幕文本不能作为“按键已生效”的
  门控**；回合也可能在两次轮询之间就结束（没有 `running: true → false` 边缘）。
  E2E 的等待改为协议状态：`dashboard.snapshot.approvals` 与 `/api/state` 的
  `turn.approval.id` 对齐才按键，回合结束以输入前记下的 `turn.end.turn` 标记
  变化判断。

回归：`python -m pytest tests/ -q` → **4237 passed, 101 skipped**（393 s）；
`npm test` → **91 passed**；`npx tsc -p tsconfig.json --noEmit` 干净；
`ruff check xenon tests scripts` 干净。

### 修复：MCP 端到端联调暴露的路由与交付缺陷（2026-11-15）

真实入口 `xenon.exe`（ConPTY）+ `/dashboard`，配一个真实 stdio MCP 服务器
（普通 JSON-RPC 进程，工具 `echo`/`upper`），跑完"`/mcp add` → `/mcp tools` →
真实任务调用 → 退出重启 → 变体任务"（`integration_phase_d.py`，PASS）。
MCP 传输、注册表、catalog 注入、审批各自单测都正常，缺陷全在
"意图判定 → 执行契约 → 回合路由 → 最终交付"的衔接处：

- **点名工具的回合被判成"不需要外部操作"**（`xenon/repl/execution_policy.py`、
  `xenon/repl/turn_contract.py`）：`/mcp add` 已连接、`/mcp tools` 已列出
  `e2e_echo:echo`，但任务本体「使用 MCP 工具 e2e_echo:echo 回显 hello」被判成
  `ANSWER_ONLY`（理由"请求不需要外部操作"）：信号层没有"用户点名调用工具"的
  结构，契约因此 `requires_tools=False`，回合走直接对话（账本是
  `chat-*.jsonl`），模型只能回答"这个工具不可用"。现在信号层新增统一的显式
  工具请求：`server:tool` 命名空间标识符（两侧字母开头，排除时刻/URL/盘符）
  与「使用/调用 … 工具」句式，与具体 MCP/领域无关；策略层据此给到
  `READ_ONLY`，契约层补 `read` 操作，交互 REPL 与协议宿主通过同一个契约路由
  进内核。远端工具是否更敏感仍由执行器按工具名分级 + 审批升级决定，写/命令
  禁令照旧一票否决（"不要使用任何工具"仍判 `ANSWER_ONLY`）。
- **内核 finisher 丢弃散文式总结**（`xenon/harness/llm.py`）：工具已真实调用、
  回执内容就是 `echo:hello-e2e`，但模型对 finish 提示用散文回答（未按协议返回
  JSON），`_salvage` 在 JSON 解析失败时直接换成占位符
  `Plan execution completed; final summary unavailable.`——用户看到"总结不可用"，
  成功的任务看起来失败。改为保留散文正文（剥掉代码围栏）；只有空回复或畸形
  JSON 对象才回退占位符。

验证：同一入口重跑 Phase D 全程 PASS——`/mcp add` 连接、`/mcp tools` 列出
`e2e_echo:echo`/`e2e_echo:upper`、dashboard `mcp.status` 同步；echo 任务经审批
真实调用工具，答案含回执原文 `echo:hello-e2e` 且声明未改文件；退出后新进程
`/mcp list` 仍有 `e2e_echo`（凭据持久化 + 懒连接），upper 变体返回
`upper:ABC`。全量回归与 `ruff check` 结果见上方 Phase E/F 记录。

### 修复：真实多端联调发现的三个跨端缺陷（2026-11-15）

真实入口 `xenon.exe`（ConPTY）+ `/dashboard`，跨进程跑完"浏览器取消长任务 →
追问 → 只读内核任务 → 保存 → 新进程 `--resume` → 命令/dashboard 证据/上下文"
（`integration_phase_c.py`，PASS）。三个缺陷都不是单模块问题，而是信息在
"意图判定 → 执行契约 → 会话恢复 → 前端输入"之间断裂：

- **带"不要修改、不要运行"的读取请求被折叠成仅回答**（`xenon/repl/
  execution_policy.py`、`xenon/repl/turn_contract.py`）：用户说"读取
  `xenon/__init__.py` 告诉我版本，不要修改文件也不要运行命令"，硬约束把
  `no_write + no_execute` 直接判成 `ANSWER_ONLY`，而任务实际需要读文件；模型
  读取后返回工具协议，被执行层判为"违反仅回答约束"，任务失败。现在
  `read_evidence` 优先：同时 no-write/no-execute 但要求读取时保持
  `READ_ONLY`，`_constraint_contract` 的操作集也随之带 `read`。验证：同一
  提示词在真实前端返回版本 `1.0.0` 并附 read 回执。
- **恢复会话的账本/证据到不了 dashboard**（`xenon/frontend/host.py`、
  `xenon/frontend/queries.py`）：`--resume` 只恢复 REPL 的账本指针，宿主的
  `last_run/last_ledger` 仍是空的；`/evidence` 在终端正常，`evidence.chain`
  却报 `未报告`。宿主新增 `_sync_kernel_view()`（命令结束与启动恢复共用），
  读不到运行对象时按 `<ledger>.result.json` 读取内核自己写的结果；只读查询
  层 `_evidence_view()` 统一走这一事实源。验证：恢复后 dashboard 与终端指向
  同一账本 `repl-*.jsonl`、同为 `partial`。
- **intro 动画吞掉第一段输入**（`frontend/src/App.tsx`）：启动动画期间
  `useInput` 的第一个按键只用于"跳过动画"然后 `return`，整段输入被丢弃。
  `--resume` 的"已恢复会话"卡片正好与 3 秒动画重叠，恢复后的第一条命令
  （如 `/context`）静默消失；新会话里手快的人第一字符也会丢（`/status`
  变成普通任务 "status"）。改为：跳过动画后继续走正常的按键处理，输入不再
  丢失。验证：新会话启动即输入 `/status` 能执行；恢复会话后第一条 `/status`
  输出"系统状态"，Phase C 的 `/context`、`/evidence` 全部通过。

验证：`python -m pytest tests/ -q` → **4223 passed, 101 skipped**（351 s）；
`npm test` → **91 passed**；`npx tsc -p tsconfig.json --noEmit` 干净；
`ruff check xenon tests scripts` 干净（`docs/evidence/` 下的历史探针脚本另有
34 条既有告警，本次未动）。真实 ConPTY Phase C PASS：从 dashboard 取消长任务
（`cancelled=true`）、同会话后续追问、只读任务产出账本、`/save` 后新进程
`--resume` 恢复，终端 `/context`、`/evidence` 与 dashboard `evidence.chain`
指向同一账本且状态一致，跨进程上下文问答正确回忆起
`xenon/__init__.py` 与版本 `1.0.0`。

### 修复：真实多端联调发现的四个集成缺陷（2026-11-14）

真实入口 `xenon.exe`（ConPTY）+ `/dashboard`，一个会话连续跑完计划 → 批准 →
交付 → 保存 → 分叉 → 压缩 → 浏览器提交任务与命令（`integration_phase_b.py`，
PASS）。单模块测试全绿，缺陷都出在衔接处：

- **计划同意误读内核事件**（`xenon/frontend/queries.py`、`docs/design/dashboard.md`）：
  账本 `plan_accepted` 是运行时"已提出计划并停在 PLANNED"的内部标记，面板却当成
  "用户已批准"。`plan.get` 改为复用 `/plan` 的同一解析器 `resolve_plan_approval`
  （会话事件 `plan/approved|rejected` 按 task_id 绑定），并新增 `rejected`；
  没有用户决定时，已交付/已结束的运行视为执行过，其余仍是待批准。页面补
  "待批准/已批准/已驳回"三种横幅。
- **批准计划的交付卡死 60 秒**（`xenon/catalog/finalizer.py`）：宿主 stdin 是
  NDJSON 管道，`GitFinalizer` 的 git 子进程继承它后一直等输入，直到 60 s 超时，
  交付被记为 `unavailable`（"cannot read git status"），没有生成提交。改为
  `stdin=subprocess.DEVNULL`，并把真实 git 错误写进失败原因。复测：批准计划在
  10 s 内完成并产生交付提交。
- **命令线程的内核运行不进入面板**（`xenon/repl/runtime_adapter.py`、
  `xenon/frontend/host.py`、`xenon/repl/turn_flow.py`）：`/plan approve` 在命令
  线程恢复计划，宿主只在 `run_turn` 更新 `last_run`，于是账本已 `verified` 且交付
  了提交，`evidence.chain` 仍报 plan-only 的 `unverified`。现在所有内核运行统一
  发布到 `repl._last_kernel_run`，宿主 `_sync_kernel_view()` 在命令结束后接管
  **新**运行（普通命令不会复活上一轮已失败的运行）。
- **Dashboard 拒绝空参数**（`frontend/src/dashboard/server.ts`）：`command.args`
  与 `prompt.value` 套用了"非空字符串"校验，无参数命令 `/cost`、回车接受默认
  答案在浏览器 400、在终端正常。改为可选字符串（缺省/null → `""`，非字符串仍
  400），并补服务端回归。

验证：`python -m pytest tests/ -q` → **4219 passed, 101 skipped**；
`npm test` → **91 passed**；`npx tsc -p tsconfig.json --noEmit` 干净；
`ruff check` 干净。真实 ConPTY Phase B 全链路 PASS（计划仅规划、批准后交付
`beed68c0`、`pytest` 7 passed、`evidence=verified`、保存/分叉/压缩、浏览器提交
fork 任务交付 `bff4fd32`、Dashboard `/cost` 落进共享 transcript；查询最大耗时
0.10 s）。

> 开发注意：源码工作区的入口优先加载 `xenon/frontend/assets/xenon-ui.js`
> （发布副本）。改完前端后若只跑 `npm run bundle`，真实入口仍会用旧产物；
> 验证前先执行 `python scripts/build_frontend_assets.py --skip-build`。

### 新增：面板九项验收补齐（E1/E2/E3/E5/E6/E7/E9/E10/E11，2026-11-14）

把计划里标记为「简化」的九项面板能力补齐，仍然只读、不改内核设计。

- **查询帧**（`xenon/frontend/queries.py`、`host.py`）：
  - 新增 `sessions.tree`：从会话事件日志读 `turn/user` 与 `fork.parent`，
    返回 `nodes[{id,parent,turns,events,first_at,last_at}]` + `roots`（最多 100 份）。
  - `dashboard.snapshot`：债务项增加 `next_action` / `attempts` /
    `acceptable` / `user_accepted`，顶层增加 `delivery`。
  - `workspace.status` / `diff.get`：增加 `baseline` / `baseline_count` /
    `delivery`；diff 每个文件带 `origin`（created / modified / baseline）与
    `verification`（verified / attributed / unverified）。
  - `plan.get`：增加 `accepted` / `proposed` / `current`。
- **页面**（`frontend/src/dashboard/page.ts`）：
  - 输入框 `/` 斜杠补全（↑/↓ 选择，Tab/Enter 补全，Esc 关闭）。
  - ≥1400px 四宫格（总览/Diff/执行过程/证据链，2.5s 节流），「▦ 宫格」切回标签模式；
    窄屏退化为标签页。
  - Diff：origin/verification 角标、unified/并排切换、baseline 污染区、交付 commit。
  - 执行过程：瀑布条显示逐事件耗时、>30s 停滞、连续同类事件重试、取消/中断、token 合计。
  - 证据链：增加投影 JSON（可展开）。
  - 计划：步骤流 + `depends_on` 依赖、当前步骤高亮、待批准/已批准横幅。
  - 审批：五档按钮（本次/本轮/30 分钟/拒绝/取消）与提问输入框，面板内可直接作答。
  - 工作区：脏文件来源角标、baseline 污染表、已核验/归因/未核验、交付 commit。
  - 会话：fork/rewind 树 + 已保存会话 + 记忆 + MCP。
- **契约修复**（真实入口发现）：`plan.get` 在没有账本时只返回 `plan/steps/error`，
  缺少 `accepted/proposed/current`，面板必须按「有无账本」分支。现在错误帧与正常帧
  字段一致；`diff.get`（非 git）与 `evidence.chain`（无账本）同样补齐
  `baseline/delivery`、`projection`。
- 文档：`docs/design/dashboard.md`（查询字段、九个面板契约、布局）、
  `docs/design/frontend-protocol.md`（§3.10 增加 `sessions.tree` 与稳定形状契约）。

验证：`cd frontend && npx tsc -p tsconfig.json --noEmit` 干净；`npm test` →
**91 passed**（新增 `page.test.ts` 9 条，在 VM 里执行内联页面脚本，覆盖九项）；
`python -m pytest tests/frontend -q` → **18 passed**；`ruff check xenon tests` 干净。

真实 ConPTY 验收（`dash_gaps_e2e.py`，rc=0）：`xenon --dashboard` → 页面包含
宫格/并排/baseline/待批准/会话树标记；十一个查询 kind 全部 200 且字段齐全
（`sessions.tree` 返回 100 个节点）；浏览器提交真实写文件任务、浏览器作答审批，
轮次完成并产生答案；`dashboard.snapshot.delivery.commit` 非空且
`verified/attributed/unverified_paths` 含该文件；`diff.get` 的 baseline 样本
（`CHANGELOG.md`）带 `origin=baseline`、`verification=unverified`；
`workspace.status.baseline_count ≥ 1`；终端无残留面板。原 `dash_e2e.py`
回归仍 rc=0。

> 该验收任务由 Xenon 自己的交付逻辑生成了一次 `xenon: …` 提交；验收后已
> `git reset --soft` 撤销并把 scratch 文件从工作区删除，仓库回到验收前状态。

### 新增：本地可视化面板 `/dashboard` / `--dashboard`（2026-11-14）

终端仍是默认入口；`/dashboard`（或 `xenon --dashboard`）在同一个 Node 进程内起一个
只监听 `127.0.0.1` 的 Web 面板，与终端共享同一个 `SessionController` 和同一份内核会话。
面板是第二个渲染器，不是第二个真相源：不 spawn 内核、不 import `xenon.repl.*`、
不直接读账本。

- **只读查询帧**（`xenon/frontend/host.py` + `queries.py`）：
  `dashboard.snapshot` / `workspace.status` / `diff.get` / `ledger.events` /
  `evidence.chain` / `plan.get` / `memory.list` / `mcp.status` / `report.get` /
  `sessions.list`，请求带 `id`，回答为同 id 的 `<kind>.result`；不能回答时给
  `error` 字段，不抛异常。独立 daemon 线程执行，避免 git 阻塞 stdin 主循环。
- **本地服务**（`frontend/src/dashboard/server.ts`）：`node:http`，无新增运行时依赖；
  随机端口 + 每进程随机 token（`?t=` 或 Bearer，`timingSafeEqual`）、Host 允许列表、
  仅回环绑定、CSP / `X-Frame-Options: DENY`、64 KB 请求体上限、SSE 最多 16 个客户端；
  没有 shell/eval 端点；`XENON_DASHBOARD=0` 关闭。
- **页面**（`frontend/src/dashboard/page.ts`）：单个自包含 HTML（无 CDN），
  概览 / 主交互 / Diff / 执行过程 / 证据链 / 计划 / 审批 / 成本 / 工作区·会话
  九个视图；SSE 事件是带 `rev` 的完整快照，断线重连不丢事件、不重复渲染。
- **双端一致**：终端与浏览器都能提交输入、运行命令、回答审批/提问、取消；
  同一审批只有第一次回答有效，第二次是幂等的 no-op。
- 命令与入口：`/dashboard` 注册进 `/help` 与补全；`xenon --dashboard` 启动时自动打开；
  `--ui python` + `--dashboard` 明确拒绝（退出码 2）。
- 文档：新增 `docs/design/dashboard.md`；`docs/design/frontend-protocol.md` 增加
  §3.10 查询帧；`docs/TUI.md` 与 `frontend/README.md` 增加面板说明。

真实 ConPTY 验收（`xenon.exe` 入口，`dash_e2e.py` / `dash_turn_e2e.py` /
`dash_approval_e2e.py` 均 rc=0）：`/help` 列出 `/dashboard`；终端打印带 token 的地址；
无 token 401、非法 Host 403；页面与 `/api/state` 可读；`dashboard.snapshot`
（模型 `['deepseek-v4-flash']`）与 `workspace.status`（branch main）应答；
SSE 首包到达；浏览器 `POST /api/action` 运行 `/status` 在终端可见；非法 action 400；
`XENON_DASHBOARD=0` 明确拒绝。浏览器提交真实任务后终端实时渲染、状态携带
objective/answer；终端审批卡与浏览器共享同一 request id（`a-2 shell`），浏览器选
「本次允许」后轮次完成（42），第二次回答无副作用。

验证：`cd frontend && npx tsc -p tsconfig.json --noEmit` 干净；`npm test` →
**82 passed**（含 dashboard server 9 条）；`python -m pytest tests/frontend -q` →
**15 passed**；全量 `python -m pytest tests/ -q` → **4208 passed, 101 skipped**；
`ruff check xenon tests` 干净。

#### 修复：面板所有查询超时——git 子进程继承了宿主的 NDJSON stdin（2026-11-14）

真实 ConPTY 里 `/dashboard` 打开后，`workspace.status` 30 秒无应答、`diff.get`
8 秒后返回「工作区不是 git 仓库」。根因不是 git 慢：宿主 stdin 是协议管道，
`_git()` 的 `subprocess.run` 继承了它，git 子进程一直等输入，直到 `GIT_TIMEOUT`
才被杀死；面板的 5 秒查询超时先到。

修复（`xenon/frontend/queries.py`）：git 子进程 `stdin=DEVNULL`，并设置
`GIT_TERMINAL_PROMPT=0`（不在无终端时等凭据）、`GIT_OPTIONAL_LOCKS=0`（只读查询
不抢 index 锁）。复测同一入口：`workspace.status` 0.09s、`diff.get` 0.15s。
回归测试 `test_git_never_inherits_the_host_stdin` 用 spy 锁定这三个参数。

#### 修复：`--dashboard` 启动通知让欢迎卡消失（2026-11-14）

`showWelcome` 的条件是 `ready && transcript.length === 0`；面板地址以 notice 写入
滚动区后，欢迎卡（版本、模型数、cwd）在第一秒消失，ConPTY 验收直接卡在
`wait_ready`。修复（`frontend/src/state.ts`）：抽出 `showWelcomeCard(state)`，
空会话的判断忽略 `kind: 'notice'` 的块；真实轮次/命令仍会正常顶掉欢迎卡。
新增 reducer 测试「a notice does not hide the welcome card, a turn does」。

### 修复：/import_models --dry-run 报"已注册 0 / 更新 0"；/remove_model 不落盘（2026-11-14）

真实 ConPTY 验收：`/import_models e2e_scratch/import_demo.yaml --no-probe --dry-run`
对一份合法文件输出「✅ 已注册 0 / 更新 0 个模型」——与畸形文件完全同一行，
用户无法据此判断文件是否可用；随后的真实导入成功，但 `/remove_model <alias>`
只删内存，重启后 `/import_models` 写进 `models.yaml` 的条目又回来。

根因两条，都是"结果没有回传到下一层"：

1. `batch_register(..., dry_run=True)` 在注册循环前直接 `return`，`registered` /
   `updated` 必然为空；`BatchResult` 没有"我本来会做什么"的字段。
2. `/remove_model` 只调 `registry.models.pop(...)`，从未写回盘；而
   `ModelRegistry` 不记录自己是从哪个文件加载的，于是"写回"也无从安全实现
   （进程内/测试 registry 猜路径会覆盖用户真实配置）。

修复：

- `BatchResult` 增加 `dry_run` / `would_register` / `would_update`；dry-run 先按
  registry+pool 的存在性分类再返回，摘要改为 `🧪 dry-run：预计注册 1 / 更新 0
  个模型（未写入磁盘）` 并列出新增/更新别名。（方括号 `[dry-run]` 会被 rich
  当样式标记吞掉，所以状态词放在冒号前用的是普通文本。）
- `ModelRegistry.source_path` 记录**实际加载来源**；`/remove_model` 仅在该路径
  存在时 `save_to_file` 持久化，无来源则诚实说明"当前会话；无配置文件可写"，
  凭据派生模型（`derived=True`）说明"重启后会重新加载"。

真实 ConPTY 复现（同一入口）：dry-run → `🧪 dry-run：预计注册 1 / 更新 0 个模型
（未写入磁盘）` + `新增: imported-flash`；真实导入 → `✅ 已注册 1 / 更新 0 个模型`
+ `💾 已持久化到 …\.xenon\models.yaml`；`/remove_model imported-flash` →
`✅ 模型 'imported-flash' 已移除` + `💾 已持久化`，之后 `/models` 不再列出。
验收后 `~/.xenon/models.yaml` 已按验收前的字节恢复（sha256 一致），用户配置未被测试污染。

验证：`python -m pytest tests/test_batch_register.py tests/test_model_commands.py -q`
→ **40 passed**。

### 修复：/code、/sub-agent、/edit 不使用会话已选模型，命中死代理 402（2026-11-14）

真实 ConPTY 验收（`xenon --model deepseek-v4-flash`，状态栏显示该模型）：

- `/code 写一个 Python 脚本打印 1 到 5 的整数之和 --file e2e_scratch/sum5.py --run`
  → `❌ 代码生成失败: Client error '402 Payment Required' for url
  'https://heyroute.ai/v1/messages'`，文件未写入、后续 `/edit` 报文件不存在。
- `/sub-agent 读取 e2e_scratch/sales.csv 的第一行…` → 子任务失败，面板直接
  倾泻子进程 traceback（同样是 402）。
- `/edit e2e_scratch/sum5.py 把变量名 total 改成 result` → 成功，但耗时 **152 秒**：
  逐个尝试 9 个不可用代理模型后才落到可用的 `deepseek/deepseek-v4-flash`。

根因是一条断裂的信息流：`_preferred_model_ids` 是「本会话使用哪个模型」的契约
（`--model` 写入、`/set_model`//model` 改写、状态栏与内核轮次读取），但一次性
LLM 命令只读 `get_role_priority(...)` 的静态顺序，从不读取用户选择。`/code`
更只取 `model_ids[0]`，第一个死代理失败即整体失败；`/sub-agent` 按模型池插入
顺序挑首个；`/edit`、快捷指令/技能生成、输入分类器、计划恢复、静态路由回落
都从死代理开始试。

修复：新增共享 `ordered_model_candidates(registry, repl, *roles)`（会话已选模型
优先、别名归一化为 canonical id、去重），并接入所有一次性 LLM 选择点：
`/code`（失败依次尝试下一候选；`/sub-agent`、`/edit`、快捷指令生成、技能生成
与执行、输入分类器、`turn_flow` 静态回落、已批计划恢复。

真实 ConPTY 复现（同一入口、同一命令）：

- `/code … --file e2e_scratch/sum5.py --run` → `✅ 代码已写入`（5 行），确认后
  `✅ 运行成功: 15`；变体 `/code 用 Python 输出 6 的阶乘` → `✅ 代码已写入`。
- `/sub-agent 读取 e2e_scratch/sales.csv 的第一行…` → `🧬 子任务 1/1 已交付
  finalized`，输出 `date,customer,amount`，账本落盘。
- `/edit … 把变量名 total 改成 result` → diff 正确、确认后 `✅ 已修改 sum5.py`，
  耗时 **152s → 6s**。

验证：`python -m pytest tests/test_agent_commands.py tests/test_shortcut_commands.py
tests/test_skills.py tests/test_workspace_commands.py tests/test_repl.py -q` →
**146 passed, 1 skipped**；`ruff` 干净。新增用例覆盖「会话模型优先」「首个模型
被拒后回落」「子 Agent 继承会话模型」。

### 修复：/review 永远显示"本会话没有记录到文件改动"（2026-11-14）

真实 ConPTY 验收：`/edit e2e_scratch/sum5.py …` 成功修改文件、内核轮次
`apply_patch` 成功创建 `e2e_scratch/review_probe.txt`，随后 `/review` 仍回答
「本会话没有记录到文件改动」——包括会话保存/合并也依赖的改动清单。

根因：`session_created_files` / `session_modified_files` 两个工作记忆键只有
读者（`/review`、`session_ops` 的会话合并、`context_manager` 的压缩保留），
**全代码库没有任何写入者**。`ReplEventSink` 的注释甚至声称它们「让会话工作记忆
看到内核写了什么」，但实际只把路径交给面板，从未落账。

修复：建立唯一生产者 `ContextManager.record_file_changes(created, modified)`
（去重、created 优先、最多保留 50 条），并在所有文件变更路径接入：

- 内核工具回执：`ReplEventSink` 从 `fs_write` 的 `paths` / `created_paths`
  记录（协议宿主与交互路径共用同一个 sink）；
- `/code` 写入新脚本（新建/覆盖分别记 created/modified）；
- `/edit` LLM 修改成功后记录（`.bak` 因此可被 `/review revert` 找到）。

真实 ConPTY 复现（同一入口、同一会话）：`/edit` 后 `/review` →
`[修改] D:\Xenon\e2e_scratch\sum5.py （有 .bak，可 /review revert）`；
内核 `apply_patch` 写入后 `/review` → 同时列出
`[新增] D:\Xenon\e2e_scratch\review_probe2.txt` 与上一条修改。

验证：`python -m pytest tests/test_agent_commands.py tests/test_workspace_commands.py
tests/test_diff_review.py tests/test_repl_kernel_runtime.py -q` → **72 passed**。

### 修复：新建快捷指令不能立即调用（2026-11-14）

真实 ConPTY 验收：`/shortcut create e2e-hello` 完成五步对话后提示
「✅ 快捷指令 /e2e-hello 已创建！使用 /e2e-hello 执行。」，下一次输入
`/e2e-hello` 却返回「未知命令: /e2e-hello。输入 /help 查看可用命令。」。
技能同场景（`/skill create` → `/e2e-skill`）可以调用。

根因：动态命令只在 REPL 启动时由 `_load_custom_commands()` 注册；技能创建路径
有 `_register_skill_handler` 立即注册，快捷指令的两条创建路径（自动生成/手动配置）
都没有。修复：新增 `_register_shortcut_handler`，在 `manager.create` 之后立即写入
`_HANDLERS` 与命令元数据，与技能走同一契约。

真实 ConPTY 复现（同一入口）：创建后直接输入 `/e2e-hello` 输出
`$ echo shortcut-ok` / `shortcut-ok`，`/help` 中出现 `[快捷] 回显测试`；
`/shortcut run e2e-hello`、`/shortcut delete`、技能创建/运行/删除均无回归。

验证：`python -m pytest tests/test_shortcut_commands.py -q` → **8 passed**；
新增用例覆盖「创建后命令可立即 dispatch」。

### 修复：前端轮次不写会话历史，/save 落空、--resume 报"会话不存在"（2026-11-14）

真实 ConPTY 验收（`/save d6-e2e` → `/exit` → `xenon --resume d6-e2e`）暴露：
保存的会话 `history` 为空，`/sessions` 不列出（`list_sessions` 跳过空会话），
`--resume` 直接输出「会话 'd6-e2e' 不存在」并退出；会话内 `/save` 却显示成功。

根因是协议入口漏掉了交互路径的「对话记录」契约：

- **内核轮次不记用户消息**：交互式 `turn_flow` 在派发前调用
  `ctx_mgr.add_request_message(...)`，回答由 `run_kernel_turn` 渲染时补记；
  协议宿主 `run_turn` 直接调用 `run_kernel_turn`，跳过了前半段。真实工具任务后
  `ctx_mgr.history` 只有 assistant 一条（或为空），`/save` 导出的会话无法被
  `/load` 回放，`/context` 也少算。
- **记忆门轮次什么都不记**：`_run_chat` 的「记住……」分支直接返回回执，
  用户消息与回执都没有进入历史——正是 D6 场景（"请记住这个数字…42"）触发的路径。

修复：协议宿主按交互路径的同一契约记录每轮——`run_turn` 在调用内核前写请求
（记录原始用户输入与意图元数据，内核收到的仍是同一份冻结 envelope），
`_run_chat` 先写请求再走记忆门，记忆门命中时把回执作为 assistant 消息写入。

真实 ConPTY 复现（同一入口、同一输入）：
- 内核任务（读 `e2e_scratch/sales.csv` 首行）+ 普通对话各一轮 → `/save` →
  会话文件 `messages: 4 roles: ['user','assistant','user','assistant']`，
  `/sessions` 列出，新进程 `--resume d6-e2e-v2` 恢复后提问
  「我的项目代号是什么？」正确答出 `蓝鲸-8888`。
- 变体（"请记住：项目代号是海豚-4242" 记忆门）→ 会话 `messages: 2
  roles: ['user','assistant']`、回执在历史中，`--resume d6-mem-v2` 正常启动并列出。

验证：`python -m pytest tests/test_protocol_host.py tests/test_session_commands.py
tests/test_repl.py -q` → **161 passed**；新增用例覆盖内核轮次写请求、记忆门
记录请求+回执、普通对话只记一次；`ruff` 干净。

### 修复：前端审批作用域、/permissions 与 /restart 的五处阻塞（2026-11-14）

真实 ConPTY 验收（从 `xenon` 前端入口，`/permissions default|plan|bypass`、五档
审批答案、`/permissions` 查看、`/restart`）暴露五处阻塞，均按「定位 → 根因 →
跨层系统性修复 → 同一入口复现」处理：

- **权限上限只作用于交互路径**：`/permissions plan` 后经前端仍能写文件/执行命令。
  每轮 `run_turn` 都会按目标重算 turn contract，而模式只存在
  `agent_context["_execution_level"]`，重算时被覆盖。协议宿主现在在
  `run_turn` 里调用 `_apply_permission_ceiling`，把契约钳制到会话模式上限
  （只降不升），与交互式 `turn_flow` 走同一个 `apply_permission_ceiling`。
- **「取消整个任务」等同于拒绝**：审批面板选 cancel 后内核只把这一次工具调用
  当失败，继续规划下一个动作。新增共享 `cancel_running_task`，两个审批桥
  （协议宿主与交互 mixin）都在 cancel 时对 `_active_runtime.request_cancel()`，
  让内核自己的 `_check_limits` 以 `stop_signal="cancelled"` 中止本轮。
- **`/permissions` 无参选择器吞掉授权列表**：前端里 `ask_choice` 总会回答，
  命令在选择器处直接 return，`/permissions` 只剩「切换模式」而看不到也撤销
  不了已记住的授权。选择当前模式（Enter）时回落到原有的授权列表渲染。
- **前端「允许」不落 GrantBook**：`allow` 只写进当轮 policy，会话级授权簿仍为空，
  下一轮同路径再问一次、`/permissions` 也列不出来。新增共享
  `remember_allow_answer`（工具 + 路径 + 30 分钟 TTL，工作区取实时根目录），
  交互 mixin 与协议宿主都调用它。
- **`/restart` 经协议前端只答应不执行**：命令只 `request_restart`，真正执行在
  交互式主循环里，协议宿主没有主循环。宿主在 `run_command` 里读取
  `RestartCoordinator` 并执行 `perform_restart`，把结果并入同一条命令输出。

真实 ConPTY 复现（`xenon` 前端入口）：`plan` 下写文件被拒且无文件、`bypass`
下 `wc -c e2e_scratch/sales.csv` 无审批执行、`default` 下审批一次写入；
`allow` 后同一路径第二轮不再出现 edit 审批面板且 `/permissions` 列出
`edit: e2e_scratch/d2z.txt [剩余 28 分钟]`；`cancel` 后本轮输出
`⏹ 本轮已中断（Ctrl+C），未交付任何结论` 且文件未创建；`/restart` 输出
`🔄 正在准备重启（保存会话）... ✅ 重启成功 · 会话已恢复`，随后仍能继续对话。

验证：`python -m pytest tests/test_protocol_host.py tests/test_approval_policy.py
tests/test_permissions_command.py -q` → **104 passed**；新增用例覆盖权限上限
钳制、cancel 中止运行时、allow 写入会话授权、`/restart` 实际执行、
`/permissions` 选择当前模式后仍列出授权；`ruff` 干净。

### 修复：脚本产物被当成脏文件、交付门自锁、已批准的链式命令被拒（2026-11-13）

真实 ConPTY 验收（数据清洗任务：写脚本并运行，脚本输出 `sales_clean.csv`）暴露
三处阻塞，均按「定位 → 根因 → 系统性修复 → 同入口复现」处理：

- **脚本写出的产物是本轮证据**：`WorkspaceProbe` 增加 `created_paths`（本轮新出现、
  开跑前不存在的未跟踪文件）。清洁检查不再把这类文件报成
  `worktree contains unverified paths`；`GitFinalizer.finalize` 新增
  `attributed_paths`，只豁免、不暂存（提交里仍只有回执背书的文件）。
  现场：模型按要求生成 `sales_clean.csv`（脚本确实写了它），交付却因这条"未验证路径"
  被判失败，任务永远无法交付自己刚生成的成果。
- **交付门不再被自己的债封死**：`finalizer` 拒绝一次后开出的债，会让
  `_preflight_delivery` 再也不调用 finalizer——能解这条债的组件被它自己的债挡住。
  现在交付门只把「其他」债/verdict 当作前置条件；成功交付会记一条
  `finalizer satisfied` verdict 并关闭旧债。另：由「已证实失败」开出的债
  （`acceptable=False`）不再被 V2 当成语义 claim 重新审计，避免把
  `worktree contains unverified paths: …` 改写成
  `semantic claim needs a coverage binder`。
- **人已批准的链式命令被自己的授权拒绝**：`ScopedGrant.allows` 在原文本**完全相等**
  时直接放行；链式守卫只用于阻止前缀/形状匹配把 `a` 的授权扩散到 `a; b`。
  现场：`python count_sales.py; Get-Content sales_count.txt` 在审批面板原样展示、
  用户选择允许，实际执行却返回 `denied · shell requires approval`。

真实 ConPTY 复现（`xenon` 前端入口，审批面板自动应答）：
`python e2e_scratch/clean_sales.py` 生成的 `sales_clean.csv` 不再阻断开交付，
finalizer 生成提交；变体任务 `count_sales.py` → `sales_count.txt` 输出 `count=7`，
脚本提交、输出文件留在工作区（未跟踪、未被误提交）；链式 shell 命令获批后
连续执行三次均 `ok · opaque`，输出 `count=7`。

验证：`python -m pytest tests/ -q` → **4125 passed / 101 skipped**；新增用例覆盖
探针创建路径、finalizer 豁免、交付门重试、链式授权精确匹配；`ruff` 干净。

### 修复：TS 前端补齐 Python TUI 的展示与实时状态（2026-11-13）

TS 前端此前只渲染时间线，Python TUI 的启动、实时与结果呈现全部消失。
本次把「事实在内核、表现在前端」的契约补齐：

- **启动体验**：跑者动画由 TS 播放（帧与 Python 播放器同源，`scripts/brand`
  同时生成 `_brand_frames.py` 与 `frontend/src/brand-frames.ts`，3 秒 / 12 FPS，
  任意按键跳过）；欢迎卡恢复 `XENON vX.Y.Z`、MODE/MODEL/TIP；启动摘要按
  `ready.probed` 如实显示「已准备」或「已加载 N 个配置模型」；终端标签标题
  （OSC / `process.title`）在运行中滚动、等待审批时静止。
- **实时状态**：轮次运行中每 500 ms 拉取 `status`；spinner、本地秒表
  （与内核 `progress` 取较大值）、阶段/步数/工具数/停滞、token 条、消息数、
  会话时长（`MM:SS`，与 Python 工具栏一致）、缓存命中/费用/节省/流式。
- **结果呈现**：`● Assistant` 锚点 + 最小 Markdown（代码块/标题/列表/行内）；
  `💭 N 步 · N 个工具 · 证据`；`⚠️ 待人工核验清单` 与降级核验面板
  （`evidence=partial/unverified` 即使没有 debt 也会披露）；账本路径；
  `Ctrl+O` 展开完整执行详情；审批 diff 预览 30 行；退出时 `session.report`
  渲染本次会话省钱报告。
- **内核事实修补**：协议宿主像 `start_repl` 一样把 registry 配置喂进
  `model_pool.from_config()`（此前池为空，启动摘要显示 0、AutoRouter 失去数据源）；
  `ready` 增加版本/模式/模型计数/探测状态；`status` 增加 token/窗口/消息/
  会话时长/缓存/费用；`turn.end` 增加 `metrics` / `debt` / `planned`。
- 协议与 TUI 文档同步。

验证：前端 `tsc` + 49 条单测全绿；`tests/test_protocol_host.py` 33 条、
`test_startup_experience.py` / `test_logo.py` 9 条通过；Python 全量
`4087 passed / 100 skipped / 0 failed`（5m24s）；真实 ConPTY 启动确认
动画逐帧变化、欢迎卡（`XENON v1.0.0`）与「已加载 10 个配置模型」出现、
`/help` 正常；用配置内的 proxy 模型跑通一轮真实对话（`OK`），
状态栏显示 `MM:SS`，退出时省钱报告落地。

### 修复：命令输出顺序、模型切换、文件遍历上限（2026-11-12）

- **提问前的输出先到前端**：新增 `command.output` 帧。命令先打印的表格/菜单
  （`/set_model` 的模型列表、`/setup` 的向导菜单）现在出现在提问面板**之前**，
  不再"先问、后出列表"。
- **模型切换对下一轮生效**：协议宿主改用实时的 `repl._preferred_model_ids`，
  而不是启动时缓存的模型列表；聊天、内核轮次、账本记录与状态栏同时更新。
- **回答不再丢失**：`turn.end.answer` 在内核路径回退到 `TaskResult.answer`
  （此前只有回调路径会赋值，回放块里只剩工具步骤）。
- **结束态不再重复显示**：实时区只在轮次运行中渲染，证据页脚独立显示。
- **目录遍历有硬上限**：`list_files` / `search_files` 跳过
  `.git` / `node_modules` / `__pycache__` / `.venv` 等目录；列表 5,000 项 / 10s，
  搜索 2,000 个文件 / 15s，单文件读取上限 2MB。触发上限时结果**明确标注不完整**
  （新增 `scan_truncated` / `skipped_large` 字段）。
  起因：一次不带 `path` 的 `search` 以工作区 `D:\` 为根，遍历整块磁盘导致界面卡死。

## [1.0.0] — 2026-11-12

**Xenon 1.0：一个循环，一本账，一套可回放的真相。**

本版把此前累积的收口批次合并为 1.0：单一内核循环成为唯一的执行与验证链，
每一轮（含纯对话）都进账本，崩溃恢复精确到调用，凭证静态加密，配置分层，
沙箱 seam 可接 Docker/WSL，外部审计产品化为 CI Action，TS 前端成为唯一交互界面
且构建产物随 wheel 分发，Xenon 同时可以作为 **MCP server** 接入其他 Agent。

验证：`4080 passed / 101 skipped / 0 failed`（6m46s），ruff、kernel purity、
engine imports、docs references 全绿；前端 `tsc` + 36 条单测通过；
真实 ConPTY 双轮自进化验收通过；Claude Code 握手日志见
`docs/evidence/mcp-claude-integration/`。

### 收口批次：每轮入账、精确恢复、沙箱 seam、配置/凭证、审计 CI、TS 默认（2026-11-11）

**每一轮都有账本**
- 新增 `xenon/ledger/conversation.py`。REPL 的纯对话轮次与协议宿主的对话轮次都写入
  `ledger/repl/chat/`：`task_started(mode=chat)` → `delivered` → `finalized`；
  `_last_kernel_ledger` 指向它，`/evidence`、`/goal` 对纯对话也成立。写入失败只记 debug，不影响回复。
- `direct_chat._run_direct` 返回 `DirectChatOutcome(answer, model_id, delegated)`；
  升级到工具路径的轮次标记 `delegated`，不重复记账。

**崩溃恢复精确到调用**
- `ToolCall.call_id` 由 runtime 在**执行前**铸造；新增 `tool_started` / `tool_interrupted` 事件；
  网关三处回执与 `_execution_call` 复用该 id；打开已有账本时把无回执的 start 标为
  `TOOL_INTERRUPTED(outcome=unknown)` 并开 `call:<id>` debt。
- `report` 增加“结果未知的调用”；`replay` 展示 start/interrupted。
  **不自动重放有副作用的调用**（可能已部分执行），由模型/人核验。

**沙箱 seam**
- 新增 `xenon/sandbox/backends.py`：`process`（默认，仍**不是**安全边界）/ `docker` / `wsl`，
  复用 `ToolRuntime.command_prefix/prelude`；配置 `sandbox.backend|image|network|workdir`；
  配置的后端不可用时 fail closed（`kind="sandbox"`），不静默降级。

**配置分层**
- 新增 `xenon/utils/project_root.py`（不读配置的共享查找器）；
  `system_config` 按“用户 < 项目 < 环境变量 < CLI”合并（项目覆盖用户），缓存包含两个文件的 mtime；
  `skill_manager` / `project_context` 改用同一查找器；`XENON_CONFIG_PATH` 显式指定时关闭分层。

**凭证静态加密**
- 新增 `xenon/utils/credentials_io.py`：Windows DPAPI（ctypes，零依赖）、`keyring`（可选）、
  plaintext 兜底并在文件头与 `describe()` 标注；全部读者（provider_registry、llm_transport、
  harness/llm、model_registry、integration_cli、evals、github_auth）统一走它。
- `models.yaml` 保存时把 `api_key` 移入 credentials 的 `_model_keys`；明文凭证在读取时自动加密，
  不留下明文备份。

**其他修复**
- `harness/runner.run_plan`：全绑定（capability-bound）的计划即使配置了模型也不再调用模型——
  确定性计划不隐式发起网络请求，也不会因第三方端点抖动而失败。

**外部审计 CI**- 新增 `.github/actions/xenon-audit`（composite：JSON + PR 评论产物、strict 退出、artifact 上传、
  marker 更新评论）与 `examples/xenon-audit.yml`；`docs/EXTERNAL_AUDIT.md` 增加 CI 一节。
- 修复 `.github/workflows/delivery-report.yml`：`workflow_dispatch` 永远不满足旧的
  `pull_request` 条件，评论步骤是死代码；新增 `pr-number` 输入。

**TS 前端（三阶段全部完成）**
- 协议补齐：`turn.cancel` → `turn.cancel.result`、计划审批（`approval.request.kind="plan"`）、
  `prompt.request/response`（命令内提问）、`session.select`、状态字段与审批 `advisories`；
  宿主把 `rich.Prompt` 与 `selector.ask_choice` 桥接到协议（`/resume`、`/permissions`、`/model` 可用）。
- 宿主修复：加载 `models.yaml`（此前只读 credentials，模型解析到不同端点）；协议 stdout 只走帧，
  其余输出改为 stderr；`run_kernel_turn` 返回 `None` 时发 failed 帧而不是空 phase。
- 纯对话在宿主里走 direct 路径（此前被送进内核，问候被只读策略否决并空转）。
- `xenon --ui ts|python`（`XENON_UI`）：有 `frontend/dist/cli.js` 时默认 TS，`--ui python` / 无构建产物回退。
- **步骤 4：删除 Python TUI 的渲染层。**
  - 删除 `completer.py`、`command_catalog.py`（命令面板/MRU）、`selector.py` 的补全菜单与按键绑定、
    `status_bar.py` 的 PT fragment 渲染、`theme.DEFAULT_PT_STYLE`；`prompt-toolkit` 从依赖中移除。
  - `repl.py` 不再持有 PT 会话：输入统一走 `repl_input`；`_pt_session`、`_init_prompt_toolkit`、
    `_install_input_lower_rule`、`_read_input_pt` 全部删除。
  - 回退编辑器补上失去的能力：`↑/↓` 历史（`xenon/repl/input_history.py`，持久化到
    `~/.xenon/input_history.txt`）；`XENON_NO_PT` 配置项随之删除。
  - `direct_chat` 的 Rich `Live` spinner 移除（重绘会干扰协议流与编辑器光标）。
  - 回退模式现在没有固定底栏、补全菜单和命令面板；这些由 TS 前端承担，`docs/TUI.md` 已重写。
- **收紧入口**：`xenon` 缺少 `node` 或 `frontend/dist/cli.js` 时报错退出（code 1）并打印
  `npm install && npm run build`，不再静默回退到内置编辑器；管道输入（非 TTY）同样报错并指向
  `xenon exec`。`--ui python` / `XENON_UI=python` 保留为显式 opt-in。
  `--resume` 与 `xenon resume` 也改走 TS（`--resume` 传给宿主，启动后执行一次 `/resume`），
  不再绕过默认界面。

**发布物与前端收口（2026-11-11 续）**
- **打包**：`frontend` 用 esbuild 生成单文件 `dist/xenon-ui.js`（Ink/React 内联，不依赖 `node_modules`）；
  `scripts/build_frontend_assets.py` 把它暂存进 `xenon/frontend/assets/` 并随 wheel 分发；
  CI `package` job 构建前端、打包后断言 wheel 内含该文件。`_ts_frontend_script` 优先解析包内 bundle，
  其次是源码检出的 `frontend/dist`。干净 venv 只装 wheel 即可启动 TS 界面（需要 Node.js 20+）。
- **首次运行**：`ready` 帧带 `setup_needed`（只读本地凭证与已配置模型，不探测网络）；
  TS 在状态栏下提示“尚未配置 API Key · 输入 /setup”。
- **`@` 文件补全**：新增 `files.request` / `files.result`；宿主在工作区内有界列举
  （跳过 `.git`/`node_modules` 等，最多 50 条，只列名字不读内容），TS 用 `↑/↓` 选择、`Tab` 补全。
- **Shift+Tab**：新增 `/mode next|cycle`，前端快捷键与命令走同一条路径。
- **修复**：`session.select` 分发的命令名缺少前导 `/`，恢复会话一直返回“未知命令”；
  宿主启动时的 `--resume` 也因此第一次真正生效。

**修复：真实任务暴露的三个执行缺陷（2026-11-11 续）**
- **子进程继承宿主 stdin（Windows 挂起）**：主线程阻塞在 stdin 读帧时，任何继承该句柄的子进程
  都会在启动阶段挂住——真实任务里的 `pytest` 一行输出都没有，工具 60s 超时、轮次被判 stalled。
  代理执行层的 spawn 点（`tool_node`、`test/git/github/script` 工具、catalog gateway/worktree）
  显式传 `stdin=subprocess.DEVNULL`；hooks 通过 `input=` 自带管道，天然隔离。
  新增 AST 静态守卫防止回潮。
- **`test` 工具忽略模型给的 `command`**：`command` 被通用别名映射成 `action`，`test` 却没读它，
  于是 `python -m pytest` 不带路径、跑整套测试。现在 `action` 里的 `pytest` / `python -m pytest`
  会被解析成 pytest argv，其他命令按原样执行。
- **直连对话不解析 `-m` 别名**：内核路径会解析，direct 路径把 `deepseek-v4-pro` 原样交给 provider
  层，报“model_id 必须为 provider/model_name”。`_canonical_model_ids` 统一成注册表里的规范 id。

真实任务验收（DeepSeek 官方端点）：TS 界面 → 协议 → 内核 → 读文件 → 打补丁 → 验证审批 →
`pytest` 通过 → `证据 verified · finalized · 已交付`；同一任务在协议宿主直连路径同样
`accepted: true / evidence: verified`，`xenon report` 显示两条 claim 均 bound。

**外部审计：区分「冲突」与「无法核验」（2026-11-12）**
- 工作区之外（或没有 VCS）的声明不再计入 `contradicted`：这类声明既不能证实也不能证伪，
  现在标记为 `out_of_scope`。整体状态按「有冲突 → contradicted；有无法核验 → partial/unverified；
  有未声明改动 → partial；否则 verified」判定。
- `--strict` 的口径不变：只在 `contradicted` 时退出 1；无法核验不会让门禁失败。
- `to_report` 增加 `claims[].status = out_of_scope`、顶层 `out_of_scope` 与
  `delivery.out_of_scope_paths`；`render_audit` 把「与工作区冲突」和「无法核验」分成两节。

**MCP server：把 Xenon 接入其他 Agent（2026-11-12）**
- 新增 `xenon mcp-serve`（`xenon/mcp/server.py`）：JSON-RPC 2.0 over stdio，暴露三个工具——
  `xenon_run_task`（内核任务 + 账本）、`xenon_evidence`（读账本）、`xenon_audit`（外部审计）。
- 权限 fail-closed：默认 `read-only`，单次调用只能降低、不能超过服务器上限；workspace 默认限制在
  服务器根目录内（`--allow-any-workspace` 可放开）；需要人工审批的调用直接失败并说明原因，不挂起。
- stdout 只承载协议，诊断走 stderr；`--permission` 的默认值按子命令区分（`exec` 仍是 `auto-edit`）。
- 真实 stdio 验收：`initialize` 协商 2025-06-18、`tools/list` 3 个、`run_task` finalized/12.3s、
  `evidence` finalized/verified、`audit` partial（0 contradiction / 10 out_of_scope）。
- 文档：新增 `docs/MCP_SERVER.md`；README 增加接入说明；ARCHITECTURE 模块树更新。

**缓存 TTL 实测**
- `docs/evidence/cache-ttl/probe.py` 直连 DeepSeek 官方端点：前缀在 5 分钟内命中 512 tokens；
  **无中间命中的对照在 300 秒与 900 秒后仍命中**，说明 5 分钟 TTL 不适用于 DeepSeek，
  至少 15 分钟内的停顿不会失效。"命中是否延长 TTL"仍未跨过 TTL，证据见该目录 README。


**首次运行闭环与命令面板（2026-11-12 审计）**
- **`/setup` 在默认界面里真正可用**：向导的 API Key 输入（`_masked_input`，逐字符 `*` 掩码）
  原本直接读进程 stdin；TS 前端下终端归 Node 所有，这一读既拿不到按键也会挂住，
  而首次运行提示恰好指向 `/setup`。宿主现在把 `setup_wizard._masked_input` 一并桥接到
  `prompt.request(secret=true)`，并随命令结束还原补丁。
- **命令面板补回缩写过滤**：TS 的 `completeCommand` 原本只做前缀匹配，README/TUI 承诺的
  “`prm` → `/permissions`” 并不成立。现在按「精确 > 前缀 > 子序列」排序，同分保持目录顺序。
- **`Enter` 补全，不直接执行**：真实 ConPTY 验证发现 `/e` + `Enter` 会运行目录里第一条匹配
  `/exit`——等于误退出。现在 `Enter` 只把高亮项补进输入框，再按一次才执行；输入已是完整命令名时
  始终运行用户键入的那条。
- **退出不再抛栈**：`KernelHost.shutdown` 先置 `closed` 再关闭 stdin，迟到的帧（文件列表、状态）
  不会再写进已结束的流，消掉 `ERR_STREAM_WRITE_AFTER_END`。
- **自进化双跑验证通过**（最后一项待办）：真实 ConPTY + TS 前端两轮，账本
  `lesson_proposed` / `lesson_stored` → `lesson_applied`（同指纹），项目记忆里有 `lsn-…` 记录。
  验收脚本现在**循环拒绝每个审批面板**：模型会把同一条命令改成 `test` 再问一次，
  旧脚本只按一次 `4`，第二个面板一直等，看起来像审批卡死。


### 审计修正批次：清死代码、账本分支、补齐文档缺口（2026-11-11）

**清死代码（阶段 1）**

- 整个 REPL 发布闸门不可达（`_render_engine_result` 零调用方 → `_verify_turn` →
  `TurnGate.evaluate` 只互相调用）：删除 `turn/gate.py`、`engine/task_verifier.py`、
  `engine/hollow_detector.py`、`turn_helpers.turn_status_from_result`、`_render_engine_result`、
  `_verify_turn`、`_last_gate_verdict`、`_turn_gate`；`task_state.py` 保留（仍用于 legacy 续接）。
- `engine/evidence_gate.py` 只保留 `GateVerdict`（`tool_executor.before_tool` 仍使用）。
- 删除三份零调用的 MCP 提示词格式化与只写不读的 `_mcp_tools_list`（catalog 是唯一通道）。
- 删除 `engine/trace.py`（账本即 Trace）与 `tool_executor` 里永远拿到空 id 的过滤器。
- 删除 `engine/checkpoint_manager.py`、`_get_checkpoint_manager`、`_confirm_checkpoint`、
  `engine/context.py` 的事实/钩子接缝、`register_evidence_gate`/`evidence_gates`。
  工具执行期的 `set_tool_checkpoint_callback` 是另一条活路径，保留。
- 内核渲染规则：`accepted` 但 `phase != FINALIZED` 时标题显示"已阻塞/已中止/失败 · 证据状态"，
  不再出现"停止的运行被标成完成"；partial/unverified 结果附"待人工核验"披露面板（不是闸门）。

**权限（阶段 2）**

- 删除内核的封闭测试命令白名单（pytest/npm/go/cargo/mvn…）：所有命令走统一审批，
  有通道就问、"a" 记住 30 分钟，无通道拒绝。VERIFY 与 SHELL 一样受破坏性硬规则约束
  （`pytest …; rm -rf .` 不再只是"问一下"）。因此无 TTY 的 `xenon exec` 跑测试命令需要
  显式 `--permission bypass`。

**账本（阶段 3-5）**

- **单写者**：`ledger/store.py::open_shared` 按规范化路径共享同一 `Ledger` 实例（弱引用 +
  任务 id 校验）；`append` 在文件被别的进程推进时先重新加载（`_refresh_tail_if_moved`），
  两个句柄不再产生重复 `seq` / 分叉 `prev_hash`。`runner` 与 `child._open_parent_ledger` 改用它。
- **子进程封存分支**：`child_started.branch` 记录 `task_id/ledger/base_seq/base_hash/depth`；
  `child_finished`/`child_interrupted.branch` 记录父进程亲自验证后的
  `head_seq/head_hash/event_count/sealed`（读不到就 `sealed:false`，不编 head）。
  新增 `xenon/ledger/tree.py::verify_tree`；`report.build_report` 增加 `branches` 段。
  没有 `next[]`：回写前驱会破坏 append-only。
- **崩溃恢复接线**：`_runtime_bundle` 打开账本后 `recover_truncated()`，隔离半行为
  `.corrupt-*` 并写 `ledger_repaired` 事件 + debt；无法证明是哪次调用中断，因此只标任务级，
  不编造单条回执。
- **迁移是显式命令，不是启动钩子**：legacy 会话日志仍是 `/resume`、`/evidence`、`/rewind`
  的活存储；每次启动扫描会把仍在写入的数据复制成"一会话一账本"（真实环境实测 25k 文件
  ≈ 165 秒/次，且随会话数增长）。启动不再调用 `migrate_legacy_sessions()`，改为一次性命令
  `xenon import-legacy`，在该存储真正退役时执行。导入仍幂等，目标仍是独立的 `ledger/legacy/`。

**意图事件（阶段 6）**

- `TaskSpec.intent/intent_source` 由 REPL 传入，运行时在 intake 写 `intent_classified`
  事件（intent/source/mode）。

**保留策略（阶段 7，默认关闭）**

- `xenon/ledger/retention.py`：只归档（移动）过期子账本到 `ledger/archive/<date>/`，
  不删除；主账本永不自动清理；被分支封存引用或仍有未闭合 `child_started` 的不移动。
  配置走统一入口 `get_config().retention`（`retention.enabled` / `children_days` /
  `max_total_mb`）；环境变量 `XENON_RETENTION_ENABLED` / `XENON_CHILDREN_DAYS` /
  `XENON_LEDGER_MAX_MB` 优先级更高。

**统一错误信封（阶段 8）**

- `xenon/utils/errors.py::envelope(exc)` 按行为分类（permission/network/llm/tool/integrity/
  internal/cancelled）+ code + retryable；CLI 与协议 `turn.end`（新增 `error_kind`/`retryable`）
  使用同一份。`_gs_check.py` 移入 `docs/evidence/obligation-progress/probe.py`。

**对齐复核（第二轮审计）**

- 启动路径回归修复：legacy 迁移移出启动（见上），REPL 冷启动不再做全量历史扫描。
- `docs/ARCHITECTURE.md` / `.en.md` 的模块树补上 `kernel/`、`ledger/`、`catalog/`、`harness/`、
  `frontend/`，`session/` 明确标注为 legacy；英文版过期的"TurnGate 仍在运行"说明改为已删除；
  行数/工具族计数按当前代码校正。
- 保留策略不再自读环境变量，统一走 `get_config().retention`（一条配置通路，不是两条）。
- `max_total_mb` 从"写了但没用"变成真规则：超预算时按最旧优先归档（仍绝不删除、不动已封存分支）。
- 删除零写入的事件枚举 `TOOL_REQUESTED`（并修正引用它的过期注释：崩溃定位到 `TOOL_CALL` 转移，
  `call_id` 由网关在执行时生成，无法在决策前写入）。
- 补齐 `kernel/runtime.py` 漏掉的 `ModelView` 导入（F821：注解引用了未定义名）。
- `ruff check xenon tests evals scripts`（与 CI 同一条命令）重新归零：删除未使用导入、
  整理导入顺序、修 `callbacks.py` 的 E402、修一个 docstring 的无效转义警告。
- README 版本号与 `CLAUDE.md` 的版本/测试数按实际值校正（版本单一来源是 `xenon/__init__.py`）。

### 全量审计（代码 / 测试 / 死代码 / 文档）与修复批次（2026-11-11）

五路并行审查（内核+harness、repl+catalog+utils、测试有效性、死代码与遗留、文档与架构不变量），
所有结论逐条复核后修复：

**权限与会话授权**

- **SCOPED 的写盘问题被审批后端自动答掉**：审批 UI 里"工作区内写工具免问"的旧捷径先于
  面板返回 allowed-once，`/permissions default` 实际等于 `accept_edits`。内核发起的审批
  现在传 `adjudicated=True` 跳过该捷径；旧捷径只服务 legacy 执行器回调。
- **`accept_edits` 反而更严**：审批通道此前只在 EXECUTE 打开，同一 `pytest` 在
  `accept_edits` 被硬拒、在 `default` 会询问。现在非只读模式一律有通道。
- **`bypass` 的 push 被静默放行**：bypass 快捷返回在 push 规则之前。push 规则现在先于
  任何授权与 bypass（有通道询问、无通道拒绝）；`/permissions` 描述同步为
  "破坏性命令仍拒绝，push 仍需确认"。
- **链式/后台/重定向可借授权提权**：`_CHAIN_MARKERS` 缺 `&`、`>`、`<`、换行，且
  `*` 通配在链式检查之前返回 —— `sleep 1` 的授权可放行 `sleep 1 & rm -rf x`、
  `echo hi > /etc/passwd`。现补全标记，且**通配授权也不能覆盖链式命令**。
- **ALLOW 授权没有截止时间**：`grant_from_choice(ALLOW)` 不写 `expires_at`，推给运行中
  策略就没有 TTL。现在授权自带 30 分钟期限（面板用 book 的 TTL），推入策略时用
  `add()` 返回的已计时副本。
- **模式名回显漂移**：`default`/`accept_edits` 共用级别，反向映射总是返回 `default`，
  选完 `accept_edits` 再回车会静默切档。回显改为以持久保存的模式名为主。

**内核与 harness**

- **外层轮次未重置计划/教学预算**：`_begin_outer_round` 现在同时重置 `_plan_rounds`
  与 `_loop_taught`，否则重试轮无法 replan、首个小重复就被 force-finish。
- **折叠水位越过实际叙事窗口**：一次折叠 >80 项时只叙事最后 80 条，水位却推进到全部，
  最早的折叠事件永远不会被叙事。水位现在只覆盖实际送出的窗口。
- **并行调用簿记跨回复残留**：`_skipped/_answered` 只在 `_note_parallel` 里清空，而无工具
  调用的回复会提前返回 → 上一回复的丢弃调用被当作本回复重报、状态答案重复入账。
  现在按每次调用清空。
- **resume 丢失会话归属**：`resume_task` 从既有账本的 `task_started` 读回 `session_id`，
  恢复后的任务仍能查到本会话历史。

**脱敏与工具发现**

- **日志与工具结果存储的敏感键集合不一致**（一个漏 `refresh_token/client_secret/cookie/
  private_key`，另一个漏 `credential`）：合并为 `utils/redact.py::SENSITIVE_PARAM_KEYS`。
- **合并后的密钥阈值回退**：`sk-` 从 12 字符抬到 16 且丢了 `ak-`，12–15 字符的密钥会进入
  记忆。恢复为 `(?:sk|ak)-…{12,}`。
- **`find_tools` 未知 capability 静默**：返回空集且无说明，模型无法区分"过滤器不存在"与
  "没有匹配工具"。现在 receipt 的 `note` 明说过滤器无效。

**测试审计**

- 重写两处"自证式"测试：一处先写入授权簿再断言授权簿、一处把产品的外层 try/except 抄进
  测试再断言自己写的消息。它们不调用任何产品路径，删掉产品逻辑也不会红。
- 删除 `test_file_claim_recursive_react_exception_placeholder`（同上，兄弟用例已覆盖真实路径）。
- `tests/test_checkpoint_integration.py` 与检查点毫无关系（11 个 PartialContent 用例），
  更名为 `test_partial_content.py`。
- `tests/test_llm_classifier_live.py` 五个只打印不断言的"live"用例：正则用例改为断言（并
  真的失败过一次），其余四个显式 `skip(reason="live model probe")`。
- `test_memory_v2_status` 实际调用的是 v1 命令且只断言 `isinstance(str)`；改为真正调用
  `_cmd_memory_v2`。
- `test_f3_compactor` 两处 `assert 0.6 < mock 的 0.7 < 0.85` 属自证断言，删除。

**文档更正**（此前被标记"已实现"但代码不支持）

- `llm_intent` 事件从未存在：分类结果参与契约判定但**没有入账**。
- `report.py` 输出的是 `schema`/`schema_version`，没有 `version` 字段，也没有启动迁移。
- `external.py`/`report.py` 行数、sandbox 行数（68 行进程内检查）、崩溃恢复
  （`recover_truncated` 无生产调用）与迁移脚本（`migrate_session_jsonl` 无生产调用）的
  状态改为准确描述。

### 跨轮上下文改为按需拉取 · 外部审计适配器化 · 脱敏合并（2026-11-11）

- **§3.2 跨轮上下文（pull，而非 push）**：不再把"最近 5 条摘要"预置进每轮
  prompt。`get_state` 新增 `session` 查询：只读、零模型，按**会话 id**（不是
  ledger 目录——每个项目共用同一目录）从各自账本投影最近 5 个任务，一行一条
  （任务 → 证据状态 · 结论）；本轮消息只增加一行恒定指针，告诉模型"有历史、
  可用 get_state what=session 取"。任务内部的折叠叙事仍主动推送（纯 pull 在
  真实任务中失败过，见缺陷 4）。新增 `kernel/session_history.py`（有界读取
  文件头尾、容忍坏行）、`TaskSpec.session_id` 贯穿 runner/adapter 并记入
  `task_started`；StateRead 照旧消耗一步（已确认可接受）。
- **§11.4 外部审计适配器化**：`ClaimAdapter` 协议（`name` / `sniff` / `read`），
  按**记录格式**识别而非按 Agent 名；显式 `--agent` 优先且未知名字不回退嗅探；
  识别不了时**不解析任何声称**、`agent=unknown`、notes 说明原因，且不列
  `unclaimed_paths`（读不了的日志不能反过来构成指控）。`ExternalAudit.agent`
  取适配器自报名，不再硬编码。
- **§11.7 脱敏合并为一份实现**：`xenon/utils/redact.py` 统一报告、MCP 日志与
  记忆写入（此前三份各自枚举、互相漏）；补环境变量凭证赋值（保留变量名）、
  Bearer/Basic、GitHub/AWS/OpenAI/Anthropic/Google/Slack 形态、整块私钥、
  敏感查询参数、中文标签，普通文本不动。
- 真实验证：REPL 冒烟脚本 `docs/evidence/repl-smoke/drive_repl.py`——plan 模式
  写盘被 VETO 且模型如实报告、bypass 模式真实修复并跑测试（verified）、
  `/sub-agent` 只读委派成功；新增用例见 `tests/kernel/test_session_history.py`、
  `tests/test_external_audit.py`、`tests/test_redact.py`。
- 真实运行抓到**四个**缺陷并修复（前两个在会话历史链路，后两个让模型根本收不到历史）：
  1. **会话历史查询性能**：真实 REPL 账本目录有 3805 个账本，逐文件
     `Path.resolve()` + 全目录扫描使每次查询耗时 ~1.6s。改为规范化路径比较
     （不碰磁盘）、只扫描最近 40 个候选，实测降到 ~67ms（会话目录 ~1ms）。
  2. **`get_state` 被写成 `call_tool` / 写进计划时无法作答**：模型会发出
     `{"action":"call_tool","capability":"remote","params":{"tool":"get_state"}}`，
     没有任何工具暴露 remote 能力 → 绑定两次失败；也会把它作为**计划步骤**
     （计划步骤只能绑定目录工具，必然失败）。现在：前者由 harness 识别写法直接
     作答；后者把该步骤降级为无意图的 inspect 步骤，交给 actor 自己动手。终局
     动作不是工具，内核仍然不认识任何工具名。
  3. **步执行路径的当轮消息缺少尾部上下文**：默认「先规划、再按步执行」走
     `build_step_messages`，它自己拼消息，**既没有折叠叙事、也没有会话历史指针**
     ——模型从头到尾不知道有 `get_state`，只能去翻账本文件。现在 `current_turn`
     与步执行共用同一个 `_tail_context`，两条路径不会再漂移。
  4. **策略丢弃步内的 `StateRead`**：步执行子循环只放行
     `Think/AskUser/Abort/CallTool`，模型发出的 `StateRead` 被丢弃并「释放步骤」，
     答案渲染后直接扔掉。现在 `StateRead` 放行，运行时记录 `STATE_READ` 并把
     答案带入后续轮次。
  - 端到端真实验证（`docs/evidence/session-history/probe_turn.py`，抓真实请求）：
    指针进入步执行消息 → 模型调用 `get_state what=session` → 账本记录
    `STATE_READ`（含 earlier_tasks）→ 最终答案正确。

### 修复 /sub-agent：权限误读与生命周期丢记（2026-11-11）

- **子任务一直起不来**：`/agent` 读不存在的 `repl._execution_level`（恒 0）→ 父
  权限恒 READ_ONLY，而子任务默认 AUTO_EDIT → 子集检查抛错。现在级别从
  `agent_context` 读取并尊重 `_permission_mode` 上限；委派按架构约定只读
  （新增 `child_permission_for` = `min(parent, READ_ONLY)`），显式传入 sub 权限。
- **子任务生命周期从未写入父账本**（配合上轮审查发现）：父账本在
  `ledger/repl/`，此前按 `ledger/children/<id>.jsonl` 猜测导致
  `child_started/child_interrupted/补记` 静默丢失；`/agent` 现传入
  `repl._last_kernel_ledger` 的真实路径（`parent_ledger` 参数贯穿 child API）。
- 验证：新增 /agent 接线回归测试（子任务只读、不再抛错）+ `child_permission_for`
  边界测试；真实模型 + 子进程 + 只读权限的委派任务正确完成。全量 4102 passed。

### 授权与权限模式产品决策落地（2026-11-11）

- **「全部允许（a）」跨轮持久化**：`grant_from_choice(ALLOW)` 不再打当前轮戳记
  （30 分钟 TTL 跨轮有效）；shell/verify decision 无路径目标时以**命令文本**作为
  执行路径；**前端授权簿为唯一记忆**——删除 kernel `last_grant` 回写通道，
  内核只把授权作用于本次运行（`_remember_grant` 补上命令目标兜底）。面板在
  给出答案时即写入记账簿（approval_ux，既有路径），重复选择同一模式不再清空。
- **`/permissions` 四档语义成真**：所选模式成为**会话级持续上限**，`turn_flow`
  每轮把分类结果 clamp 到上限；`default` → `PermissionMode.SCOPED`（写盘仍询问，
  新增策略分支）、`accept_edits` → `AUTO_EDIT`、`plan` → `READ_ONLY`、
  `bypass` → 真正 `BYPASS`。SCOPED 时审批通道始终打开；`bypass` 描述补充
  "push 与破坏性命令仍拦截"。
- **否定句转为受保护路径**：intake 不生成变更义务；`execution_policy` 的否定
  提取补齐英文（`don't change x.py`）、紧凑中文（`别改 x.py`）、后置否定
  （`保留 x.py 不变`），ASCII 逗号/分号纳入分句；修复「保留…不变」路径在
  match 内部时的提取。
- **补齐写句式低判**：`修复 util.py` / `fix util.py` / `把 util.py 改了` 判为
  写请求（原先 READ_ONLY 会导致编辑被静默 VETO）；`检查 util.py` 仍为只读。
- **find_tools 零匹配**：保留按名排序的候选并在 `ToolDiscovery.note` 与视图
  中明确提示"无匹配，换关键词"。
- `GrantBook.active()` 跳过轮内授权（子任务不继承「本轮允许」）；`/permissions
  remove` 按 `cap: target` 规范化比较。

全量 **4100 passed**；真实 API 任务 finalized/verified；内核纯度守卫与文档引用
守卫通过。

### 全量 diff 审查与修复批次（2026-11-11）

三个独立审查员分组深读未提交 diff（内核 / harness+catalog / REPL+授权），
所有发现经逐条复核后修复：

- **native 工具 schema 形状**：`_native_tools` 改发 `parameters`（OpenAI 兼容
  通道唯一识别的字段；此前 `input_schema` 被静默丢弃，函数到达端点时没有参数
  表），移除非标准 `capabilities` 字段；MCP/插件规格无 `json_schema` 时从散文
  参数生成宽松 schema，不再空表。
- **native REMOTE 调用参数形状**：`action_from_tool_call` 把业务参数包进
  `tool_args` 并注入 `tool_name`，与文本协议统一；此前业务参数被 ToolNode 吞掉
  或 TypeError，真实 MCP 调用必然失败。
- **拒绝/绑定失败对模型可见**：`binding_failed`、`plan_updated` 加入当轮事件
  白名单（此前计划被拒后模型看不到原因，重复提交直到预算耗尽）。
- **覆盖率与 V2 口径一致**：`_refresh_obligations` 构造 Claim 时透传
  `expect`（此前 get_state 说 satisfied 而审计判 UNRESOLVED）。
- **授权形状的换行注入**：`_command_shape` 在 `#` 注释截断前拒绝 `\n`/`\r`
  （此前授权 `python -c "..."` 后接 `#\nrm -rf` 会放行第二行）；清理重复
  `_normalize`。
- **plan 模式 finisher/reflect 恢复证据**：finish/reflect 消息重新携带
  （折叠预算内的）`working_set`、debt、obligations——finisher 无法调工具，
  零证据合成会产出全 abstain 或 CONTRADICTED；reflect 同理。
- **子任务生命周期真实落账**：`parent_ledger_path` 支持显式父账本路径，
  `/agent` 传入 `repl._last_kernel_ledger`（父账本在 `ledger/repl/`，此前按
  `ledger/children/<id>.jsonl` 猜测，child_started/finished/incomplete 与崩溃
  补记全部静默丢失）。
- **折叠记录重写**：只摘要自上次摘要后的新增折叠事件（此前签名随丢弃集合每轮
  变化 → 压缩区间内每轮一次模型调用且提示词随历史二次方增长）；摘要输入上限
  80 条、输出 1024 token、视图 clip 放宽到 8000 字符；`CONTEXT_COMPACTED`
  改从最终交给模型的视图记录（此前重建时 `_compact_arm` 仍开，ledger 记录的
  折叠集 ≠ 模型实际所见）。
- **恢复执行能力补齐**：`resume_task`/adapter.resume/`resume_kernel_plan` 透传
  lesson_store、summary_provider、session_grants 并设置新轮次 id（批准计划
  执行不再丢失学习、折叠叙事与已记住的授权）。
- **intake 分类器修正**：outcome 词边界（`password` 不再命中 `pass`）、
  中文新建动词（创建/新建/生成/写入）、描述性短语边界（`described in BUG.md`
  不再生成变更义务）、测试文件按命名约定识别（`latest.py`/`contest.py` 不再
  误判）、前后距离对称。
- 观测口径：`metrics["steps"]` 与状态栏改为任务累计步数；`/permissions
  remove` 接受复制自列表行的 `cap: target` 写法。

全量 **4089 passed**；真实 API 任务 finalized/verified；文档引用守卫通过。
**仍未处理（需产品决策）**：授权 `a` 跨轮持久化归属、`/permissions` 四档语义、
intake 否定句策略、`find_tools` 零匹配行为。

### 旧引擎遗留件审计（缺陷 1/7）

- **逐文件导入审计完成**（`scripts/legacy_import_audit.py` 可复现），结论见
  `docs/design/legacy-retirement-plan.md`。核心发现：`engine/` 不是一整块遗留
  ——内核网关内部包着 `ToolExecutor`，所以 12 个文件是两代路径共用的活跃基础
  设施（context / callbacks / tool_tracker / tool_runtime / tool_profiles /
  tool_gate / circuit_breaker / checkpoint_manager / scheduler / task_recipe /
  trace 等），真正的遗留是 TurnGate 耦合单元与旧证据链。
- 四阶段退役计划：①旧压缩小件（budget/structured_compactor，真实依赖仅一个
  类型枚举 + 两个助手函数）→ ②evidence_runtime legacy 分支（先迁 `/evidence`
  命令）→ ③TurnGate 单元整体退役（前提：direct chat 也走内核）→
  ④evidence_contracts 迁入 nodes/。删除门槛：零生产调用方 + 替代者真实任务
  验证 + 全量测试绿。
- **阶段 1 已执行（2026-11-10）**：删除 `engine/budget.py`（`BudgetManager`
  零运行时使用者；`BudgetPhase` 内联进 `context_strategies`）与
  `engine/structured_compactor.py`（两个助手迁入 `context_manager`，逻辑逐行
  保留；`tests/test_f2_budget.py` 随删）。-324 行，全量 4120 passed。
- **阶段 2 已执行（2026-11-10）**：删除 `engine/evidence_runtime.py`（737 行，
  旧 L0 哈希链）。审计澄清：`/evidence` 命令早已不读它（读 `session/events.py`
  与 kernel `ledger/`）；真实写入方仅 executor 的 `_ledger_event`（网关本就
  关闭）、MCP 注册表 `context.evidence` 钩子、会话保存快照——全部随之移除
  （`AgentContext` 证据门面、executor 的 `record_evidence`/`evidence_ledger`
  参数、MCP 三处钩子、`/save`/自动保存/恢复的快照与回填）。FactBindingGate
  盲编辑拦截（基于 tracker）保留。删 4 个子系统测试、重写
  `test_online_evidence_gate.py`、清 MCP 两处旧断言、`docs/EVENT_SOURCE.md`
  标记历史归档。全量 4088 passed；真实 API 任务 finalized/verified；文档引用
  守卫通过。
- **阶段 4 已执行（2026-11-10）**：`evidence_contracts.py` 由 `engine/` 迁入
  `nodes/`（`git mv` + 8 处导入改写，无逻辑改动；契约属于工具结果解析层）。
  全量 4088 passed；真实 API 任务 finalized/verified。仅剩阶段 3（TurnGate
  单元，需先让 direct chat 走内核循环）。

### 权限单一裁决权威（缺陷 2）

- **根因**：内核路径中同一次调用被两处裁决——`PermissionPolicy` 依 grants/规则/
  mode（信息更全）放行后，executor 的 Stage 1.5 `execution_policy_denial` 按回合
  级别二次拒绝，甚至弹第二个升级确认。典型触发：EXECUTE 回合中人类授予的 shell
  「允许」grant，在后续 WRITE 回合被再次拒绝。
- **修复**：内核已裁决的调用（`_kernel_adjudicated`）跳过 Stage 1.5 的级别检查
  （与既有 Stage 1.6 审批抑制同理）；`execution_policy_denial` 新增
  `enforce_level` 参数，**受保护路径检查始终生效**——那是用户显式"不要改动"约束，
  不是权限裁决。legacy 调用方（无内核）语义不变。
- 验证：新增 `tests/kernel/test_single_decision_authority.py`（端到端：WRITE 级
  回合复用 shell grant，真实执行无二次拒绝）+ `test_execution_policy.py` 3 个
  用例；真实 API 冒烟任务 finalized/verified。全量 **4144 passed**。

### 架构文档与代码对齐（缺陷 8）

- `docs/ARCHITECTURE.md` 版本号 v0.9.2 → **v0.9.8**；capability **7 类 → 8 类**
  （新增 `discover`），补充 `find_tools` 只读发现器说明。
- 新增**【双轨现状】**标注：第 1–3 节的 `TurnGate / HollowDetector /
  turn_flow` 是仍在运行的 legacy 路径（REPL 经 `should_route_to_kernel` 分流），
  第 0 节 `LoopRuntime` 是单循环内核与收敛目标——即缺陷 1 三轨并存，不再让读者
  误以为其中一套是唯一控制流。
- 英文版 `docs/ARCHITECTURE.en.md` 加显著滞后警告：引擎注册表已移除、
  `xenon/modes.py` 为模式唯一来源；原版本仍描述已删除的"7 引擎"。
- 经核对，文档引用的模块路径（session/turn/engine/nodes 等）真实存在，漂移集中
  在版本、capability 数量与双轨语义，而非编造路径。纯文档变更，无代码影响。

### 工具发现器（缺陷 6）

- **落地**：新增闭合能力 `Capability.DISCOVER`、`EffectKind.TOOL_DISCOVERY` 与
  只读核心工具 `find_tools`。目录按结构特征确定性打分（精确名 > capability
  命中 > 词重叠），返回有界候选（默认 5）及完整参数但不执行；DISCOVER 在所有
  权限模式（含 read-only）下免费放行。模型一步选对工具、看清参数。
- **真实 API 验证抓到两个 fail-open 缺陷**：真实模型面对晦涩工具时先调
  find_tools，但随后 11 次 REMOTE 绑定失败、12 步 blocked。根因：命名
  REMOTE 调用未把函数名注入 `params.tool_name`（现自动注入）；非法 capability
  过滤器被当"无过滤"忽略（现返回空）。修复后 find_tools → 正确工具、3 步
  finalized。
- 证据：`docs/evidence/tool-discovery/real_discover_probe.py`；新增
  `tests/kernel/test_tool_discovery.py`（12 用例）；更新三处闭合集合守卫。

### 双摘要压缩（设计文档 §13 决策一 / 缺陷 4）

- **落地**：折叠在确定性 `elided` 摘要（事实骨干，seq/status/tool/why）之外，
  新增 `SummaryProvider` 协议与 harness 实现 `LLMSummarizer`（新文件
  `harness/summarizer.py`），用当轮模型生成延续叙事并追加 `CONTEXT_SUMMARY`
  事件；当轮重建视图。叙事只压缩、不产生新事实，确定性摘要并列在场防幻觉。
- **真实测试抓到关键集成缺陷**：叙事最初仅持久化、未进入模型当轮 prompt（pull
  架构下需 get_state 拉取，模型没拉），紧窗口任务把 mod_1 读取折叠后反复重读
  mod_1 六次而不推进。修复为在 `current_turn` 中**主动推送**最新折叠叙事（事件
  之后、消息末尾，缓存前缀不受影响）；修复后真实任务按序读完全部 8 个文件、14
  步收敛并产出正确的 64 helper 报告。
- **视图契约**：`_working_set` 增返折叠条目；`ModelView` 新增 `folded_items`；
  `get_state` 新增 `summary` 通道；`EventKind.CONTEXT_SUMMARY`；顺带接上从未被
  调用的折叠记录（现由 `_record_fold` 统一处理）。
- 证据：`docs/evidence/dual-summary/real_fold_probe.py`；新增
  `tests/kernel/test_dual_summary.py`（11 用例）。

### 义务生成契约校准（设计文档 §1.5 / 缺陷 10）

- **背景**：缺陷 10 被文档标为最高优先级——错误的验收契约会生产虚假保证。
  首版动词分类修复已存在，但真实句式审计发现四个边角缺陷：无支配动词时
  默认硬编码 write（"difference between a.py and b.py?" 错误生成两条变更
  义务）；文件名中的点被当作句号截断分句（并列列表第二个文件丢义务）；
  中文后置动词（"shapes.py 需要修复"）被漏判；连接词后的不及物动词
  （"c.py then run tests"）反向管辖前一文件。
- **修复**：`harness/intake.py` 的 `_classify` 重写为同时扫描文件名前后两侧
  的当前分句（前 64 / 后 32 字符，按句末标点划界，拉丁句号仅在后跟空白时
  断句），取距离最近的支配动词；两侧均无动词归 read（不生成义务）；新增
  顺序连接词（then/然后/接着/之后）阻断后向管辖；补充 "needs work"/"work
  on"/裸"修"等写类表达。删除被取代的 `_last_position`/`_last_suffix`。
- **真实 API 复现原始缺陷**：预置 add() 做减法的 shapes.py，执行
  "Fix it so test_shapes.py passes"，模型正确只改源码、2 个测试全过、
  finalized/verified、gaps=0——原场景曾必然 blocked。
- `test_v2_derivation.py` 新增 6 用例（共 13）。全量 **4117 passed**。

### 外层任务循环（设计文档 §4.3）

- **落地**：`LoopRuntime.run()` 与 `resume_planned()` 的两处重复循环统一为
  `_run_outer()`：最多 3 轮，跨轮保留账本、债务、计划、已验证证据与授权，
  每轮经 `_begin_outer_round()` 刷新轮内状态（步数、repair/replan/audit
  计数器、轮 deadline、卡死检测器、forced-finish）。
- **按显式信号分类重试**：`TaskResult` 新增 `stop_signal` 字段，仅
  `step_budget` / `time_budget` / `stalled` 三类自动重试；用户取消、
  awaiting_user、模型 Abort、token 预算、审计/工具阻塞、策略异常一律
  不重试。三轮耗尽写 `outer_rounds_exhausted`、转 BLOCKED，不自动交付。
- **辩证修订（首版实现的实际缺陷）**：首版按 phase 分类导致头号场景
  （步数耗尽 → BLOCKED）从不重试；第二轮不重置步数/deadline，首次
  limit check 即再次撞限、零模型调用；用户取消被"复活"三次；最终账本
  RUNNING 与结果 FAILED 矛盾。可重试轮次现保持 RUNNING（BLOCKED 是
  无出边的终态），仅最后一轮或不可恢复阻塞才进入 BLOCKED。
- **真实验证（2026-11-10）**：直连 `api.deepseek.com` 双跑，两个任务均
  finalized/verified（pytest 真实通过），缓存命中全部 64-token 对齐
  （384/512/768/896）；非交互 auto-edit 无批准人时 veto 后 finalized/
  unverified 且如实披露 gaps，无阻塞死锁；真实 ConPTY 审批面板五档
  （once/turn/allow/deny/cancel）默认拒绝，`y` 后命令执行闭环。
- 新增 `tests/kernel/test_outer_loop.py`（7 用例）；更新
  `test_budget_stops_the_loop` 为轮/任务两层语义。全量 **4112  passed**。

### 自进化（Self-Evolution）链路在产品内激活

- **背景（证据）**：曾有 621 个任务账本含 `lesson_proposed`，0 个含
  `lesson_stored`——内核在失败点提案，但没有任何生产调用方提供存储，
  链路在提案处中断。
- **激活**：`RuntimeAdapter.from_repl` 经 `_lesson_store_for` 取 REPL 共享的
  `MemoryService` 并包成 `LessonStore`；交互 REPL 与 TS 前端的协议 host 都走
  `run_kernel_turn → from_repl`，因此单点注入、两路同时生效。无记忆后端的
  最小宿主得到 `None`（不学习、不产生噪声）。`/project refresh` 时随记忆
  服务一起重置。
- **教训即记忆**：无 `lessons.yaml`、无 `PITFALLS.md`、无 `/lessons`；教训是
  project-local 的 `MemoryKind.LESSON` 记录，指纹/类别/复发计数以标签承载，
  经 `/memory` 查看、编辑、归档（已实测命令输出包含 lesson 记录）。
- 设计文档 `docs/design/self-evolution.md` 全面对齐实现；`tests/test_lessons.py`
  重写为记忆后端 API，新增此前完全缺失的 `store_proposals`/`LESSON_STORED`
  端到端闭环测试。
- **真实 ConPTY 验收抓到一个审批死锁**：协议 host 的 stdin 循环在
  `turn.start` 时同步执行回合，内核等待审批时冻结了唯一能读取
  `approval.response` 的循环 —— 面板能显示、用户能作答，但响应永远不被
  读取，只能等 300 秒超时后 fail-closed。修复为回合在独立 daemon 线程
  运行（`docs/evidence/self-evolution/tty_lesson_double_run.py`），stdout
  写入由既有 `_EMIT_LOCK` 保护；回合进行中拒绝第二个 turn.start/command。
  新增管道级回归测试（`tests/test_protocol_host.py`）。旧 ConPTY 测试因
  使用 `test/fake` 模型（从不需要审批）而漏掉它。
- **端到端证据**：两轮真实回合账本——第 1 轮 `lesson_proposed`×2 +
  `lesson_stored`×2，第 2 轮 `lesson_applied`×2，三处 id 集合一致，
  LESSON 记录物理落盘（`docs/evidence/self-evolution/transcript.*`）。

## [0.9.8] — 2026-11-04

> 原则版：**内核不替人决定「能不能做」，只决定「做完了算不算数」。**
> 0.9.7 之后连续 6 轮真实 dogfood（全部用复杂任务）把这条原则落成了代码，
> 同时补上了三级校验里实际没跑的那两级。

### 执行（对齐成熟 agent 的行为）

- **push / 破坏性命令改为问人**（#414）：`PermissionPolicy` 里三处硬拒与
  `refuse_escalation` 全部改为审批；无审批通道时仍 fail-closed，但理由是
  「需要人决定，本回合没有审批通道」。内置策略不再声明任何「永不可批准」项。
- **批准必须被下游服从**（#411）：新增 `sandbox_roots.grant_once/consume_grant`，
  审批面板说 y/s/A 时把被批准的**确切路径**登记为一次性授权，`ToolNode` 的写围栏先查它。
  子代理的批准不产生授权。
- **循环判死后允许一次收尾工具**（#414）：`force_finish_allow_once`，让收尾真能补一件事。
- 删除 `kind=think` 步骤禁止调工具的守卫（把 B 类当成 A 类）。
- 步数预算耗尽此前不写 `policy_decision`，现统一走 `_budget_block` 并附循环诊断。

### 校验（三级真正都跑）

- **V2 有输入了**（#420）：`xenon/harness/intake.py` 从任务文本确定性派生
  `file_changed(路径)` / `test_passed(测试文件)`，写入 `TaskSpec.obligations` 并记入
  `intake_recorded`。散文要求不伪造覆盖。
- **V1 覆盖无计划回合**（#420）：`direct`/`react` 没有步骤，就把这一轮当隐式步骤，
  从 receipt 的 `FsWrite.paths` 派生。
- **三级同形事件**：audit 的 V2 verdict 也写成 `step_verdict`，`/evidence` 与回放可直接数。
- **声明了写入却零验证 → 不交付**（#418）：`changed=false` 的新建文件曾被 finalize 忽略，
  交付 `verified` 却 `verified_paths=[]`。真实复杂任务抓到的。
- **失败即教学**（#417）：失败 receipt 产生一条可执行的下一步，并记入
  `control_decision{teach_next_step}`（否则无法审计）。
- **A 类拒绝计数**：`result.metrics["refusals"]` 与 `status_snapshot()["refusal_total"]`。

### 上下文 / 意图

- **`context %` 说真话**（#408）：记录窗口深度（`last_prompt`），不再读 legacy history。
- **截断可见且可分页**（#409）：`truncated{shown,total,remaining}`，`read` 声明
  `start_line`/`max_lines`。
- **对上一问的回答原样传入**（#413）：内核以「等待你的回答」收尾时置 `_awaiting_user_answer`，
  下一句不分类、不套模板；路径/URL/标识不再被当成调研任务。
- **目录不是文件**（#410）：`read` 命中目录返回 `is_directory` + 条目，不再泄漏 OS 的
  `PermissionError`（曾被误读成"没有权限"）。

### 工程

- 新增 `scripts/audit_mechanisms.py`：用**真实账本**核对 13 项机制是否真的跑过
  （`OK` / `MISS` / `n/a` 三态）。它第一次运行就抓出"失败即教学"完全不可观测。

## [0.9.7] — 2026-11-04

> 现实检验版：0.9.6 发布后连续 5 轮 dogfood 抓出 7 个缺陷，全部是**机制之间的翻译层**
> 问题——模型想做的事与内核理解成的事不一致。本版修的是根因，不是表面症状。

### 内核

- **`kind=think` 不再被忽略**（#410）：`StepKind.THINK` 此前从未被任何代码使用。计划里
  的推理步骤（"汇总前 8 步的证据"）没有任何地方说明「这一步要的是推理」，模型只能硬凑一个
  工具调用，内核照单执行 → 连续失败 → 循环判死。现在视图直接写明该步骤的期望，
  `_reasoning_step_guard()` 对 think 步骤的工具调用记 veto 并继续回合（模型可改用
  `think`/`finish`），不再判死。
- **无 target 的调用在绑定阶段被拒**（#405）：`ToolSpec.requires_target` 覆盖
  `read`/`search`/`apply_patch`/`read_file`/`search_files`/`write_file`/`edit_file`。
  拒绝理由里写出 spec 声明的参数名，并提示「要总结就用 `finish`」。具体参数名来自
  `params_schema`，kernel 内仍无工具名/参数名（K005）。
- **判死前先收尾**（#407）：循环信号达阈值且已有成功 receipt 时不再直接结束，而是记
  `control_decision {decision: "force_finish", receipts: N}`、关闭工具、要求用现有证据
  作答；当次动作不执行；仍要调工具才 veto 结束。claim→fact 绑定不变。

### 上下文

- **`context %` 不再是假的**（#408）：内核回合不写 legacy history，`ContextManager` 的
  比率退化到「几条消息的 token 数」，20K 字符的 prompt 显示成 `0.3%`。现在
  `UsageTotals.last_prompt` 记录**窗口深度**，`status_snapshot()` 暴露 `context_tokens`，
  进度钩子写回 `record_real_usage`。
- **截断可见且可分页**（#409）：effect 的 `content` 此前被静默砍到 8000 字符；现在带
  `truncated {shown_chars, total_chars, remaining_chars}`，`read` 的 schema 与描述声明
  `start_line`/`max_lines`（工具一直支持，模型从来没被告知）。
- **等待用户的回合不再显示成失败**（#402）：`blocking_reason` 认 `user_question`，
  返回「等待你的回答：<问题>」，面板标题为 `Kernel · 等待回答`；顺带删掉 40 行重复实现
  与一份从未执行的 `_result_summary`。
- **生成进度不再刷屏**（#403）：`模型生成中 Ns · M 字符` 每次回调一行（一轮刷 200+ 行）。
  现在只在单次调用超过 15s 时提示一次，其余交给状态栏。

## [0.9.6] — 2026-11-04

> 主题：**一个循环**。0.9.5 之前是七套引擎并存的形态；本版把执行收敛成单循环内核
> （`LoopRuntime` + 阶段配置），把「谁说了算」从提示词挪进 ledger：模型提议、
> 内核裁决、证据成链。旧的七套执行路径保留为 frozen legacy 兼容层，不再是主路径。
>
> 仍是 **research beta**：机制齐备，但只在有限真实任务上验证过（见 `docs/evidence/`）。

### 内核（单循环）

- `/mode` 改用中立的 `xenon/modes.py` 模式目录：名称、描述与 kernel 路由能力只有一份来源，`model_registry` 不再 import `xenon.engine`。修复 `reflection` / `react-reflection` / 未知模式被 kernel 静默降级为 ActFirst 的问题；只有 `direct` / `react` / `plan-execute` 进入 kernel，其余模式继续走 frozen legacy，未知模式 fail closed。legacy 引擎描述复用同一目录，避免 `/mode` 与 `ENGINE_REGISTRY` 漂移。
- PlanFirst 修正 plan prompt 的字面量“反斜杠+n”（与 ActFirst 一样使用真实换行）；计划步骤结束后新增一次受约束的最终总结，基于 ledger 投影的 working set 返回 `Finish(answer)`，不再固定回答 `completed`。真实 TTY `/mode plan-execute` 读取随机 probe 并返回 token；ledger 有 `plan_accepted` / `tool_receipt` / `delivered`，证据在 `docs/evidence/tty-plan-execute/`。
- kernel 回合（accepted / blocked / adapter exception）结束时统一清理 legacy `_task_state`，不再只在 accepted 清理；并在创建 legacy TaskState 之前决定 kernel 路由，kernel 回合不再物化/延续第二套任务状态。避免 kernel ledger 已裁决后下一轮仍被旧的“未完成交付”状态驱动。
- kernel `ReplEventSink` 从 `tool_receipt.effects` 投影 `fs_write.paths` / `created_paths` 到 ThinkingPanel action input；`session_ops` 将 kernel 的 `apply_patch` 计入修改文件。kernel 编辑回合的 working memory、turn-node artifacts 不再因 legacy 参数缺失而为空。
- `session_ops` 使用 `created_paths` 区分新建与修改：kernel `apply_patch` 新建文件进入 `session_created_files`，已有文件进入 `session_modified_files`；同一路径从 modified 中移除，避免 working memory 重复。
- kernel 回合的 `TaskResult.metrics` 进入 turn node / `turn/verdict`，覆盖 panel 推断的 steps/tools；`TurnTree.status_suffix()` 仅在 `is_resumable` 时提示“继续”，`degraded` 不再暗示可续接。
- `/plan` 不再把所有计划模式回合强制切回 legacy：只读计划回合继续走 kernel（权限只读），deferred-write 计划使用 kernel plan-only；`/plan approve` 接 `resume_planned()` 执行同一份计划。
- kernel 新增 plan-only 原语：`TaskMode.PLAN` 接受计划后进入 `TaskPhase.PLANNED`，不启动步骤、不执行工具；plan-only 任务拒绝任何非 `SubmitPlan` 动作，避免策略把“只规划”变成执行。`run_task(mode=...)` 可显式选择该模式。
- REPL adapter 新增 `plan_only=True`：强制 PlanFirst + `TaskMode.PLAN`；`PLANNED` 渲染为 `Kernel · plan`，turn node 记 `pending-approval`，不再误报 blocked/degraded，并保留 ledger 指针供后续审批。`/plan` 路由与批准后执行尚未迁移。
- `/plan approve|reject` 可对当前 `planned` kernel ledger 做审批：校验链和 phase，解析计划并计算 digest，把 `plan/approved` / `plan/rejected` 写入 session 事件（不追加 kernel ledger）；approve 退出计划模式，reject 保持当前模式；重复审批幂等。批准后执行仍未实现。
- kernel 新增同 ledger `resume_planned`：校验 `planned` phase、已接受计划摘要和必填 approval id，写 `plan_approved` 事实，`PLANNED -> RUNNING` 后执行 accepted steps；不追加第二个 `task_started`，不允许再次规划或重复恢复；plan digest 不一致 / 缺失 approval / 非 planned 均 fail closed。
- `/plan approve` 已接到 `resume_kernel_plan()`：审批事件落 session 后，用同 ledger 执行 approved plan 并渲染 kernel 结果；执行启动失败只报错，不标记交付。`/plan` 写计划路由仍走 legacy，下一片迁移。
- `/plan` 写计划路由已迁到 kernel：plan mode + deferred-write 回合走 `run_kernel_turn(plan_only=True)`，先得到 `PLANNED`，审批后由同 ledger 执行；不再打开 legacy 计划门。
- `/plan status` 在计划执行后显示 phase / finalize commit；已批准但 blocked/failed 的计划不会显示成已交付。
- 真实 TTY 跑通 `/plan on` → plan-only → `/plan approve` → 同 ledger 执行 → finalize commit；证据在 `docs/evidence/tty-plan-approval/`。本轮修复 plan-only prompt 允许计划写步骤、空/非法计划 fail closed、plan 词汇别名、`apply_patch` 边界路径、审批执行级别、绝对 `changed_paths` 的 `created_paths`。
- `no_write` 否定支持按目标作用域：`不要修改 probe.txt` 只保护该路径，不再连带禁止 `创建 out.txt`；工具执行层对 protected path 硬拒绝（含 `apply_patch`）。`任何/所有/文件` 等泛化否定仍是全局禁令。
- `scripts/patch_cov.py`：本地算 patch 覆盖（diff 的新增行 × coverage.xml），对齐 Codecov 语义（只算可执行行、merge-base、路径归一化、过期报告警告）；CI 对 PR 加同一道门（阈值 90%）。目的是 push 前就知道，而不是等邮件。
- 权限规则层（#373，对齐 Claude Code settings）：`allow`/`ask`/`deny` 三档 + `capability(pattern)` glob，**deny > ask > allow**；分用户/项目两层文件；审批四选一（只这次／本会话／永远写规则／只这条命令），`/permissions` 列出与撤销。destructive 与 push 仍然不可授权。
- 会话级授权（#373）：`GrantBook` + `ScopedGrant` 通配，一次弹窗后同一会话同类调用直接 ALLOW；硬安全规则（destructive / push）先于 grant，wildcard 不能授权它们。两处真 bug：wildcard 绕过 destructive 检查、MSYS/Windows 同一路径不匹配导致 grant 失效。**§9 口径修正**：授权改为**只存内存、带 30 分钟期限**（`DEFAULT_GRANT_TTL_SECONDS`），不再写 `GRANT_ADDED` 进账本（删除该枚举值会让 217 个历史账本加载失败，故保留值、停止写入）；`GrantBook` 的 `to_payload`/`load` 已删除，重启后重新询问。账本仍记 `USER_APPROVAL`（人做了什么决定）与 `POLICY_DECISION`（这条调用为什么没问）。
- 循环检测补齐（#366）：引擎层原有 4 个信号，迁 #322 时只留了最窄的「完全相同签名」，理由是"避免模糊匹配"——但原实现是精确哈希/精确模式比较，理由用错了对象。现在相同签名／同一错误 ×4／A→B→A→B ×3 是硬停，"连续 N 次无新信息"只做诊断（真实运行证明它会在成功前 4 步误杀），预算停机时附在原因上。
- shell 工具描述改为平台感知（#366）：Windows 上明确是 PowerShell 而非 cmd，并说明 `python -c` 被策略拒绝。真实运行：同一任务工具调用 18→7、失败 6→1，默认 20 步即可交付。
- 恢复 provider 阶段事件（#320）：`ModelConfig.request_sink` 在每次 HTTP 尝试前后报告 phase/超时/尝试次数/重试/失败原因，三种真实网络结果（成功、401 不重试、502 重试）都有证据；REPL 用 `kernel_request_reporter` 渲染。删除了 `EngineCallback` 上两个从未被调用、`ConsoleCallback` 也从未实现的死钩子。事件不进账本——账本记事实，不记每次尝试的遥测。
- 修复 PlanFirst 结束语：`_FINISH_PROTOCOL` 只写了 `"claims":[]`，模型于是回 `"file_changed:calc.py"` 这种字符串，`parse_action` 判非法 → 整个回答被丢弃成 `Plan execution completed; final summary unavailable.`（真实 `--plan` 运行里就是这样）。现在协议写出 claim 结构、接受 `kind:target` 简写，并且只有 claims 非法时保留模型自己的 answer。
- 子任务记录 `batch_id`（#361）：`TaskSpec.batch_id` → `TASK_STARTED`，一次派发一个批次，`xenon replay --children` 按批次分组；没有批次的旧账本显示为 `(no batch)`，不被悄悄并入。
- 并行子任务默认跑在各自的 **git worktree** 里（#358）：真实运行里两个子任务改同一文件的不同函数，后写的把前一个整段覆盖，两边账本都以为成功。现在成果以 patch 回流（`apply_patch` 显式调用，`--3way` 真合并，冲突报冲突），`git worktree list` 无残留。
- 父级可见子级（#356）：`TaskSpec.parent_task_id` 落进子任务账本的 `task_started`，`xenon replay --children <parent>` 从账本重建调用树（零模型零网络）；子任务的授权请求由父级 provider 代办（文件请求/应答，父级无 provider 即拒绝），子级权限必须是父级子集。
- 修复 `_transition` 对「已在目标态」抛错：真实运行里 `running -> running` 把整个任务打挂（释放的步骤没有违规可修时发生）。
- 子任务改为**独立进程组**（#354）：线程杀不掉，被放弃的子任务会在父级记下 `timeout` 之后继续写工作区（实测到 +60s）；现在超时杀整个进程组，`phase="killed"` + `pid` 落进父级证据，返回后工作区不再变化。真实并行实测 1.95x 重叠、无残留进程。
- V2 审计提案器（#286）：新增 `LLMAuditProposer`（只提案、只点账本里已有的回执）；提案型绑定在裁决上标记 `from_proposal`，证据等级最高 `PARTIAL`——事实存在不等于事实证明了需求。
- 修复 `xenon exec --plan`：`run_plan` 按计划形态选择 profile（全 capability → 确定性执行；含自由步骤 → step actor + 模型），缺少 `capability` 的步骤明确 `ReleaseStep` 说明原因，不再空转耗尽修复预算。
- `no_stray_changes` 确定性绑定（#287）：新增 `WorkspaceProbe` 效果 + `workspace_probed` 事件（宿主观测，kernel 不调 git），`ClaimResolver` 据此判定「有无杂散改动」，不再永远 abstain；探针仅在任务声明该义务时算作证据。
- 修复整套测试无法绑定：`pytest -q` 这类无路径运行的 `TestRun.target` 改记为空串（整套），整套通过覆盖任意具体测试目标。
- 修复阻断理由失真：已被确证违反的义务（如「工作区有未验证改动 notes.txt」）不再被交给覆盖绑定器重审，避免把具体原因替换成通用的「semantic claim needs a coverage binder」。真实运行验证：理由现在直接点出路径。
- 假完成基准相应收紧：`false_test_claim` 的证据等级从 `abstained` 更正为 `unverified`（claim 无法绑定 = 义务未满足，不是"无法判断"）。
- 端到端冒烟新增 `stray-change-reported`：预置一处无关改动，断言被拒绝且理由点名该路径。
- 真实端到端冒烟（`scripts/e2e_smoke.py`，9 场景）：跑用户入口（`xenon exec` + `xenon.run_task_result`）、真实模型与 git 仓库，覆盖改代码/提交、plan-execute、只读、不提交、接受缺口、无审批人 fail-closed、不可能任务；证据落盘 `docs/evidence/e2e-smoke/`。
- 冒烟发现并修复：SDK 无授权通道（`verify` 一律 blocked，库调用无法跑测试）→ 新增 `permission=` / `allow_approval=`；`TaskOutcome` 增加 `reason`（`blocking_reason()` 提到 `xenon/replay.py` 供 REPL 与 SDK 共用）。
- 新增 `xenon.run_task_result()` 返回 `TaskOutcome`（accepted/phase/evidence/debt/finalize_sha/ledger/reason），`run_task()` 仍返回字符串以保持兼容。
- 假完成基准（#288）：`evals/false_completion.py` 9 个对抗用例（健康路径/部分完成/假测试声明/杂散改动/修复后成功/语义 abstain/无 push 回执/空跑/工具失败），固定分母、失败不剔除；报告明确标注 `scripted-fake`（测 harness 非真实模型），产物见 `docs/evidence/false-completion/`；CI 增加该基准 smoke。
- session context 注入（#340）：`ContextManager.context_layers()` → `TaskSpec.context_blocks` → 视图 `Session context` 段（明确标注 reference only / 非证据）→ `SESSION_CONTEXT` 事件（只记标签、字数与摘要，不复制记忆内容）；REPL 任务轮次与已批准计划恢复都注入项目规则与检索记忆。
- 引擎残骸清理（#202/#338）：删除 `task_runtime`(703L)、`task_state_projection`(239L)、`execution_evidence`(195L)、`loop_detector`(304L) 与 `evidence_gate` 的 6 个具体 Gate 类 + `default_gates()`，共约 1741 行；`xenon/engine/` 43 → 21 文件（全部在役器官）。逐条等价性见 `docs/evidence/mechanism-audit/residue-removal.md`。
- 文档真话化（#206/#294）：`COMPARISON`(zh/en)、`TUI`、`EVAL_PROTOCOL`、`ARCHITECTURE`(zh/en)、`EXTENDING`(zh/en)、`OBSERVABILITY` 不再宣称已删除的 7 引擎/PlanDAG/组合引擎；历史文档加归档标记。
- 新增 `scripts/check_docs_refs.py` + CI：文档里引用的 `xenon/**.py` 必须存在（历史文档需显式标记）。
- 授权输入不再和 prompt_toolkit 抢 stdin：kernel 轮次内改用 REPL 的 prompt_toolkit 会话读取 y/n/a/q（此前用 rich `Prompt.ask`，Windows 上按键被吞，表现为「输入后卡住」）。
- 状态栏显示**正在执行的 loop**而非 `/mode` 配置（此前任务轮次里仍显示 `direct`）。
- 审批可诊断（#202 收尾）：`KernelApprovalProvider.last_reason` 保留前端判定理由，runtime 用它写 `user_approval`；`_result_summary` 从账本取最后阻断理由，blocked 不再只显示 `phase=blocked`。
- CI 对齐新架构：ruff 覆盖 `scripts/`；新增 `replay`/`metrics` 确定性冒烟（读已提交账本，零模型零网络）。
- `user_accepted_debt`（#287 前半）：`TaskSpec.accept_debt` 显式声明 → 无证据的缺口记为 `debt_accepted` 事件并可交付，`evidence` 保持 `partial`；**`VIOLATED`（有失败证据）永不被接受**。
- 分类修正：finalizer 无法读取 worktree（无 git/不可读）从 `VIOLATED` 改为 `UNVERIFIABLE`（`FinalizeOutcome.unavailable`），"无法验证"与"验证失败"不再混为一谈。
- CLI/SDK：`xenon exec --accept-debt`、`run_task(accept_debt=)`。
- **引擎骨架已删除**（#202）：`xenon/engine/` 从 43 个文件降到 25 个，18 个骨架文件移除（base/registry/builtin_engines/react/plan_execute/reflection/combined/event_bus/plan_dag*/plan_contract/prompts/strategy_guide/scout/verification_nudge/claim_gate/execution_policy）。
- 守卫升级：`check_engine_imports.py` 改为「骨架文件不得存在 + xenon/tests/evals/scripts 四棵树禁止 import」，并用 import 语句匹配（不误报守卫测试里的字符串断言）。
- 机制迁出：`ExecutionPolicy`（请求截止/节流/事件）从引擎包迁到 `xenon/harness/request_budget.py`（`RequestBudget`），swebench 适配器改用它。
- evals 全面转 kernel：`runner.RealAgent` 默认走 `_KernelEvalAgent`（工具轨迹来自账本而非 monkey-patch），`execution_benchmark` 与 `agentic_completion` 改用 `run_eval_task`。
- Batch 3 B 类收官（#202）：`test_document_tools`、`test_tool_schema_registry`、`test_tool_execution_lifecycle`、`test_run_script_tool`、`test_context_continuity`、`test_update_plan_tool` 全部去骨架化；**测试侧骨架引用清零**。
- 机制回迁：插件工具进入 `build_catalog`（新增 `plugin_specs()`，`plugin` pack 在所有 profile 可见）——引擎删除后插件注册会静默失效，属真回归。
- 登记 #340：kernel 轮次不注入 memory/project 上下文（chat 轮次注入），属真缺口，待实现。
- Batch 3 B 类第三批（#202）：证据门三件套去掉引擎私有管线用例，保留器官断言（`verify_file_claims`/`task_requires_write`/`PlanCompletenessGate` 等纯判定），`evidence_enforcement` 默认值改由 `ToolExecutor` 直测。
- 登记 #338：4 个具体 Gate 类（`PlanCompletenessGate`/`TaskCompletionGate`/`FileClaimGate`/`EvidenceCaptureGate`）在引擎外零引用，能力已由 kernel 审计+债务覆盖，待确认后退役。
- 仓库分支清理：38 条已合并/空差异分支全部删除，远程仅保留 `main`。
- Batch 3 B 类第二批（#202）：`test_execution_policy`(99)、`test_permission_integration`(24)、`test_input_boundary_validation`(43) 去掉引擎夹具；并把旧引擎的**入口输入校验**真正搬到 kernel（`TaskSpec.__post_init__` 拒绝非字符串/空 objective/task_id）。
- Batch 3 B 类第一批（#202）：`test_tool_profiles`、`test_tool_result_read`、`test_tracker_detail_log`、`test_llms_txt`、`test_pull_request_tool`、`test_mcp_management_tools` 去掉引擎夹具/骨架常量，改测器官（`tool_profiles` 过滤、`ToolExecutionTracker`、`BUILTIN_TOOL_METHODS`）。
- Batch 3 A/C 类（#202）：删除 19 个"断言对象是引擎内部/引擎提示词"的测试文件（约 170 用例），`test_turn_gate` 去除死夹具与 `EngineSpec` 依赖，`test_contract_single_source` 守卫改为覆盖仍在的模块；判定表落盘 `docs/evidence/mechanism-audit/batch3-table.md`（A 删 18 / B 迁 17 / C 守卫 2）。
- `xenon replay` / `xenon metrics`（#289）：只读回放与指标，纯事件折叠、零模型依赖；`--json` 机器可读，`--all` 显示全部事件；支持 task-id 或账本路径。
- kernel 轮次进度可见（#327 续）：`ModelConfig.progress_sink` + 主线程轮询回调；真实传输走 `chat_completion_stream` 组装，注入 client 保持同步；REPL 仅对 >1s 调用提示"生成中 Ns · N 字符"，不打印协议 JSON。
- 取消可打断在飞调用（#327）：`kernel/cancel.py` + `ModelConfig.should_cancel`；调用走工作线程+轮询，取消在 ~150ms 内生效；runtime 以 `ABORTED{reason:"model call cancelled"}` 结束，与 `BLOCKED`/`FAILED` 严格区分；REPL 用 SIGINT 触发。
- steering 落地（#329）：`kernel/steering.py`（纯逻辑）+ `post_user_input` 写事件 + 动作边界消费；约束逐条附 seq 进视图；纠正以 `superseded` 标注而非删除；新增 `CONTROL_DECISION{kind:"steering"}` 记录消费；REPL 侧运输层（TTY 监听 → 队列 → runtime 单写者落账）。
- token 计账落地（#326）：新增 `kernel/usage.py` 与 `MODEL_USAGE` 事件；`Budget.max_tokens` 真正生效（超限 → `policy_decision{VETO}` + `BLOCKED`）；用量经 per-call sink 注入，并发子任务不串账；provider 未报 usage 时退化为估算并标 `estimated`。
- steering 四项决策已定并记录（时机=动作边界、不可撤销只能追加纠正、约束逐条保留、打字即注入），实现拆到 #329。
- 窗口跟随模型：`runtime_adapter.model_config_for` / SDK 不再丢 `context_window`；kernel 轮次同步 REPL 计量（1M 模型不再按 128K 显示）；状态栏显示真实窗口。
- 设计文档：`docs/design/steering.md`（注入时机 A/B/C 三方案与语义）、`docs/design/parallel-branching.md`（并行=事件流分叉/回流，不做 DAG）。
- 遗漏审查登记：#326（token 用量计账）、#327（取消打断模型调用），kernel 流式与模型链 fallback 判定见文档。
- 机制适配性审计 `docs/design/mechanism-fit.md`：逐条判定迁入/改造/放弃（放弃 deadline 降级、反思打分、LLM 自动切范式，理由写明）。
- `Budget.context_window` + `ModelConfig.context_window`：view 按模型真实窗口折算输入预算，不再用 `max_tokens*2` 魔法系数。
- 截断变为具体下一步：`ResponseTruncatedError` 不再打穿 loop，转为"输出被截断，请给出一个更短的 action"的 thought（附动作与步骤两处）。
- 压缩策略拆分为独立小模块 `xenon/kernel/compaction.py`（view 只负责组装）；新增 `tests/kernel/test_robustness.py` 并修出真缺口：gateway 抛异常会打穿 loop。
- 新增 `docs/evidence/mechanism-audit/`：拆除期 53 个被删测试逐条复审（已迁/已补/形态消失/待补）。
- 机制回迁到 loop（#322）：working set 压缩不再静默丢弃，折叠成确定性摘要条目并写 `context_compacted`；新增 `RepeatedCallGuard`，同一 `(tool, args)` 重复 N 次触发 `policy_decision{VETO}` + `BLOCKED`。
- 删除 4 个引擎内部测试（trace 日志、perf profile、`_near_context_window`、provider 事件路由）；`conftest` 与 `test_repl_real_usage` 不再依赖 `engine.base` 的 mock 恢复。
- 记录真实缺口 #320：`provider_request_start/end` 只由 legacy 层发出，kernel 轮次下 UI 拿不到请求阶段事件。
- 删除 LLM 调用层（`BaseEngine`）专属测试 8 个：max_tokens 解析、model overrides、cache telemetry、prompt 稳定性、截断上限、两种 compaction、deepseek 协议、错误分类、trace 日志。等价行为改由 kernel 侧 `ModelConfig → llm_client` 透传测试与 `transient_errors` 覆盖。
- 删除 35 个"主体即已移除架构"的测试文件（引擎循环、plan DAG、prompts/strategy guide、subagent 引擎路径、引擎 chaos）。保留覆盖器官的测试，逐个迁移后再删骨架文件。
- **引擎宿主清零**：`xenon/repl/engine_host.py` 从 900+ 行降到 213 行，只保留日志捕获、回调工厂、历史配对收尾与 kernel 转接（`_run_react_engine`）。`_run_engine`、6 个 `_run_*_engine` 兼容壳、trace 持久化、MCP 注入、plan 面板订阅、steering 全部删除。
- CI 引擎守卫白名单清空：**生产代码已无任何 `xenon.engine` 骨架引用**。剩余 legacy 测试逐个迁移/删除。
- **SDK 入口迁到 kernel**：`xenon.run_task()` 不再构造引擎，改为用 loop profile 跑 `run_task`（`finalize=False`，库调用不提交）。`create_engine` / `register_engine` / `bind_workspace` 三个引擎工厂 API 随引擎层退役；`list_engines()` 保留为“列出可选 thinking mode”。
- `xenon run <workflow.yaml>` **不退役也不迁**：它跑在 `nodes/` + DAG 调度器上，与七引擎无关，调度器归为器官。
- `setup_wizard` 的模式描述回退改为模式目录；`direct_chat` 的瞬时错误分类移到 `xenon/repl/transient_errors.py`。
- **子任务运行时（#204 最小可用）**：新增 `xenon/harness/child.py`，`/sub-agent` 改为在**独立 ledger** 上跑同一个 loop（`run_task`），不再 new `ReActEngine`。子任务不产生交付提交（`finalize=False`），父会话只记录账本指针事件。
- **评测迁移到 kernel**：`evals.swebench_xenon` 不再 new 引擎，改由 `evals/kernel_runner.run_eval_task` 走同一个 loop；评测引擎名保留为 loop profile 映射，历史结果仍可比。
- `LegacyToolGateway` / `run_task` 支持注入 `tool_runtime`：评测容器里的工具执行不再需要给引擎绑运行时。
- `coding_contract` 从 `xenon/engine/` 移到 `xenon/harness/`（它是补丁提取工具，不属于引擎骨架）。
- 新增 `scripts/check_engine_imports.py` 并接入 CI：把仍依赖引擎骨架的文件显式列为白名单（`/sub-agent`、`xenon run`、`xenon/__init__` 多引擎导出、`setup_wizard`、`direct_chat`、`engine_host`），任何新增引用直接失败。白名单只减不增。
- **校验分为两级**：步骤级（`step_verdict` + 新的 `step_verified` 汇总事件，随 `ModelView.step_verdicts` 提供给反思）与回合级（V2 audit + finalizer）。步骤级校验先跑，reflection 在其结论之上提修正，不能反过来判定通过。
- 反思/审查阶段接入事件流：`step_verified`、`step_explored`、`plan_reviewed`、`reflection_proposed` 都会投影到思考面板与回合树，不再只躺在账本里。
- **移除 turn_flow 的引擎分发**：删除 `ENGINE_REGISTRY` 查表与自动范式切换（`_select_turn_mode`）。工具/技能轮次统一走 kernel loop；无法进入 kernel 时显式失败，不再换范式。
- 删除 `XENON_RUNTIME=legacy` 逃生口：kernel 是唯一运行时。直接对话中的工具升级（模型请求工具、声称已执行）也改走 kernel loop。
- **策略层收缩为单一 loop**：删除 `ActFirst` / `PlanFirst` / `PlanReact` / `ReflectFirst` 四个类，改成一个 `LoopStrategy` + `LoopProfile`。plan / react / reflect 现在是同一 loop 的三个阶段，模式只声明阶段开关与预算，不再各自对应一个实现。
- LLM 侧的五个 `build_llm_*_strategy` 收成一个 `build_llm_loop_strategy(config, profile)`；适配器按模式取 profile，未知模式 fail closed。
- `/mode plan-reflection` 迁移到 kernel：`PlanReact` 增加两个审查钩子——执行前的计划覆盖审查（可加步骤，不能删义务，删义务直接拒绝）、以及步骤释放前审查（在出错的步骤内补一次修正）。审查结论写入 `plan_reviewed` / `step_explored`。
- 计划/步内/反射协议禁止在 `verify` 命令里写 `cd`：工作区本来就是 cwd，`cd` 会把普通校验变成需要授权的 shell 命令。
- `/mode react-reflection` 迁移到 kernel，且不新增策略实现：`ReflectFirst` 现在支持多轮反思（`max_reflections` 轮，每轮 `max_calls_per_round` 次修正）。`reflection` = 1 轮 1 次，`react-reflection` = 3 轮 2 次。反思可以审查"拟提交的答案与 claims"；reviewer 说没问题或重复提出同一修正即停止。
- `/mode plan-react` 迁移到 kernel：新增 `PlanReact`（提交一份计划，然后逐步执行有界子循环）与 `ReleaseStep` 动作。`ReleaseStep` 只能由策略发出、不能由模型生成；它强制关闭当前步骤，未满足的期望转成 debt，耗尽修复预算即 BLOCKED。步内探索记入 `step_explored` 事件。
- 计划/步内协议现在显式列出允许的 capability（`read/search/edit/verify/vcs/shell/remote`），并把 `bash`/`grep`/`apply_patch` 等工具名映射为 capability，不再因一个工具名作废整份计划。
- 反思决策进入账本：新增 `reflection_proposed` 事件（`decision=correction|declined`、`capability`、`round`）。策略只做记录、运行时负责追加，反思仍然没有关账或交付权限。
- `/mode reflection` 迁移到 kernel：新增 `ReflectFirst` 策略（ActFirst + 一档有界反思）。反思只能追加一次修正性工具调用，不能声明完成、不能关账、不能判断通过与否；未绑定的 claim 在反思后仍是 debt。未知 strategy 名称在适配器里 fail closed，不再静默回落到 ActFirst。
- `/save` / `/resume` 的 kernel 指针现在同时保存计划审批事实（task_id + status + plan_digest）；恢复时仍需 ledger 的 task_id 与 accepted plan digest 一致才生效，否则视为待确认，不伪造已批准。

### 证据层

- 会话事件哈希链新增**可选密钥锚点**（#106）：设置 `XENON_EVIDENCE_HMAC_KEY` 后，链哈希从 sha256(正文) 改为 HMAC-SHA256(key, prev ‖ hk ‖ 事件正文)，前驱链接与 hk 模式标记一并纳入签名；密钥只从环境变量读取、永不落盘，事件文件仅带 `hk: "hmac-sha256"` 模式标记。缺密钥的核验者遇到 keyed 链计入"待核验"（状态 `partial`，绝不静默放行）；无密钥写入后再设密钥的混合模式核验为 `mixed/partial`，`/evidence` 新增链模式显示（keyed/unkeyed/mixed，keyed 完整链显示 🔒）。默认（不设密钥）行为与以前完全一致。
- legacy `ExecutionEvidence` 的 Git 快照显式按 UTF-8 解码（`errors="replace"`），并在 `stdout is None` 时按空快照处理；修复 Windows 下未提交中文 diff 导致 `plan-react` / `plan-reflection` / `react-reflection` 崩溃的问题。原英文 diff 行为不变。

### 旧执行层（legacy）

- **撤销** 0.9.5 的「连续 3 次低置信度循环 → 强制收束」：真实 SmartBench 会话中，它在 18/120 步把「调研完成、准备实现」的正常多阶段任务误杀。低置信度循环恢复为只注入提示，循环的硬终止只保留高置信度分支；执行层不新增校验门，与 L0/L1/L2 证据层保持正交。
- 规划协议与 Claude Code / Codex 对齐：新增规划专用 `submit_plan` 原生 function calling，模型直接提交结构化计划，不再手写 13KB JSON；原生端点不支持时自动回退原有文本 JSON 协议。真实 DeepSeek 会话验证：规划 1 次 native 调用、0 次文本回退，文件正确落盘。
- 规划提示移除含完整 Flask 代码的示例（与「禁止粘贴代码」规则冲突），只保留结构示例。
- 新增共享「执行契约」系统提示（ReAct + Plan-Execute）：端到端交付、默认直接做、交付物真实存在、先取证不猜、根因/最小改动、先读后改、验证闭环、工作区安全、计划不僵化、收尾可核对；证据层（工具输出不可信/不编造/事实绑定/TurnGate）原样保留。真实 A/B：旧/新提示均落盘并通过 pytest，新提示收尾更短。
- 写类工具的工具记录改为 `content=<N chars>` / `old_text=<N chars>` / `files=<N items>`：总结不再把「未展示原文」误判成「没有传 content」。真实复跑：总结明确写「content 参数已传入，共 5 字符」。
- 低置信度循环提示按 `(reason, loop_length)` 去重：真实复跑同一循环场景，提示从 6 次降为 1 次；不新增硬停门，预算仍为 120 步兜底。

### 工具

- `clone_repo` 的 git 认证头改为 Basic（`x-access-token`）：git smart HTTP 不接受 `Authorization: Bearer`，修复已登录 `gh` 仍报 `Authentication failed` 的问题。
- `clone_repo` 的浅克隆/抓取超时提升到至少 300 秒（原 60s 在网络慢时会被误杀），`ls-remote` 探测至少 30 秒。
- `clone_repo` 的默认缓存根 `~/.xenon/repos` 在工作区之外时（例如从 `D:/` 启动）自动回退到工作区 `.xenon/repos`，修复「克隆成功但读不了」的问题。
- 系统提示新增 Windows 命令语法规则：`command` 由 PowerShell 执行，不要用 `&&`/`export`/`$()`，串联用 `;`、环境变量用 `$env:NAME`。
- 提交与 PR 强制署名（默认开启）：`git commit`（`git` 工具与 `command` 工具）自动追加 `Co-authored-by: Xenon <noreply@xenon.dev>`，`create_pull_request` 在正文追加 `🤖 Generated with [Xenon] vX`；`XENON_ATTRIBUTION=0` 可关闭。

## [0.9.5] — 2026-10-18

### 引擎

- 规划 JSON 修复：补齐模型漏写的键名引号（`{"id":26,task":...}` 形态），真实 13KB 计划可恢复解析为 31 步。
- 规划解析连续失败时**降级为单步 ReAct 执行**，不再把原始计划 JSON 当最终答案返回；合法空计划仍按原语义处理。
- 规划提示收紧：最多 12 步、禁止粘贴文件内容/代码块；修改已有文件时 `old_text/new_text` 留空，由执行阶段先读后改。
- 文件声称审计只认完成声明（“已创建/创建了/Created”），且路径必须出现在声明附近；调研回答引用 issue 里的路径不再误报（实测两轮误报 18/17 个文件 → 0）。
- 统一标准步数预算为 **120 步**（与 Claude Code 类代理对齐）：`react` / `plan-execute` 由 40 步、`plan-react` / `plan-reflection` / `react-reflection` 由 24 步统一为 120；引擎类默认值与工厂默认值一致，`XENON_MAX_ITERATIONS` 仍可覆盖。SWE-bench 等评测运行冻结在 120 步（见 `docs/EVAL_RESULTS.md` 预注册），测试 `tests/test_step_budget.py` 锁定该常数。
- 引擎时限支持 `XENON_ENGINE_TIMEOUT`（秒，默认 600）：超时说明与规划阶段提示使用配置值，不再写死 600。
- 引擎时限（默认 600s）不再整轮红错：组合引擎在超时时返回「已完成步骤 + 时限说明」，REPL 以警告而非错误展示；输入「继续」可接着做，已完成的操作保持有效。
- 新增「改完代码必须验证」软提醒（#163）：ReAct 在 `final_answer` 前检查「最后一次成功写入之后没有任何测试/构建命令」，命中时注入一次提醒（不阻断、不重复）；系统提示规则 8 同步扩展为所有代码变更，要求先跑「能因改动而失败的测试」。
- 修复拒绝检测误判：`_detect_denial` 旧实现整段子串匹配，会把「可以，但有几个前提……我无法直接访问 X」判成拒绝并触发备用模型切换；现在只看开头第一句且排除肯定性表态。
- 循环检测的低置信度提示不再无限刷屏：连续 3 次检测到循环后强制收束并汇总当前结果（查询/研究任务保留原有宽容语义）。

### 会话

- 会话保存时记录工作目录；`/resume`、`/load` 在目录变化时提示旧路径可能失效，并自动重映射工作记忆中仍存在于新目录的路径（历史对话不改写）。

### 工具

- Windows 命令退出码修复：`2>&1 | Select-Object` 会把原生命令的 stderr 转成 PowerShell error record，导致成功的 `git push` 被误报为失败；`command` 工具现在追加 `$LASTEXITCODE`/`$?` 判定，成功即 0、真失败保留原码。
- `create_pull_request` / `github_fetch` 的 token 解析新增 `gh auth token` 兜底：机器已用 `gh auth login` 登录但没配 `GITHUB_TOKEN` 时可直接建 PR（缓存，失败降级为空）。
- `git` 工具新增 `repo_path`：从上级目录启动时也能在工作区内的具体仓库执行 `status/commit/push`（路径仍走沙箱校验）。
- `github_fetch` 的 `fetch_issue`/`fetch_pull` 现在接受 `github_path` 作为编号（`27` / `#27` / `issues/27`），不再退化成「列出 open issues」。
- `clone_repo` 新增 `target_dir`：可把仓库直接克隆到工作区指定目录；若目标已是 git checkout，只做只读校验、绝不 fetch/merge 用户仓库。
- 新增 `create_pull_request` 工具（GitHub API 建 PR；REPL 中按 CRITICAL 审批）；`branch` 为 head，`base_branch` 省略时自动探测默认分支。
- 新增 `XENON_EXTRA_ROOTS`：显式追加文件工具允许读写的根目录，解决「从 A 目录启动、仓库在 D 盘」的越界问题（默认行为不变，越界报错会提示该变量）。

### 评测

- SWE-bench 适配器新增 `XENON_EVAL_APPLY_TEST_PATCH` 开关（默认 `1` 保持现有行为）：设 `0` 时不在解题阶段向 agent 工作树应用官方 test patch，从而回到与官方协议、与旧盲测基准一致的「测试不可见」协议。
- 30 例盲测 3×2 结果与零重叠留出集的污染判定已录入 `docs/EVAL_RESULTS.md`（9.10–9.12）；复现步骤与验收标准见 `docs/REPRODUCE_SWEBENCH.md`。

### 文档

- README 评测章节前移并补充首屏成绩摘要；新增 `docs/REPRODUCE_SWEBENCH.md`（协议、环境、验收清单、7 个坑与结果模板）。
- README（中/英）结构优化：新增「快速开始 / 文档索引 / 平台支持 / 参与贡献」章节，文件、文档与安装链接统一到 v0.9.4；英文版补齐工具输出威胁模型边界说明（与中文版对齐）。
- README 视觉升级：居中 Hero（跑者动画 + 终端交付示意 SVG，由 `scripts/brand` 管线生成）、核心承诺告警框、特性表格；环境变量改为折叠展示。

## [0.9.4] — 2026-10-14

### 品牌：跑者启动动画

- 全新启动动画：中间拉线 → 像素跑者沿中线持续奔跑 → 身体粒子边跑边剥离（四肢先散、头部最后），在身后组装成方块 XENON 字样，中线被字母吸收、远端回缩，两侧各留一段对称签名线（`────── XENON ──────`），最后残余粒子汇入右侧线收尾；动画播完才进入欢迎界面。
- 动画创作管线用 TypeScript（`scripts/brand`：pixels / storyboard / render），同一像素数据生成终端帧表 `xenon/utils/_brand_frames.py`、`docs/logo.svg`、`docs/logo.png`（仓库头像）与 `docs/logo-animated.gif`（README 嵌入）。
- 帧表只存语义色键，`xenon/utils/logo.py` 播放时按主题解析：新增 `logo.*` 语义 token（9 个，入 `xenon/repl/theme.py` 目录与冻结测试），动画颜色受 `/theme` 管辖；`NO_COLOR` 时动画降级为无色。
- 终端标签活动状态由星位 `✶··` 更新为方块 `█··`（ASCII 帧不变）；`xenon` 工作流面板标题同步。
- README（中/英）嵌入启动动画 GIF。

### 终端与主题

- 新增 `python -m xenon` 入口（**本版起可用**）并补充 Windows PATH 排障文档；README 中 v0.9.4+ 的版本注记自本版起成立（v0.9.3 wheel 不包含该入口）。
- `NO_COLOR` 现在同样作用于输入提示样式（#115）。
- 新增语义 token 目录与默认调色板冻结测试（#117，`xenon/repl/theme.py`）。

### 智能路由

- 新增智能路由决策分析（#116）。
- 从手动 `/mode` 覆盖学习范式偏好（#134）：同一任务签名下同一范式被手动选择 ≥3 次后，路由对其推荐 +0.1 置信度（封顶 1.0，不绕过既有检查）；偏好持久化到 `~/.xenon/routing_preferences.json`（0600；文件损坏或不可写时静默降级）。

### 文档与基础设施

- 修正 MCP SSE 状态为「简化实现」并引用 #103（#133）。
- 新增观测层（AgentOps）P0 设计文档 `docs/OBSERVABILITY.md`（#131/#132）：span 模型、`xenon.trace/v1` schema、隐私 allow-list 与性能预算；P1–P4 待实现。
- 收录 [awesome-opensource-ai](https://github.com/alvinreal/awesome-opensource-ai)（Agentic AI & Multi-Agent Systems → Autonomous Coding Agents）。
- 接入 Codecov：新增 `codecov.yml`（project 目标 70%、patch 目标 80%，与 `pyproject.toml` 的 `fail_under=70` 对齐），README（中/英）增加覆盖率徽章。
- README 重写聚焦；新增 CODE_OF_CONDUCT；CONTRIBUTING 增加英文精简版、CI 证据要求与版本语义说明。

### 会话恢复体验

- 启动会话选择器：`xenon` 进门之前先选会话（pi/Claude Code 式体验），
  数字选择历史会话或新对话；`xenon resume` 直接恢复最近；非交互终端自动跳过。
- `xenon resume [<序号|名称>|list]` 位置参数支持。
- 恢复任意会话后回放历史对话（用户/助手消息，超长折叠、较早折叠标注）。
- autosave 空会话不再覆盖旧自动保存（防启动即退顶掉上一段对话）。
- credentials.yaml 旧版扁平格式（`provider: key`）自动按内置目录派生默认模型。

### 安全修复（外部评审四项 High，全部带回归测试）

- **H-4 凭证外泄**：`register_tool` 的模块前缀白名单过宽——模型可注册
  `load_credentials` / `load_github_token` 把 API Key 原样取回。新增
  凭证/配置模块点名拒绝（github_auth / provider_registry / llm_client /
  model_registry / system_config，含子模块）。
- **H-5 克隆仓库 hooks 无审批执行**：项目级 `.xenon/hooks.yaml` 改为显式
  信任制（sha256 指纹存 `~/.xenon/trusted_hooks.json`，新命令
  `/trust-hooks`）；未信任不加载并醒目提示；内容变化需重新信任；
  PreToolUse 超时/启动失败从放行改为 fail-closed 阻断；`.xenon/` 入
  .gitignore。
- **H-6 命令黑名单引号绕过**：`rm -rf "/"`、`curl … | "bash"`、
  `dd "if=…"` 全部拦截。匹配重写为引号感知 + 段锚定：按 shell 链
  分隔符切段，原始与去引号双候选段首锚定；跨段管道模式只在真实 `|`
  相邻段对上判定（引号内伪管道不误报，B-1 语义全保）。
- **H-7 授权语义收紧**：`y` 授权从按工具名改为按 `(工具, 目标路径集合)`
  ——同一轮同工具不同路径仍需分别询问。

### 加固与清理

- 路径围栏：`security_enabled` 不再是模型可设置参数（曾被一个键关掉
  围栏与黑名单）；子代理继承父上下文审批通道；执行级工具（command /
  spawn_agent / 执行型 mcp_call）一律询问，工作区内 file_path 不再
  绕过审批面板。
- 沙箱命令白名单修复死代码（normalize 后读错键名）并升级为整条命令链
  检查（`&&` / `||` / `;` / 换行逐子命令）。
- 死代码删除：`GLOBAL_BREAKERS`、`tool_gate.EvidenceMode`；CHANGELOG
  历史 enforce 声明标注为 v0.8.2 行为（v0.9.x 交互路径证据门固定
  observe 审计级）；`_SENSITIVE_PATHS` Windows 反斜杠条目修复。
- 文档收敛：ARCHITECTURE.md 重写为 v0.9.2+ 现状（三层可信度模型）；
  COMPARISON/GUIDE/配置指南同步修正；README 徽章与浅克隆指引；
  Windows 测试收集修复（`pytest.importorskip`，本机全量可收集）。

## [0.9.2] — 2026-10-12

### 三层可信度模型（定位收敛）

- **L0 证据层**：会话事件日志升级为 append-only 哈希链（每条事件带内容哈希
  + 前驱哈希），新增 ``/evidence`` 命令随时重算自证（当前会话 / 全局 /
  指定会话三视图，全部确定性核验、无 LLM）；默认视图为结构化回合摘要
  （意图/引擎/工具统计/判定/回复预览），``/evidence raw`` 保留逐条哈希明细。
- **L1 结构层**：空洞回答为唯一阻断类校验（确定性），命中才重试/熔断。
- **L2 语义层**：宣称审计/完成准则/文件声称/写入后验证不再阻断发布，
  降级为「待人工核验清单」黄色面板随答案输出；重试耗尽 → 降级发布
  （degraded），仅同因结构失败熔断按未完成交付。
- **单一语义通道**：工具成败只由工具回执的 success 字段决定，面板/事件/
  渲染不再用关键词猜失败（修复降级路径 ConnectError 被误判为失败的假阴性）。
- **工具能力**：github_fetch 无编号 issues/pull URL 自动列 open 概览；
  web_fetch 限流错误附 GITHUB_TOKEN 提示。
- **版本单一来源**：``xenon.__version__``（0.9.2）为唯一版本源，
  pyproject 动态读取；Ruff 移除 <0.16 上限（钉规则合同而非工具版本）；
  覆盖率门槛 55% → 70%（实测 74%）。

## [0.8.5] — 2026-08-22

### 安全修复：两处沙箱逃逸（写入可越出工作区围栏）

两个漏洞均实测复现后修复，各自带可复现 POC 与「临时回退修复 → 断言必须
FAIL」的有效性自检，确认测试真的锁住行为而非恒真。

- **fix(security): 路径围栏跟随符号链接。** `_validate_path` 用
  `os.path.normpath` 规范化，它只在字符串层面消解 `..`，不跟随符号链接。
  工作区内存在指向外部的链接时，路径在字符串上看仍在围栏内 → 校验放行 →
  写入实际落在围栏外。读路径同样中招，意味着 A13 的凭证读取防护也能被
  绕过。符号链接在克隆仓库、`node_modules`、虚拟环境里极常见，Agent 自己
  也能用 `command` 工具创建链接后再写入。现改用
  `Path.resolve(strict=False)`，让围栏比对看到真实目标；12 个调用点全部
  经由此单一收敛点，无需逐个改。恶意路径无法关掉围栏：符号链接循环由
  pathlib 抛 `RuntimeError`（**不是** `OSError`，最初只捕 OSError 导致
  异常直接泄漏给调用方），超长路径抛 `OSError`，两者都退回文本规范化且
  结果仍在围栏内。
- **fix(security): 交互路径绑定 ToolRuntime。** `bind_tool_runtime` 此前
  只在子代理 spawn 与 SWE-bench 评测中调用，`xenon/repl/` 下一处都没有，
  交互模式始终运行在 `ToolExecutor.runtime is None` 状态。该状态下
  `_runtime_params` 原样返回模型参数，而 `cwd` 在 `ToolNode._VALID_PARAMS`
  内、围栏根又由 `cwd` 推导——模型自选的 `cwd` 因此**移动围栏本身**而不是
  被围栏约束。现在 `_run_engine`（七种范式唯一装配收敛点）绑定 ToolRuntime，
  可信根覆盖每次工具调用的 `cwd`。子代理连带受益：spawn 路径读取
  `node.tool_runtime`，此前交互模式下它恒为 None。绑定失败只记警告不上抛，
  任务不因目录被删而整体不可运行。
- **一致性**：此前交互模式与评测模式跑在两套不同的隔离强度上——40% 的
  SWE-bench 成绩是在强隔离（容器 + 绑定 runtime）下测的，交互模式却没有
  同等约束。本版消除该差异。
- **测试**：新增 `tests/test_symlink_fence.py`（14 例）与
  `tests/test_interactive_tool_runtime.py`（6 例）。全量
  **2359 passed, 8 skipped**，零回归。

## [0.8.4] — 2026-08-21

### 自审修复：MCP / 引擎输入 / 子代理超时（2026-08-21 全面审计）

- **fix(mcp): `discover_tools` 重建 `tool_map`，消除工具集收缩后的幽灵工具。**
  此前 server 撤掉某工具后重新发现，`tool_map` 残留死条目，`call_tool` 仍会
  路由到已消失的工具（`registry.py` 只增不删；全清仅在 `close_all`）。现与
  `_short_name_owners` 等三个共享派生态一并重建，并补「工具集收缩/增长/
  幽灵调用拒绝/单 server 失败不影响他人」四条回归测试。
- **fix(engine): 全部引擎 `run()` 入口统一输入校验。** `ReActEngine.run(None)`
  此前穿透到 `execution_policy.strip_execution_boundary` 的 `text.split()`
  裸崩 `AttributeError`。根因是 `BaseEngine.run` 契约无入口防护；现提供
  `_validate_run_input` fail-fast（组合引擎经 `SteeringMixin` 对齐同语义），
  None/非字符串/空白输入统一抛带定位信息的 `ValueError`。
- **fix(spawn): 超时路径接上 F6 协作式取消。** `_spawn_subagent` 超时后只
  `shutdown(wait=False)` 放弃等待，子线程继续后台执行（写文件/调工具），
  且原代码探测不存在的 `cancel()` 方法——真正的取消通道 `interrupt()`
  （`base.py` F6，run 循环每轮检查）从未接上。现单任务与批量并行路径超时
  均发送 `interrupt()`；组合引擎经 `SteeringMixin.interrupt()` 转发子引擎。
- **tests: live 测试无凭据时 skip 而非 fail。** `test_answer_only_live` 在
  无 `DEEPSEEK_API_KEY` 的环境以断言失败收场（贡献者本地/CI 误判回归）；
  现按 `llm_client.py` 的实际凭据来源做 `skipif`。
- **feat(api): 包级公共 API（懒加载）。** `import xenon` 零子模块开销；
  `xenon.create_engine(name, model_priority, **kwargs)` 走 `ENGINE_REGISTRY`
  稳定契约构造引擎，`xenon.list_engines()` 列出范式。README「框架核心是
  可观察、可替换的模块」自此有编程式入口。
- **refactor(engine): Plan-Execute DAG 执行器拆分（模块拆分第一步）。**
  `_run_dag`/`_exec_wave_*`/`_declared_paths`/`_wave_parallel_blocker`/
  `_build_prev_results`/`_step_outcome` 与 `StepOutcome` 纯移动至
  `plan_dag_executor.py`（1646→1247 行），零行为变化，266 项相关测试全绿。

### 最终回答完整性与输出预算

- 模型配置的 `max_tokens` 现在是用户可见回答的唯一 Xenon 侧输出预算；移除按
  provider 名称写死的二次钳制，并让 direct、流式、`/ask`、`/code` 与原生
  function-calling 路径一致传递模型配置。上游模型自身的上下文/输出能力仍由
  上游明确报错，不再由 Xenon 静默缩短。
- ReAct 遇到残缺 JSON、明显停在半句/未闭合 Markdown 的 `final_answer` 时
  fail closed 并要求基于已有工具证据完整重写；完整 JSON 后的游离文本会被
  丢弃，避免混入未请求来源。
- 子 Agent 最终回答及无结构化回答回退不再按 2000/1000 字符裁剪；流式协议
  明确返回 `finish_reason=length` 或 `stop_reason=max_tokens` 时拒绝保存半截
  内容。

### 执行边界：修复文档读取请求误路由

- 保留用户请求中位于“请你分析/读取”之前的文件路径证据，避免 `.pdf`、
  `.tex` 简历或论文请求被误判为 `ANSWER_ONLY`，从而进入不带工具 schema
  的 direct 模式。
- 补充“读简历文件”“读工具”等常见中文口语只读请求的识别，并新增回归测试。

### MCPRegistry 健壮性对齐（评审驱动的工程修补）

针对外部代码评审（Cilpiot）指出的高优先级问题，对齐 CLAUDE.md 的
「server:tool 统一命名空间」契约：

- **短名歧义不再隐式路由**：多个 MCP 服务器提供同名工具时，短名不注册、
  call_tool 报明确错误并给出 `server:tool` 全名提示；唯一短名仍作为
  便利别名注册（向后兼容）。`/mcp remove` 后重新 discover 会重算歧义集合。
- **并发安全**：MCPRegistry 共享状态（clients/tool_map/_pending_configs/
  tool_categories/短名追踪）写入路径全部加 RLock 保护。
- **类型守卫**：discover_tools 跳过非 dict / 无合法 name 的工具条目，
  不再以 "unknown" 占位；call_tool 对缺失 name 的工具信息做兜底，
  不再 KeyError 崩溃。
- **evidence 脱敏**：call_tool 写入证据链的异常与结果摘要经
  `_redact_text` 脱敏（API key/Bearer/URL query），与 callbacks 的
  R7 参数级脱敏互补。
- **infer_category 词边界**：关键词匹配改为词边界正则（"web" 不再误中
  "webhook"），并标注该分类为仅供展示的死数据、不应做路由决策。
- 测试：新增 tests/test_mcp_registry_robustness.py（16 个用例：
  歧义/全名路由/类型守卫/脱敏/并发/词边界）。

## [0.8.2] — 2026-08-13

### SWE-bench 官方评测提升（同模型对比实证）

同模型（deepseek-v4-flash）、同参数（--max-steps 10）、同实例集
（seed 20260729）对比 v0.8.0-dev：

- **instance-level：33.3% → 40.0%**（10/30 → 12/30，+6.7pp）
- **cell-level：34.8% → 45.8%**（24/69 → 33/72，+11.0pp）
- 12 个 resolved 实例覆盖 8 个仓库，含历史硬实例 sympy-14817
- 即便最便宜模型 + 单次尝试，40.0% 已超过混合模型基线 36.7%
- 完整报告：`evals/results/SWEBENCH_REPORT.md` §2.5
  （v0.9.2 仓库清理后原始产物已移出主分支，可从 git 历史
  `git show 6ca8695^:evals/results/SWEBENCH_REPORT.md` 取回）

### 框架能力提升

- **最大迭代轮次扩大**：ReAct 默认 10 → 40、PlanExecute 默认 20 → 40、
  组合引擎 8 → 24、Reflection 3 → 8，BudgetManager 动态奖励封顶
  2× → 3×（base 40 + bonus 最多 120 = 160 上限）。复杂任务（SWE-bench
  类「读 → 改 → 验证 → 失败反馈 → 再改」循环）此前在 32 轮内可能
  提前耗尽预算；扩大后信息收集与迭代验证空间充足，简单任务不受影响
  （预算只是上限，final_answer 收敛即退出）。
- **交付闸门补救循环（「贴 diff 不落盘」根因修复）**：FileClaimGate
  拦截此前直接 raise（拦截 ≠ 修复）。v0.8.2 起 ReAct 增加第三纠偏
  循环——交付预检失败时注入补救提示（「请实际调用 write_file 落盘，
  不要只输出 diff」）再迭代再验证（最多 2 次，超限保持 fail-closed）；
  PlanExecute 在汇总前预检，失败追加「落盘补救」步骤。SWE-bench
  最大失分点从「拦截判负」变成「拦截 → 引导落盘 → 重验」。
- **验证链闭环**：PlanExecute 新增 `_ensure_verification_loop`——
  修改已落盘但测试命令失败（pytest 等）且无成功测试时，把失败输出
  反馈给 LLM 追加一轮「读取失败 → 定位根因 → 修改 → 再验证」修复
  （SWE-bench 硬实例 sympy/django 的共同失败模式）。

### 测试

- 新增 5 个回归测试（test_delivery_remediation.py：补救提示生成、
  ReAct 拦截→补救→落盘、重试耗尽不挂死、验证闭环触发/跳过），
  全套 2110 测试通过，Ruff / compileall 全绿。

## [0.8.1] — 2026-08-11

### 新特性：Mid-task Steering — 任务运行中的人机协作转向

Codex / Claude Code / Hermes 都支持任务运行中用户补充要求，Xenon
此前是同步阻塞模型——引擎 run() 期间 stdin 不被读取，用户只能
Ctrl+C 取消，无法「转向」。本特性在 **BaseEngine 抽象层**实现统一的
steering 通道（7 引擎一处继承，不各自实现）：

- **`engine.steer(text)` 线程安全入队**：任务运行中任意时刻注入补充/
  修改要求；消息先入队，引擎在下一个迭代检查点消费——当前工具调用
  不被打断（避免副作用中途掐断留下脏状态）。
- **ReAct**：迭代循环顶部消费，以 user 消息并入对话，让 LLM 自行判断
  补充/修改/新增语义并调整后续步骤。
- **Plan-Execute**：串行与 DAG 波次循环顶部消费，注入后续步骤的执行
  prompt（并行波次不传递，避免并发注入语义混乱）。
- **Reflection**：修正轮边界消费，并入下一轮执行的 feedback。
- **组合引擎（Plan-React / Plan-Reflection / React-Reflection）**：通过
  `SteeringMixin` 在 run() 阶段边界消费——组合引擎不是 BaseEngine 子类
  且子引擎 run() 起点会清空预注入队列，因此 steering 在组合层持有、
  在步骤/修复阶段边界拼进传给子引擎的 prompt。7 引擎全部支持 steering。
- **REPL 输入监听线程**：引擎运行期间后台线程读取 stdin，非空且非
  斜杠命令的输入通过 `steer()` 注入并打印「↪ 已收到你的补充，Agent
  正在调整计划…」；斜杠命令仍由主循环处理，避免双线程竞争 stdin；
  非 TTY / prompt_toolkit 会话静默降级。
- **真实 LLM 端到端验证**：任务运行中注入「给 add 函数加中文
  docstring」，引擎消费后 LLM 自主调整——add 获得完整 docstring 且
  顺带统一了 multiply 风格，已完成工作未被重做、函数行为不变。

### 安全

- **会话名路径穿越修复（`/save`）**：`save_session` / `load_session` /
  `delete_session` 三个入口此前直接把用户输入拼进文件路径，
  `/save ../evil` 可把会话文件写到 `~/.xenon/sessions/` 目录之外
  （实测写入 `~/.xenon/evil.json`）。新增统一解析点 `_session_path()`：
  拒绝路径分隔符 / `..` 分量 / 绝对路径 / 空名 / 首尾空白，
  resolve 后二次确认目标仍在会话目录内。中文会话名不受影响。
  回归测试 12 例（含「零文件逃逸」断言）。

### 修复

- **工作流配置解析输入校验**：`parse_workflow` 此前对 YAML 结构全部
  假设 dict/list，畸形配置向用户泄漏 Python 内部错误
  （`AttributeError: 'str' object has no attribute 'get'`）。
  新增统一校验层：models/nodes 容器类型、逐节点 dict 校验、节点 id
  去重、YAML 语法错误与空文件识别，全部转为带定位信息的 ValueError。
- **`_build_tool_node` 参数静默丢弃**：此前只转发 17 个固定参数，
  ToolNode 支持的 `limit` / `cursor` / `files` / `edits` 等 40+ 参数
  在 YAML 中配置后会被静默忽略。改为白名单校验（拼写错误的字段名
  立即报错）+ 全参数透传。
- **断路器把调用方错误计入熔断**：`ToolExecutor` 的异常分支此前对一切
  异常无条件 `record_failure()`，LLM 连续 3 次构造非法参数（如
  `read_file` 缺 `file_path`）后断路器开启，后续参数完全合法的调用
  也被「连败熔断」拒绝（实测复现）。断路器语义修正为只度量工具自身
  故障（超时/网络/限流）；参数缺失、文件不存在、安全拦截等终端错误
  不再计入熔断。`_TERMINAL_PATTERNS` 同步补齐「需要 xx 参数」「路径
  越界」「危险命令」等参数错误与安全拦截模式。
- **MCP 服务器名零校验**：`add_server_pending` 惰性注册此前接受空名、
  含 `:` 的名字——后者与 `server:tool` 工具命名空间冲突，会让
  `call_tool` 的路由解析产生歧义，且惰性模式要到首次工具调用才失败。
  新增 `_validate_server_name()` 统一校验（空名/首尾空白/冒号），
  并在注册时校验 command/url 至少其一。
- **`ModelPool.from_config` 输入校验**：顶层 list、entry 为字符串此前
  泄漏 `AttributeError`；`weight` 为字符串或负数静默通过、破坏加权
  调度。新增类型校验 + `_coerce_weight()` 收敛（数字字符串自动转换，
  非数字/非正数报 ValueError）。
- **快捷指令名零校验**：`ShortcutManager.create` 此前接受 `../evil`、
  `a/b`、空名、空步骤——名字会动态注册成 `/<name>` 斜杠命令，污染
  命令命名空间。新增命名校验（仅字母/数字/下划线/连字符/Unicode
  文字）与空步骤拒绝。
- **`evals/swebench_xenon.py` 跨目录运行**：此前只能在仓库根目录以
  `python -m` 方式调用，从任意 cwd 直接运行报
  `ModuleNotFoundError: No module named 'evals'`。显式补仓库根目录
  到 `sys.path`，三种调用方式（直接 / `-m` / 仓库内）均验证。

### 测试

- 新增 60 个回归测试（配置校验 11 + 会话路径安全 12 + 输入边界 22 +
  断路器分类 15），全套 2073 测试通过，Ruff / compileall 全绿。
- 本轮缺陷全部由「边界探针模拟调用」方法论发现：对每个公开入口做
  畸形输入 / 路径穿越 / 损坏文件三类探针，与 Evidence Runtime 的
  「LLM 输出是 Claim 不是 Evidence」哲学同源——外部输入皆不可信。

### ABC 加固（边界探针第三轮 + 真实多引擎矩阵）

- **完成型空洞检测**（`hollow_detector.py`）：`_HOLLOW_PATTERNS` 从 15 增至
  22，新增 7 个「声称已完成但无产物」模式（已完成修复 / 搞定 / 问题已解决 /
  已保存 / 已修改 / 已测试）。判定语义与分析型套话不同：完成型只凭
  「无实质结构」（无文件路径/代码块/URL）判空洞——「已完成修复，修改了
  /tmp/x.py 的 add 函数」这类短但附路径的合法交付不再误伤。
- **工具步骤 steering 重规划**（`plan_execute_engine.py`）：真实多引擎矩阵
  发现——计划全为工具步骤（read_file/write_file）时 steering 在波次检查点
  被消费但无处注入（工具步骤是确定性执行，不经过 LLM），用户补充静默丢失。
  新增 `_replan_remaining()`：把补充并入原任务重新 `_plan()`，按已完成
  步骤 id 过滤后重建剩余计划（串行 + DAG 双路径，DAG 重建波次）。
- **DAG while 循环死循环修复**：`for wave in waves` 改 while 时，
  `if not to_run: continue` 跳过索引递增导致无限循环（pytest OOM 复现），
  全跳过/重规划失败路径统一补索引前进防护。
- **已修复项回归锁定**：`/remove_model` 不存在模型报错（此前假阳性 ✅ 已移除）、
  CLI 错误路径非零退出码（此前恒为 0）——实测确认已修复，补回归测试防回归。

### 第二轮深挖（共享分类器 / 权限闸门 / 调度估算）

- **执行策略分类器漏判中文修改请求**（`execution_policy.py`）：
  「帮我重构这个模块」「纠正这个错误」「更新文档」「删除多余日志代码」
  「把重复代码重构成函数」此前整体落到 ANSWER_ONLY，而英文同义请求
  （`improve the function` / `correct the output`）正确判为 WRITE。
  该分类器是全引擎共享层（ReAct/PlanExecute/EvidenceGate 的
  `task_requires_write` 都走这里），漏判会让 Agent 把明确的修改请求
  当成闲聊、不调用任何工具。补齐中文修改动词+代码实体模式与
  「把/将 X 改成 Y」动宾倒装模式，回归测试含防过冲护栏（只读/问答
  请求不得被新模式误判成 WRITE）。
- **权限闸门对未知风险等级 fail-open**（`permissions.py`）：
  `risk_override` 是 ToolExecutor 掌握的最高风险信息（覆盖运行时注册
  的动态工具——名字不在静态工具表），但非法值（如 `BOGUS_RISK`）
  此前静默 fallthrough 到「允许」。权限层遇到无法识别的风险等级现在
  一律按 CRITICAL 处理（fail-closed）。
- **难度估算器把空输入/刷屏内容调度成困难任务**（`difficulty_estimator.py`）：
  空串此前落到默认 intent 基础分 0.6（tier 3 标准任务）；「x」*100000
  因长度加分冲到 tier 5。长度是信息密度的弱信号——重复内容长度大
  但信息量为零。空/纯空白输入现在固定最低复杂度，重复刷屏内容
  （unique 字符 ≤4）跳过长度加分。

### 真实链路验证（heyroute/deepseek-v4-flash，成本 <¥0.01）

修复后用真实 LLM 跑端到端修复任务（「修复 calc.py 中 add 的减法/加法
错误，用工具实际改文件」），完整证据链闭合：分类器将中文「修复 bug」
正确路由到 WRITE 并真实调用工具；read→edit→write_test→command 测试
→read 复核的工具链完整；host 环境 `python -c` 安全拦截按设计触发且
Agent 自动降级为写脚本执行；edit 前有同文件 read（FactBindingGate 的
read-before-write 证据链完整）；独立复核 `add(2,3)==5` 等断言全过，
最终回答的产物清单与实际文件一致。两轮工作的验证从「单测 + 模拟调用」
闭环到「真实 LLM 端到端」。

## [0.8.0] — 2026-08-07

### 全新：Evidence Runtime 在线验证链（框架级）

将验证从"事后审计"升级为"贯穿任务全生命周期的在线验证"：

- **跨层公共门面 `EvidenceRuntime`**：`AgentContext.evidence` 统一入口，
  REPL / Engine / ToolExecutor / Node / MCP / Session 任何模块都可写入
  或查询同一条证据链（哈希链防篡改，追加式账本）
- **9 阶段生命周期事件**：任务摄入 → 理解 → 规划 → 工具前校验 →
  工具执行 → 工具后状态 → 验证 → 补丁绑定 → 交付（含 EvidencePack）
- **确定性 Gate 管线（零 LLM，零额外 token）**：
  - `PlanCompletenessGate`：任务需写则计划必须含写步骤
  - `TaskCompletionGate`：需写任务必须有成功写执行
  - `FixBindingGate`：补丁必须绑定根因（重复+特判模式拒绝）
  - `FactBindingGate`：写文件前须有同文件读取证据（盲写阻断，enforce）
  - `FileClaimGate`：交付时验证 LLM 声称的文件确实经工具修改
- **生产 enforce 模式**：ReAct / PlanExecute 默认启用盲写阻断；
  Gate 误杀修复后实测零误杀，真实拦截 4 例"贴 diff 不落盘"幻觉
  （注：v0.9.x 起交互路径证据门固定为 observe 审计级——校验语义统一收敛
  到回合级 TurnGate，见 0.9.2 条目；此处保留 v0.8.2 时期的历史行为描述）
- **文件声称提取修复**：代码引用（`self.c`）、diff 头（`a/` `b/`）、
  并列声称（"已保存 A 和 B"）三类误判根因修复 + 14 回归测试

### SWE-bench 官方评测（SWE-bench_Lite，30 实例）

> 注：原始预测/评分文件在 v0.9.2 仓库清理（6ca8695）时移出主分支，
> 仍可从 git 历史取回（`git show 6ca8695^:evals/results/…`）；
> 复现流程见 docs/XENON_EVAL_PROTOCOL.md。

- 基线（混合模型）：**11/30 = 36.7% instance-level**（45.8% cell-level），
  完整方法论 + 预测文件（历史归档于 `evals/results/`，见上注）
- 同模型回归验证：9 单元格 A/B，基线通过单元格 **6/6 全部保住
  （100% 回归保护）**，Gate 误杀 0
- 全 flash 同模型重跑：补丁产出 **70/150**（plan-react 18、react-reflection
  21，较基线 +50%）；通过率 33.3% instance-level（低于混合基线，归因于
  flash 模型质量，非框架回归）

### 其他

- `MCPRegistry.call_tool` 支持 `context` 参数，调用前后记录证据
- REPL 任务摄入事件 + Session 证据快照持久化

## [0.7.4] — 2026-07-31

### 架构重构：命令层可扩展

- **Slash 命令按主题拆分为 12 个独立模块**（`command_groups/`），每个命令组是一个可独立维护的文件：
  - `agent.py`（/run /ask /code /sub-agent）
  - `cache.py`（/cache /cost /fix-cache）
  - `memory_cmd.py`（/memory v1+v2）
  - `model.py`（/set_model /models /pool /mode 等 11 个命令）
  - `resources.py`（/mcp /skill /tools /status /setup）
  - `runtime.py`（/thinking /stream /optimize /verbose）
  - `session.py`（/exit /help /history /save /load /resume 等 12 个命令）
  - `shortcut.py`（/shortcut）
  - `skill.py`（/skill 全部子功能）
  - `workspace.py`（/project /edit /permissions /vision）
  - `common.py`（共享 confirm_action、console）
- `commands.py` 从 2379 行精简到 142 行（纯 re-export 层）
- 新增命令只需在对应组文件加 `@command_handler`，不需触碰 `commands.py`

### 架构重构：REPL 层

- 终端输入处理提取到 `repl_input.py`（575 行），自建 Unix/Windows 输入与 REPL 主类解耦
- `_ShiftTabSignal` 移至共享 `input_buffer.py`

### 架构重构：LLM Client

- 新增 `llm_clients/` 包，定义 per-provider 扩展契约
- `llm_client.py` 新增 provider 分隔注释，标明 OpenAI-compatible / Anthropic-native 边界

### 文档

- README 重写：新增 ASCII 架构图 + "如何扩展"四段代码示例（工具/引擎/命令/provider）
- ARCHITECTURE.md 更新：大文件拆分进度表、目录树反映新包结构

### 质量

- 全量测试 1810 passed，覆盖率 70%
- CI 7/7 jobs（3 平台 × 3 Python 版本 + package）
- 新增 68 个命令组专项回归测试

## [0.7.3]
- 真实评测默认将 Provider/MCP 持久化重定向到隔离工作目录，避免评测任务污染用户凭证配置。
- Cache Rails 的诊断 Token 估算改为对中文、日文、韩文按字符保守计数，避免中文上下文被“字符数 ÷ 4”严重低估；该估算仍不用于计费。
- Prompt Lane 归档增加默认 64 条上限，仅保留最近的历史分叉，避免长会话中 `/cache` 诊断输出和内存无界增长。

## [0.7.3] — 2026-07-27

> **版本性质：** 正式发布按模型隔离的追加式 Cache Rails、不可变请求信封、
> 缓存轨道诊断和受能力边界约束的弱缓存亲和路由。

### Added

- 新增 Cache Rails：不可变会话事件流与按模型、引擎、阶段、工具契约、上下文
  epoch 隔离的追加式提示词轨道；模型切走再返回时可继续复用此前精确前缀。
- 新增 `/cache lanes`，仅显示轨道哈希、请求数、事件游标和估算 Token，不持久化
  Prompt 原文；Manifest/Cache Doctor 同步解释轨道代次与历史分叉。
- AutoRouter 在原有能力、健康、显式模型和会话锁边界内，可使用 30 分钟内同契约
  精确前缀作为弱亲和信号；真实缓存命中率仍只采信厂商 usage。

### Changed

- 工作记忆、检索记忆、单轮回答指导和执行权限边界现在冻结进不可变请求信封，
  后续调用不再替换历史中间的动态块；ReAct 不再按轮修改第一条 system prompt。

### Validation

- 完整离线套件达到 1,662 passed / 38 deselected；opt-in DeepSeek 真实验收除
  单轨追加外，新增 Flash/Pro 交替 10 次调用：两个模型各保留 1 条轨道、各接收
  5 次请求、0 次分叉；连续两轮完整验收中，剔除每轮两个模型各自的首次冷启动
  后，16/16 次返回原轨道均有厂商缓存命中，两轮热调用 input token 命中率分别
  为 96.57% 和 96.74%。

### Fixed

- 将开发依赖 Ruff 固定在 `0.16` 之前，避免上游默认规则集变更导致 CI
  在未修改产品代码时突然失败；迁移到新版规则集将单独进行。

## [0.7.2] — 2026-07-24

### Added

- 工具执行生命周期统一为 pending/running/retrying/succeeded/failed/
  timed_out/cancelled/interrupted，并在结果与追踪器中公开尝试次数和耗时。
- 工具运行期间写入隐私安全的有界恢复点；恢复会话时只读操作可显式重试，
  写入、命令和远端变更操作仅提示人工核验，绝不自动重放。
- 恢复点新增按 execution ID 管理的活动执行账本；Plan-Execute 并发工具即使在
  进程被强制终止后也会逐项恢复，不再只保留最后一个任务。账本更新与原子会话
  写入采用同一线性化临界区，避免旧快照覆盖较新的并发状态。
- MCP 调用按远端工具名区分只读、写入和执行风险；查询类 MCP 不再被一律
  当作 CRITICAL 操作，仍对未知或有副作用的远端工具保持保守确认。

### Fixed

- 显式启用并行的 Plan-Execute worker 现在把隔离上下文中的工具生命周期事件
  汇聚回主会话，进程中断后不再丢失并发步骤恢复点。
- 恢复旧版或异常活动账本时，以账本键补齐缺失的 execution ID，并在标记
  interrupted 后正确移除活动项，避免每次启动反复提示同一任务。
- `/resume`、`/load` 和会话列表对 history、context、extra、model_config 等字段
  做类型校验；单个损坏或结构异常的会话不再阻断其他会话，也不会先清空当前历史。

### 稳定性收口验证

- 新增真实 PTY 端到端测试，覆盖权限面板 `[a]` / `[q]` 输入、120 行中英文
  长日志下 Ctrl+O 展开与重绘，以及返回输入后 Ctrl+C 中断。
- 新增 SIGKILL、损坏会话和 6,000 次生命周期转换故障注入；强制终止后并行
  只读/写入任务全部恢复，参数值不落盘，损坏 JSON 不替换当前会话。
- 全部产品与测试代码通过 Ruff；清理历史测试中的废弃 import、无效局部变量和
  无断言空测试，使静态检查债务从 87 项归零。
- 完整离线套件达到 1,646 passed / 36 deselected；300 次带 fsync 的真实恢复点
  写入基准 p50 3.631 ms、p95 4.353 ms，最终会话文件 5,062 bytes。

### 权限确认与终端交互（第二阶段）

- `PermissionGate` 增加可观测状态机：`PENDING`、`APPROVED`、`DENIED`、
  `CANCELLED`、`FAILED`，并保留最近一次 `PermissionRequest`，兼容原有
  `check() -> (allowed, reason)` 接口。
- 确认面板覆盖批量写入、重构、克隆和动态工具等通用参数，并递归脱敏内容、token、
  密钥和凭证；`[y] / [n] / [a] / [q]` 显示保持可见。
- 用户选择 `[q]` 会在共享执行上下文中标记任务取消，ReAct、Plan-Execute 和迷你
  ReAct 停止后续工具调用，不再把取消误当成普通工具失败继续询问模型。
- 终端标签页从等待状态恢复运行时先保持首帧一个动画间隔，避免确认面板关闭瞬间出现
  帧竞争和界面闪烁。

### 统一工具结果协议（第一阶段）

- 所有 `ToolNode` 结果现在附带 `schema_version=1.0` 和 `tool_result` 结构化视图，
  统一提供 `kind`、`source`、`records`、`total`、`matched`、`truncated`、
  `next_cursor`、`filters` 字段；旧版 `content`、`files`、`matches` 等字段保持兼容。
- `list_files` 和 `search_files` 支持稳定排序及可选 `limit` / `cursor` 分页，结果明确
  区分总数、当前页数量和是否还有下一页。
- `web_fetch`、`mcp_call` 的时间/关键词预筛选结果也进入同一协议，后续引擎可依赖结构化
  记录而不是猜测文本是否被截断。

### 实时查询与长列表可靠性

- `web_fetch` 与 `mcp_call` 支持在工具输出截断前按 `start_time` / `end_time`
  和关键词预筛选；兼容 HTML 时刻表、结构化 JSON 以及 12306 风格管道记录，避免
  按时间排序的长列表只把凌晨/上午前缀交给模型。
- “为什么被截断”“结果呢”等连续追问会继承最近一次查询/调研的只读意图、原始目标
  与筛选条件，不再误套调试代码模板，也不会被降级为禁止检索工具的仅回答模式。
- 未配置任何 MCP 服务器时不再向模型暴露 `mcp_call`，避免虚构 `train_query` 等
  工具；确认框现在显示实际服务器名，短名称则明确显示“自动路由”。
- 实时查询的最终答案新增完整性门控：仅返回搜索 URL 或仍含电报码/日期占位符时，
  会要求继续执行只读查询并整理实际数据。

## [0.7.1] — 2026-07-23

> **版本性质：** 正式发布 Agent Skills、机器可读集成 CLI、火山方舟 Ark 一等
> Provider、llms.txt 优先文档检索和真实 Skill/MCP 互操作验证；该版本是 Xenon
> 接入 ArkCLI、VeADK 等外部 Agent 生态的稳定契约基线。

### ArkCLI / VeADK 生态端到端验证

- 新增 `xenon integrations verify`：默认只读检查四层 Agent Skill 根目录、MCP
  配置、stdio 命令和凭证权限；显式 `--connect-mcp` 后执行真实
  `initialize → initialized → tools/list` 协议链路。
- MCP 验证支持按服务器选择、0.1–30 秒单请求墙钟上限和最多 32 个服务器；报告
  给出可达数、工具数、协商协议版本与握手耗时，不调用工具，也不回显 env/header
  值或 URL query。
- MCPClient 的请求超时现同时覆盖初始化、工具发现、工具调用和资源请求；HTTP
  transport 复用同一超时配置，避免“doctor 正常但真实连接长期挂起”。
- 使用 ArkCLI 1.0.4 的真实 `+connect --path` 产物验证：24 个内嵌 Skills 全部被
  Xenon 发现，加载错误为 0；生态文档同时明确 VeADK 协议兼容与正式 runtime
  适配的边界。
- 验证结果：关联套件 79 passed、完整离线套件 1600 passed / 36 deselected；
  VeADK 官方 `mcp==1.26.0` weather 示例完成 initialize/list/call 全链路 479.6 ms，
  最终 wheel 的真实握手为 460.7 ms。

### llms.txt 优先文档检索

- 新增只读 `docs_fetch` 工具：从文档页确定性发现站点根或 docs 子路径的
  `llms.txt`，解析 H1、摘要、H2 文件列表和 Optional 分组，并按用户 query
  在本地排序相关链接，不产生额外 LLM 调用。
- 兼容 `llms-full.txt`、`llms-ctx.txt` 和 `llms-ctx-full.txt`；站点未提供
  llms.txt 时透明降级到原 URL 的 HTML 抓取，并在结果中返回 strategy、尝试记录、
  选中来源、失败来源和截断状态。
- 文档抓取沿用 SSRF、DNS/IP 和逐跳重定向保护；最多发现 4 个入口、读取 8 个
  链接页、返回 30,000 字符，单个链接失败不会推翻其余有效文档。
- ReAct、Plan-Execute、原生工具 schema、执行策略、并行只读工具、收束预算、
  上下文压缩和 `/tools` 已统一识别 `docs_fetch`。

### 火山方舟 Ark 一等 Provider

- 新增正式 `ark/<model>` provider，默认数据面为
  `https://ark.cn-beijing.volces.com/api/v3`，支持 `ARK_API_KEY`、
  `ARK_BASE_URL`、`/setup` 和实时 `/models` 发现；不再要求把方舟伪装成
  custom provider。
- 兼容读取旧 `_custom_providers` 中的官方 Ark 配置；兼容视图不静默改写文件，
  删除 Ark 凭证时会清理旧条目而保留其他自定义厂商。
- Ark 模型目录按 `task_type` / 输出模态过滤，只把文本生成模型加入聊天池；
  同时读取真实上下文窗口、输出上限和 function-calling 能力元数据。
- 模型目录分页复用单个 HTTP 连接，真实 126 条目录发现由约一分钟级降至
  1 秒内；离线时回退到经过核对的文本模型列表。
- 普通对话、流式输出和原生 function calling 复用统一 OpenAI-compatible
  协议；流式请求显式启用 usage 尾帧，缓存统计兼容
  `prompt_tokens_details.cached_tokens` 并保持 `ark/<model>` 统计身份。

### Agent Skills 兼容层

- 原有 `~/.xenon/skills/*.yaml` 配方之外，新增标准目录式
  `<name>/SKILL.md`；兼容共享 `~/.agents/skills`、Xenon 用户目录以及项目级
  `.agents/skills` / `.xenon/skills`，按“共享用户 → Xenon 用户 → 共享项目 →
  Xenon 项目”确定性覆盖。
- 启动与 `/skill list` 只读取 YAML frontmatter；技能正文仅在命中时加载，
  `references/`、`scripts/`、`assets/` 不会被预先塞入 Prompt。
- 标准技能显式调用后进入 Xenon 的 ReAct 与权限链路，保留工具确认、执行边界和
  MCP 能力；SKILL.md 中的文字不会被误判成用户的持久记忆指令。
- 资源读取新增目录穿越、符号链接逃逸、UTF-8、单文件 128 KiB 和最多 500 个
 资源索引保护；单个损坏技能被隔离，不再导致整个技能注册表清空。
- 新增 `/skill doctor`，显示扫描根、格式计数与逐文件错误；删除项目覆盖版本后，
  下层同名用户技能会自动恢复可见。

### 外部集成 CLI

- 新增 `xenon integrations describe --json` 版本化能力契约，公开 Agent Skills
 目录、MCP 传输能力和稳定命令模板，外部安装器无需猜测 Xenon 私有配置。
- 新增 `xenon skill install/list/doctor`；安装会先校验完整目录与符号链接边界，
  使用同文件系统临时目录原子落盘，支持四种作用域和显式 `--force` 替换回执。
- 新增 `xenon mcp add/list/remove/doctor`；stdio 环境变量和 HTTP 认证头可以通过
  stdin JSON/YAML 安全注入，结构化输出仅显示键名，不回显 token、header 值或
  URL query。
- MCP 配置写入加入跨进程锁与 `0600` 原子文件权限；REPL 惰性连接链路现可把
  持久化的 stdio env 和 HTTP headers 完整传递到 transport。
- JSON 模式保证 stdout 只含一个结构化对象；语法错误、业务错误和成功分别使用
  稳定退出码 2、1、0，便于 Ark CLI 等外部 agent helper 自动判断结果。

### 会话凭证安全

- 自动保存、`/save` 和旧会话迁移统一移除 `api_key`、token、authorization、password 等凭证字段；会话只保存恢复所需的非敏感模型元数据。
- 读取旧版会话时原子清理已落盘凭证并保持 `0600` 权限，不删除对话、工具轨迹或工作记忆。

### 用户意图与执行边界

- 新增“仅回答 / 只读 / 可写入 / 可执行”四级逐轮执行策略；Prompt 优化、难度路由和思考范式不能把代码生成擅自升级为写盘或命令执行。
- `write_code` 默认仅在对话中返回代码；“输出到对话”“不写入文件”“不要执行”等显式限制具有最高优先级。
- 执行边界下沉到 ToolExecutor：只读任务不能写，写入任务不能执行 shell，权限确认不能越过用户当前指令授权；MCP 工具按远端动作名保守分级。
- 新增独立 `research` 调研意图；“打算提交到某平台，请查一下……”按最后一个明确请求子句判定为只读，不再把背景中的“提交”误当成本轮写入授权。
- 只读调研会从原生工具 schema 中移除 `clone_repo` 等写工具，底层 ToolExecutor 同时保留硬拒绝，模型无法通过重试或权限确认越界。
- 代码落屏前新增完整性保护：过滤工具协议，校验闭合代码块，Python 使用 AST 解析；损坏或截断回复在展示前自动重试，裸代码统一包装为 Markdown 代码块。
- 新增真实 DeepSeek 回归：用户要求“输出到对话、不写盘、不执行”时不进入 ReAct，返回代码可解析且临时目录无新增文件。

### 调研任务与长工具收敛

- `github_fetch` 新增 `repo_activity`，以统一的最近 30 条 PR 抽样返回 push、更新时间、近期活动和中位合并耗时等维护信号；结果明确标注为公开样本而非厂商 SLA。
- GitHub API 遇到限流、服务端故障或网络失败时自动降级读取公开 HTML；组织、用户和搜索页继续走通用网页读取，不再误报 `owner/repo` 格式错误。
- 有副作用的写入、命令、Git 与克隆工具不再因超时被执行器自动重放；克隆超时会清理不完整缓存并返回确定性失败。
- `clone_repo`、`command` 和 `git` 的交互式活动行持续显示已耗时、超时上限和 `Ctrl+C` 取消提示；成功、失败、中断与异常路径都会回收进度线程。
- ReAct 连续三次工具失败后停止无效探索，使用已有证据生成最终答复；GitHub API/HTML 双重失败也不会无限重试。

### 启动模型状态

- 提供商发现提前到欢迎卡之前，`MODEL` 现在展示实际已加载的首选模型与总数，不再先显示“未配置”后又加载模型。
- 默认启动时仅抑制模型列表探测产生的 `httpx` INFO 噪声；`-v` 仍保留完整诊断，正常任务的 Ctrl+O 日志不受影响。
- 无效提供商以脱敏摘要显示（如“认证失败 HTTP 401，已跳过”），不再输出原始请求行或响应正文；欢迎卡后统一展示可用模型和提供商数量。

### 家目录隐私边界

- `$HOME` 现在是明确的账户边界，不再因 `.git`、`package.json`、`pyproject.toml` 等标记被当作项目；家目录文件树、关键文件和项目规则不会自动注入模型。
- 无项目模式仍加载用户全局指令，但项目上下文根保持为空；具体的无标记子目录仍可作为有界 scratch 工作区，不会向上继承家目录标记。
- 记忆注册表支持无项目状态，此时只启用 `user` 与 `session`；普通“记住”默认落入用户全局，显式 project-local/project-shared 会要求先进入项目。
- `/memory status` 明确显示未激活的项目作用域，`/project refresh` 同步重建记忆边界；不再以 `Path.cwd()` 回退并在家目录创建项目记忆文件。

### 权限确认可用性

- 修复 Rich 将权限面板中的 `[y]`、`[n]`、`[a]`、`[q]` 误当作标记并吞掉的问题；面板现在明确显示每个操作的输入键，底部输入提示同步显示 `[y/n/a/q]`，并接受大小写输入。

### 缓存观测核心层

- 为普通、流式和原生工具调用统一生成隐私安全的 Prompt Manifest，按模型、引擎、阶段、稳定前缀、工具模式和上下文压缩代次划分缓存族。
- 新增逐请求缓存事件：记录厂商真实 hit/miss token、字段覆盖率、预期可缓存比例、前缀效率以及 cold/warming/warm/unavailable 状态；不持久化原始 Prompt 或工具内容。
- 本地 JSONL 历史采用有界滚动存储；明确区分“厂商返回 0 命中”和“厂商未提供缓存字段”，避免把未知错误显示为 0%。
- Reflection、Plan、ReAct、Novel、组合引擎与 Direct 对话均标注独立调用阶段；上下文 compact、clear、undo 会开启新的缓存代次。
- 新增 `/cache status`、`/cache explain`、`/cache history`、`/cache doctor`，分别展示当前状态、最近一次证据、跨会话隐私安全历史和确定性诊断。
- 状态栏使用 cold/warming/n/a/实际命中率语义；`/cost` 与退出报告不再把未提供缓存字段错误显示为 0%。
- 普通文本、流式与原生工具请求统一经过五层 Prompt Compiler：STATIC、SESSION_STABLE、HISTORY、VOLATILE、CURRENT；编译器保持消息与工具协议语义顺序不变。
- 工具 schema 和 response format 递归规范化并按工具名稳定排序，注册顺序变化不再制造新的工具前缀；动态 system 内容会进入 `/cache doctor` 告警而不会被静默改写。
- AutoRouter 新增保守缓存亲和：只接受 30 分钟内厂商真实 hit 证据，并只在同 tier、健康、基础分差不超过 0.25 的模型间打破平局；显式模型、会话锁、能力和健康始终优先。
- 新增 `/cache optimize --dry-run|--apply|--disable` 与真实 `/fix-cache` 别名；设置以私有本地 JSON 原子保存，可逆且不会改写 Prompt、工具协议或制造付费预热请求。

### 缓存前缀稳定性与本地版本一致性

- 上下文注入拆分为稳定层与易变层：固定引擎指令和项目上下文前置，已有对话历史保持连续，工作记忆与按轮检索记忆靠近当前用户请求。
- 单轮 Prompt 指导不再作为会变化的 system overlay 插到历史之前，改为绑定到本轮用户消息；提示文案不再把“结构化 Prompt”和缓存提升作未经验证的因果关联。
- Direct 流式路径和各推理引擎的 DeepSeek 模型 ID 统一归一为 `deepseek/<model>`，避免 `/cost` 把同一模型拆成两个统计桶。
- 欢迎页与 `xenon --version` 统一读取运行源码的 `xenon.__version__`，避免 editable install 的旧 distribution metadata 显示过期版本。

### Star Core 品牌与终端活动状态

- 正式视觉标志从圆角方形/六边形轨道更新为 Star Core：八芒氙蓝星核、非对称群星与断续轨道，小尺寸不再退化成方块。
- 启动动画、README Logo、社交预览和工作流标题统一使用 Star Core 视觉语言。
- 新增终端标签状态机：模型、工具和命令执行时以固定宽度星位循环闪耀；等待输入、权限确认、记忆确认和任务完成时保持静止。
- 标签动画仅在交互式 TTY 启用，支持 CI/dumb 终端自动降级、手动关闭、ASCII 帧和退出标题恢复。
- 活动线程惰性启动、daemon 化并在所有退出路径回收；标题写入失败不会影响模型或工具执行。

### 验证

- 完整离线回归：`1600 passed, 36 deselected`。
- Ruff、`compileall`、SVG XML 校验、`git diff --check`、wheel 与 sdist 构建通过；家目录真实 TTY 启动下的 `/project` 与 `/memory status` 已验证。

## [0.7.0] — 2026-07-22

> **版本性质：** 新增 User-Governed Memory 第四大产品支柱，并完成终端交互、工具协议、权限回执、上下文连续性和 DeepSeek V4 兼容性的系统性加固。

### User-Governed Memory

- 新增 `user`、`project-local`、`project-shared`、`session` 四层作用域；自动候选默认项目本地，未经确认绝不持久化。
- `metadata.json` 保存权威状态与创建/更新/检索/使用时间、计数、重要度、置信度、固定、过期、来源和替代链；小型 Markdown 分类文件供用户直接检查。
- 单条、分类、作用域和上下文注入均有 token 阈值；私有作用域超限后按重要度、置信度、时间、检索和成功使用次数自动归档，固定和项目共享记忆不自动淘汰。
- 原子替换与跨进程事务锁共同保护读改写；线程及真实多进程并发写入不会丢失记录。
- 潜在冲突只提示、不静默覆盖；`/memory replace` 与 `/memory rollback` 建立可逆的 supersession 版本链。
- 新增 `/memory status/list/search --explain/inspect/doctor/add/archive/restore/pin/unpin/migrate` 操作面；损坏作用域可诊断且不阻断其他作用域检索。
- `XENON.md`、`XENON.local.md`、`AGENTS.md` 后备层级与安全 `@path` 导入；限制根目录、符号链接、循环、深度和总字节预算。

### 终端与工具调用修复

- `Ctrl+O` / `Shift+Tab` 通过 prompt-toolkit 的终端切出机制重绘，展开折叠详情不再打乱固定输入区和状态栏。
- 权限确认显示规范化后的真实命令/参数，不再出现 `命令: ?`；会话级放行按精确参数指纹记录。
- ThinkingPanel 正确处理并行工具动作与观察，工具计数和顺序不再错位。
- 识别 DeepSeek 文本形式 DSML 工具调用；direct 模式先验证再渲染，避免把协议标记当普通回复直接显示。
- 项目上下文、长期记忆和单轮提示改为可替换 overlay，各推理引擎统一注入，避免跨轮累积和上下文遗漏。

### 验证

- 离线回归：`1432 passed, 35 deselected`。
- 新增记忆并发、冲突/回滚、完整性诊断、上下文使用计数、指令层级和终端回归测试。
- Python 变更范围 Ruff、`git diff --check`、wheel 与 sdist 构建通过。

### Bug 修复与可靠性补强

- 危险工具统一接入权限闸门，文件写入/编辑/批量变更改为原子、可回滚操作。
- 模型回退区分配置错误与瞬时错误，补齐断路器半开恢复和流式失败处理。
- Plan 串行/DAG 执行传播结构化失败，依赖步骤不会在上游失败后继续执行。
- GitHub 工具支持 HTTPS/SSH/blob/tree/raw URL、私有仓库认证、真实默认分支和安全缓存更新。
- 工具轨迹、工作记忆和自动保存跨轮持久，降低长会话状态丢失。

### DeepSeek V4 兼容性

- 离线模型列表只保留 `deepseek-v4-pro` 与 `deepseek-v4-flash`，默认上下文更新为 1M。
- V4 Pro 默认使用并持久化 `reasoning_effort=max`；普通、流式和原生工具调用均按模型配置透传。
- 修复模型配置按别名保存、引擎按 canonical model ID 路由时配置未生效的问题。
- 思考模式原生工具调用会保留 `reasoning_content`、`tool_calls` 与 `tool_call_id` 结果，支持当前会话跨轮续接。
- ReAct 在正式 DeepSeek V4 为主模型时自动启用原生工具协议，仍可由调用方显式关闭。
- 强制 `tool_choice` 时仅对该次 DeepSeek V4 请求关闭思考模式，避免官方 API 的不兼容组合返回 400。
- 人民币定价快照按 2026-07-21 官方文档校准；旧别名仅用于历史账单匹配。

### TUI 布局更新

- TUI 改为贯穿终端的双线输入区、固定底部状态栏、无边框回答与可折叠工具详情。
- 优化后的 Prompt 改为无边框 dim 排版，模型回复保持正常亮度，HTTP/调试日志降低视觉权重。
- 输入下边界和状态栏分行；状态栏由 `prompt_toolkit` 固定在整个终端屏幕底端。

### 工程质量

- CI 覆盖 Python 3.10–3.12，区分离线/live/e2e 测试，并启用 Ruff、55% 覆盖率和包构建校验。
- 评测运行器移除 Python 3.11 专属的 `datetime.UTC` / `contextlib.chdir`，恢复 Python 3.10 兼容。

## [0.5.3] — 2026-07-14

### Bug 修复

- **git 工具字段名不一致**：`git` 返回 `output` 字段，而 `command` 使用 `stdout`，导致 LLM 解析工具结果时需适配两种字段名。现已统一在结果中同时提供 `stdout` 和 `output`（向后兼容）。
- **search_files 缺少文本表示**：`search_files` 仅返回结构化 `matches` 列表，缺少 LLM 可直接读取的文本格式。现新增 `stdout` 字段，提供 `file:line: content` 格式的文本表示。
- **参数校验拦截后无恢复提示**：当 LLM 使用 `command` 执行超长/复杂 shell 命令被参数校验拦截后，错误消息不提示替代方案，导致 LLM 难以自动恢复。现新增 `_tool_alternative_hint()` 函数，拦截时自动建议对应工具（如 `command → search_files / read_file / list_files`）。

### 质量验证

- **L1 工具层压力测试**: 10/10 全绿（76 次工具调用，92 步骤，0 异常）
- **L2 端到端 LLM 测试**: 4/4 全绿（14s–43s，多轮 ReAct 推理 + 工具链）
- **回归测试**: 1110/1110 全绿，无破坏性变更

## [0.5.2] — 2026-07-14

### UI 重构（prompt_toolkit 集成）

将终端输入从自建 Unix/Windows 双路径统一到 `prompt_toolkit`：

- **输入体验**：`> ` 提示符 + 命令/路径/模型名三级补全（`OmniCompleter`）+ 历史持久化
- **状态栏**：底部工具栏实时显示模型 · Token 用量 · 消息数 · 延迟，分隔符统一 `·`
- **流式渲染修复**：移除 Rich `Live` 渲染，改为收集 chunks 后一次性 `Panel(Markdown(...))` 输出，消除双重/残留面板问题
- **回退兼容**：prompt_toolkit 不可用时自动回退到自建输入（`_HAS_PROMPT_TOOLKIT` 标志）

### Bug 修复

- **模型路由空 key**（critical）：自定义模型商（如"豆包"）名称全为中文字符时，`re.sub(r"[^a-z0-9]", "", name)` 生成空字符串 key，导致模型 ID 格式错误（`/glm-5-2-260617`）。在 `register_custom_provider`、`get_configured_providers`、`_check_first_run` 三处加空 key 兜底 → `"custom"`。

### 文档

- **README 全面重构**（268 → ~570 行）：新增目录、功能特性表、38 条命令参考、安装详解、配置指南、FAQ、故障排查、贡献指南
- 修正工具列表（删除不存在的 `edit_with_llm`，补全 `create_directory`/`weather`/`refactor`）
- 所有 badge 更新到 v0.5.2，测试数 1110+

### 工程

- `__version__` 从 0.1.0 → 0.5.2（三处统一：`__init__.py` / `pyproject.toml` / 代码兜底值）
- 新增 `xenon --version` 参数
- 创建 `LICENSE`（MIT）
- SVG terminal demo 从 v0.1.0 重绘为 v0.5.2 风格
- `ARCHITECTURE.md` 修正 provider 数量（6 → 12）

### 约束

- 1110/1110 测试全绿
- PTY 端到端验证 5 类场景全部通过（边界/错误/命令/长对话/中断）
- 不修改引擎层代码

## [0.4.1] — 2026-07-13

### 分层上下文压缩系统

- 6 步压缩流水线：摘要 → 工具输出精简 → 去重 → 评分 → 裁剪 → 重组
- Token 窗口达 80% 时自动触发
- 三层策略（轻度/中度/深度），按消息重要度评分保留语义最密集内容

## [0.4.0] — 2026-07-12

### 多优先级队列调度 + 自动路由

- **ModelPool**：5 级优先级队列（Q1-Q5），模型按能力自动分层
- **AutoRouter**：根据任务难度（`DifficultyEstimator`）自动选择合适模型
- **工作窃取调度**：高优先级任务可借用低优先级队列空闲模型
- **BenchmarkFetcher**：新模型注册时自动查 HuggingFace Leaderboard 定级
- **会话恢复**：`/resume` 命令，关闭终端后可恢复上次会话（7 天过期）

### REPL 命令扩展

- `/pool` — 查看五级优先级模型调用池
- `/history` — 路由调度决策追溯
- 动态模型商注册（`/setup` 菜单选项 6）+ `setup_wizard → ModelPool` 链路打通
- 7 个引擎透传 `model_pool` + `auto_router` 参数

### HumanEval 基准

- 官方 `openai/human-eval` 评测适配器（`evals/humaneval_runner.py`）
- pass@1: 88.4%（145/164，deepseek-v4-pro）

### Bug 修复

- `_load_credentials` YAML 优先于环境变量（对齐 `provider_registry`）
- `extract_code` 重写，修复 HumanEval completion 提取鲁棒性
- 粘贴模式 ESC 序列处理器吞掉 paste end → REPL 挂死（C-1）
- bash 风格 Ctrl+C 二次确认退出（C-3）
- anthropic 兼容 `ANTHROPIC_AUTH_TOKEN`（C-2）
- B-1/B-3/B-4 子代理真实场景 bug 修复

## [0.3.0] — 2026-07-08

### 仓库清理（方向 B 起跑线）

把仓库根目录从 6 个项目文件压缩到 6 个 + 整理 5 个"无主文件"到 `docs/`：

- 删除 `binary_search.py`（与项目无关的练习题）
- 迁 `Xenon_CLI_Design_Specification_v1.1.pdf` → `docs/xenon-design-spec-v1.1.pdf`
- 迁 `xenon_design_spec_v1.1.html` → `docs/xenon-design-spec-v1.1.html`
- 迁 `REAL_TASK_TEST_REPORT.md` → `docs/reports/v0.2.2/`
- 迁 `VERIFICATION_REPORT.md` → `docs/reports/v0.2.2/`
- 补 `.gitignore` 加 `.claude/`（本地 Claude Code sub-agent 定义不入公共仓库）
- 仓库 size 3MB → 1MB，专业度立竿见影

### 差异化定位落地（方向 B：MCP + 多模型 + 多范式三合一）

3 个文档全部基于代码事实（不夸大）：

- `README.md` 顶部从中性 "Local Multi-Model Agent Runtime" 重写为方向 B 一句话定位 + 三件合一能力卡片 + 8 范式 + 20 工具 + 三件套
- `docs/COMPARISON.md`（新增 155 行）—— vs Aider / Claude Code / OpenCode / Crush 在 8 维度能力矩阵（MCP / 多模型 / 多范式 / 本地优先 / 工具断路器 / 上下文压缩 / 空洞回答检测 / 三阶段预算）+ 7 类场景推荐
- `docs/ARCHITECTURE.md`（新增 295 行）—— 8 引擎分类（直答/循环/计划/审查/创意 + 3 组合）+ 路由层 + 三件套 + ToolExecutor 7 阶段 + MCP 双传输

**重要事实修正**（不夸大、不藏）：
- xenon 实际有 **8 个引擎**（含 NovelEngine，README 之前漏提）
- MCP 子进程用 `select`+墙钟超时替代阻塞 readline（B11 修复）—— 真实
- 子进程退出用 `terminate()+kill()` 兜底无僵尸 —— 真实
- MCP server **不自动重启** —— 真实限制，已在 COMPARISON 列为已知后续项

### 评测数据（Real 模式首次跑通，5/20 → 9/20，+80%）

`docs/EVAL_RESULTS.md` v2 报告（157 行 diff）：

- Mock 模式 20/20（框架自检，CI 跑通）
- Real 模式 9/20（45%，DeepSeek-V4-Pro via 火山方舟）
- 工具调用 160 次（v1 56，+186% multi-turn 累积），断路器/异常处理/multi-turn history 路径全部正常

**方案 C 三个根因通用机制修复**（不硬编码、不动评分、不动 expected_tools）：

| 根因 | 修复 | 影响 |
| --- | --- | --- |
| 根因 1：RealAgent 单轮不友好 | `evals/runner.py` `RealAgent` 加 `max_turns=3`，每轮共享 `ContextManager` 累积 history，前一轮 `answer` 注入后一轮 user_input 作为 review feedback | `generate-diff-preview` 等改判成功 |
| 根因 2：workdir 太简单 | `/tmp/xenon_real_workdir`：cp xenon/{xenon,tests,evals,docs} + `.xenon/rules.md`（132 文件 / 114 py） | `use-project-rules` / `code-search-entrypoint` / `code-search-model-router` 等 5 任务改判成功 |
| 根因 3：ReAct 拒绝兜底固定 2 次 | `react_engine.py` 自适应 `max(2, max_iterations // 2)` 重试上限 | `generate-diff-preview` 改判成功 |

**11 个 v2 失败里 4 个仍是任务设计问题**（不是引擎问题）：
`revise-after-test-failure` / `revise-after-review` / `handle-missing-api-key` / `mcp-tool-flow`
需要 REPL 命令介入，RealAgent 只跑 ReAct 工具循环。v3.x 路线：RealAgent 接入 REPL。

### Bug 修复

#### 粘贴模式状态机死锁（Ctrl+Shift+V 粘贴不显示 + 按空格重复粘贴）

**根因**：CHANGELOG v0.2.2 启用了 bracketed paste 模式（`\x1b[?2004h`），但
**paste_mode 状态机在结束信号 `\x1b[201~` 丢失时死锁**：
1. 终端发 `\x1b[200~` → `paste_mode = True`
2. 终端发 `\x1b[201~` **结束信号丢失**（被 select 0.01s 切碎 / 某些终端不响应）
3. `paste_mode` 永远 True（状态机死锁）
4. 用户按空格 → 进 paste_mode 分支被插入 `current_line` 但 `continue` 不重绘
5. 用户看到"按空格不显示 + 字符累积成重复粘贴"症状

**通用机制修复**（不硬编码、不针对特定任务/终端加白名单）：
- 加 `paste_last_byte_at` 跟踪 paste_mode 期间最后字节时间
- select **0.3s 无新字节** → 自动退出 `paste_mode` + 强制 `_redraw_line()`
- 进入 paste_mode 时记录时间戳
- paste_mode 字符处理末尾刷新时间戳
- `\x1b[201~` 正常收到时清空时间戳

### 约束

- 930/930 单测全绿（96.37s）—— 零业务回归
- 评分函数 `_score` **未动**（不硬编码）
- `expected_tools` 列表**未动**（任务定义本身合理）
- 通用机制改进，**不**针对特定任务加白名单
- 不动 `.xenon/` 本地配置目录

## [0.2.2] — 2026-07-08

### 工具可用性全面修复

全量审查 20 个工具，发现并修复 4 个缺陷，新增 48 项工具冒烟测试（974 全量通过）。

- **天气工具 `city` 参数丢失**：`_VALID_PARAMS` 缺少 `city`/`lang`/`description`/
  `python_function`/`command_template`/`params`，`normalize_params` 将其过滤，导致
  天气工具始终查询北京。已补全 6 个缺失参数 + `_PARAM_ALIASES` 别名（`location`→
  `city`、`language`→`lang`）。
- **SSRF 误拦 `198.18.0.0/15`**：Python `ipaddress.is_private` 将 IANA 基准测试段
  归入 private，导致 `wttr.in` 等合法服务被拦截。替换为显式 RFC 1918 + RFC 6598
  私有网络检查（`_is_rfc1918_private`），仅拦截 10.0.0.0/8、172.16.0.0/12、
  192.168.0.0/16、100.64.0.0/10、fc00::/7。
- **`github_fetch` 格式校验崩溃**：`import re` 在条件块内，`github.com` 不在 URL
  中时 `re` 未绑定导致 `UnboundLocalError`。将 `import re` 移至函数顶部。
- **新增 `test_tool_audit.py`**：56 项测试覆盖 normalize_params、SSRF、weather、
  文件操作、command、git、datetime、web_fetch、github_fetch、code_index、
  ast_analyze、diff_preview、register_tool、动态工具、ToolExecutor、安全边界、
  降级方案。

### 工具降级方案

- **天气工具 curl 降级**：`get_weather` 主路径使用 Python httpx 客户端，失败时自动
  回退到系统 `curl` 命令，确保在代理/SSRF/证书等异常场景下天气查询仍可用。
  降级路径返回 `via_fallback=True` 标记。
- **SSRF 已知安全域名白名单**：`_SSRF_DOMAIN_ALLOWLIST` 包含 `wttr.in`、
  `weather.com.cn`、`api.github.com`、`raw.githubusercontent.com`、`httpbin.org`
  等公认公共 API，白名单域名跳过 IP 级 SSRF 校验，作为防御纵深最后一道防线。
- **SSRF 拦截错误提示降级**：`web_fetch` 被 SSRF 拦截时，错误消息包含
  "可尝试用 command 工具执行 curl 获取数据作为降级方案" 提示，引导 LLM 自动切换。

### 终端 UI 全面优化

- **欢迎界面重构**：移除 ASCII 艺术 Logo，改用 Unicode 细线框 + 紧凑信息面板（版本/范式/模型/提示），减少视觉噪音。
- **对话流程统一**：所有元数据（意图/优化/引擎模式/工具切换）统一 `[dim]·` 单行风格，面板边框统一 dim 色，建立清晰的视觉层次。
- **状态栏简化**：精简分隔符，Token 进度条用 `━`/`─` Unicode 字符，低用量时颜色收敛为 dim。
- **思考面板紧凑化**：摘要行去掉 emoji 前缀，折叠详情统一 dim 字体。

### Bug 修复（REPL 真实任务测试发现）

端到端真实任务测试（`tests/test_repl_real_tasks.py` 84 用例 + `tests/test_repl_real_usage.py`
25 个真实使用场景）发现并修复 6 个 bug，全部为 P2/P3 优先级。

- **query 意图路由到 ReAct**（`repl.py:1052-1066`）— query 意图（天气/价格/汇率/新闻等
  实时数据）必然需要工具，direct 模式不向 API 传工具而 prompt_optimizer 注入"使用工具获取
  实时数据"指令会让 LLM 给出前言式回复。`_detect_tool_need` 在 `intent == "query"` 时
  直接判 True，路由 ReAct。
- **B-1 (P2) write_code 意图路由缺失**（`repl.py:1063`）— `_TOOL_PATTERNS` 唯一编程类正则
  要求 `^(?:帮我|请|给).{0,5}` 前缀，无法覆盖"写一个 X"/"用 Y 写一个 Z"等自然语序。
  `_detect_tool_need` 兜底扩展为 `intent in ("query", "write_code")`，共用同一根因路径。
- **B-3 (P2) `_handle_chat` 入口空输入防护**（`repl.py:697`）— 空字符串/纯空格
  直接进完整流程会污染 history（`add_user_message("")`）并浪费 LLM token。
  入口加 `if not user_input.strip(): return` 防护。
- **B-2 (P3) 条件句 query 漏判**（`prompt_optimizer.py:222-241`）— `query` trigger 缺
  "如果…就…"条件句模式与实时天气关键词。补全 2 条正则覆盖"如果今天下雨就告诉我"/
  "今天会不会下雨"。
- **B-4 (P3) chat 模板污染 user content**（`prompt_optimizer.py:265-278`）— chat 模板把
  "（这是一句问候/闲聊…）"指令内联到 user content。改为 `template="{task}"`，
  仅依赖 `system_hint` 注入，避免 user 消息被污染。
- **观察项-1/2 (P2) ReAct 异常状态污染**（`repl.py:836-841`/`repl.py:805-818`）— ReAct
  引擎抛异常或 `_run_direct` 递归 ReAct 失败时，user 消息已 add（`repl.py:745`）但无
  assistant 响应，history 留下孤立 user 序列。改为在异常分支用 `add_assistant_message(
  "[错误] ...")` 占位让 history 仍成对；add 失败兜底 `trim_last_user()`。

### Bug 修复

- `_check_first_run` 提示信息统一 dim 风格。

### 终端输入体验修复

- **Shift+Enter 多行输入**：Linux 端 `_read_input_unix` 仅处理了 `Alt+Enter`（`\x1b\r`），
  未处理现代终端（kitty/WezTerm/gnome-terminal）的 Shift+Enter 序列 `\x1b[13;2u`，
  导致序列被丢弃无法换行。新增 `\x1b[13;2u` 匹配，与 Alt+Enter 同等处理为多行换行。
- **Ctrl+Shift+V 粘贴异常**：粘贴时未启用终端粘贴括号模式（bracketed paste），粘贴内容
  中的特殊字符（如 `\x1b`）被误解析为转义序列，导致字符错乱。启用 `\x1b[?2004h`
  粘贴括号模式，粘贴期间批量修改缓冲区不触发逐字符 `_redraw_line()`，粘贴结束时
  一次性重绘。
- **键入延迟**：粘贴括号模式修复同步解决了粘贴延迟问题——粘贴 N 个字符从 O(N²) 次
  `sys.stdout.flush()` 降为 1 次。正常打字 5-10 字符/秒不受影响。

## [0.2.0] — 2026-07-07

本版本对照《差距分析与改进建议》审核文档（`docs/差距分析与改进建议.md`，31 轮审查
收敛于 v4）的 §9 修复执行清单，完成 P0→P3 全部优先级修复，共 34 次提交、747 项测试
全绿（基线 430 → 747）。每个修复独立提交并推送至 `origin/ubutnu`。

### P0-A 安全与数据完整性（§9.7 第 1-2 步）

- `register_tool` 模式 1 任意 Python 导入 = RCE 收敛；重名工具校验，防劫持内置工具名。
- `command`/shell 工具命令注入收口；`web_fetch` SSRF 黑名单加固（`https://`、IPv6、
  数字编码 IP、重定向）。
- `edit_with_llm` 截断保护；非原子写收敛；凭据文件 `chmod 0600`。
- `model_registry.export_config` 明文导出 `api_key` 收敛；`/set_model api_key=` 凭据
  不再进 argv；`/code --run` 任意脚本执行加 `Confirm.ask`。
- `react_engine` 不再把 `register_tool` 暴露在系统提示中（消除 LLM 循环内自主注册
  `os.system` 工具的 RCE 链路）。

### P0-B 已确认 Bug（§9.7 第 1-2 步）

- B4：去除三引擎共享的 `max_tokens=131072` 硬编码；`chat_completion` 按厂商上限钳制。
- B7：激活 `ModelConfig` 死字段——`base_url` / `api_key` 覆盖真正生效。
- B8：`_verify_llm_file_claims` 扩展工具集（含 `batch_write`/`batch_edit`/`edit_file`）。
- B11：`StdioTransport` 用 `select` + 墙钟超时替代阻塞 `readline`，子进程卡死不再
  永久挂起整个引擎。
- B12：`finish_reason=length` 自动续写，token 耗尽抛 `ResponseTruncatedError`。
- B6：`response_adapter.parse_review` 解析失败默认 `pass=True/score=8` 收敛为
  `pass=False/score=0`，质量门不再静默放行。

### P1-A 横切根因（§9.7 第 3-4 步）

- R1：`_call_llm` 区分终端错误（401/403/400，立即上抛）与瞬时错误（429/5xx/网络，
  切模型），全部失败 `on_error` + 抛 `RuntimeError`。
- R2：抽出 `BaseEngine`，消除四引擎 `_call_llm` 复制与参数漂移（max_tokens/温度/截断
  统一）。
- R3：`llm_client` 原生 function-calling 能力 + per-provider `httpx.Client` 连接池复用。
- R4：`ContextManager.max_tokens` 从激活模型 `context_window` 注入。
- R7：敏感参数脱敏 + 日志级别归位。

### P1-B 核心规范功能（§9.7 第 4-5 步）

- F1：`ToolExecutor` 7 阶段门面 + 断路器 + 参数幻觉校验 + 重试。
- F2：`BudgetManager` 三阶段软预算 + 奖励机制；空洞检测器（15 正则 + 组合判定）；
  mercy compile + 合成注入 + ReAct 集成（面试 Q2/Q3 门面成型）。
- F3：Compactor 6 段结构化压缩 + 三层策略 + 安全截断 + 持久化。
- F4：`ContextManager` 注入引擎 + 引擎内每 5 轮自动压缩（抑制 O(n²) 增长）。
- F5：三层 LLM 降级 `_call_llm_native`（function-calling → 文本 JSON → 兜底）。
- F6：中断检查 + 引擎内预算检查。

### P2 增强（§9.7 第 6 步）

- E1 `DirectoryScout`：项目目录扫描防路径幻觉（Q4 第一道防线）。
- E2 `PlanDAG`：`depends_on` 依赖图 + 拓扑波次并行（ThreadPoolExecutor，规避无锁
  竞争用隔离 ctx/tracker）+ 循环检测 + DAG→串行回退 + 失败级联跳过（修复 §8.27.1）+
  双模型（规划/执行分离）。
- E3 `EventBus`：多订阅者 pub/sub 事件总线（callback 保留为默认订阅者）。
- E4 `ReflectionEngine`：独立 `reviewer_model_priority`（执行者/审查者不同模型）+
  版本回退（达到 max_rounds 返回最高分版本）+ pass/score 一致性 + 空反馈兜底 +
  执行异常回退。
- E5 `spawn_agent` 子 Agent 系统：**暂缓**。审核 §4/§9.5 明确为「最大工程量、最高
  风险，建议放最后，且仅在 BudgetManager + ToolExecutor 稳定后再做」；§8.1.1 指出
  全仓库零 async 基础设施，`asyncio.create_task` 属绿地新建。待集成验证 F1/F2 稳定
  后于后续版本交付。

### P3 工程质量与可观测性（§9.7 第 6 步）

- Q1：`chat_completion` 捕获真实 `usage`（prompt/completion/total tokens + latency）+
  `UsageTracker` + 回调侧信道（不破坏既有返回契约）。
- Q2：每次 run 生成 `run_id`，每次调用 `call_id`，日志带前缀链路追踪。
- Q3：eval 框架修复——prompt 不暴露 `expected_tools`；real 模式跑真实引擎多轮按
  **实际执行**工具评分；mock 标注 smoke test；`success_criteria` 不自动评分改人工复核。
- Q4：`code_index`/`project_context` 持久化 + mtime 增量；`detect` 限制向上层数 +
  遇 `$HOME` 停 + 不跟随符号链接；`_EXCLUDE_DIRS` glob 改 fnmatch。
- Q5：`prompt_optimizer` 意图收紧——`debug` 强信号、novel 续写要求创作语境词、
  补 `write_doc`/`chat`、模板抽配置。
- Q6：`setup_wizard` 保存 key 前连通性测试；识别 `export VAR=` 前缀；删 key 联动
  清理 registry。
- Q7：token 估算 memoization（`ConversationTurn` 缓存）+ CJK 范围扩展 + 注释代码统一。
- Q8：破坏性操作加 `Confirm.ask`（`/clear`/`/load`/`/code --run`/`/shortcut run`/
  `/mcp remove`）；`dispatch_command` 包 try/except 兜底。
- Q9：`combined_engines` 失败步骤中止 + 错误不污染共享 ctx；reactor/reflector 上下文隔离。
- Q10：`_undo_stack` 加上限；`status_bar` render 整体 try/except 兜底。

### 集成验证（2026-07-07）

- 全量单测：**747 passed**。
- CLI：`xenon --help` / `xenon run <workflow.yaml> --dry-run`（配置解析 →
  DAGScheduler 构造 → 拓扑展示）正常。
- Mock eval：20/20 任务通过，0 工具失败，报告生成（Q3 框架）。
- 引擎冒烟：8 种引擎配置（含 E2 DAG 并行路径、E4 Reflection、3 个组合引擎）mock LLM
  端到端 `run()` 全部 `ALL_OK`，无接线崩溃。
- REPL：无凭据时优雅引导 `/setup`（不崩溃）。
- **未覆盖**：真实 LLM 调用（需用户 API Key）与 real 模式 eval。

### 已知后续项

- E5 `spawn_agent`（见上，审核建议延后）。
- Q1 续：`ContextManager` 用真实 usage 替代启发式估算（需把 completion_tokens 随
  assistant turn 回填，触及各引擎 `add_message`）。
- E2 范围内「迷你 ReAct」（无工具步 3 轮）暂缓（独立 M 项，语义待定）。
