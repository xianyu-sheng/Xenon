# 更新日志

本文件记录 OmniAgent-CLI 各版本变更。版本号遵循语义化版本（预 1.0 阶段：
`0.MINOR.PATCH`，每个修复批次递增 MINOR）。

## [0.2.1] — 2026-07-08

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
- CLI：`omniagent --help` / `omniagent run <workflow.yaml> --dry-run`（配置解析 →
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
