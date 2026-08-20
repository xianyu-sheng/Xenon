# 更新日志

本文件记录 Xenon 各版本变更。版本号遵循语义化版本（预 1.0 阶段：
`0.MINOR.PATCH`）。

## [Unreleased]

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
- **文件声称提取修复**：代码引用（`self.c`）、diff 头（`a/` `b/`）、
  并列声称（"已保存 A 和 B"）三类误判根因修复 + 14 回归测试

### SWE-bench 官方评测（SWE-bench_Lite，30 实例可复现）

- 基线（混合模型）：**11/30 = 36.7% instance-level**（45.8% cell-level），
  完整方法论 + 预测文件入库 `evals/results/`
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
