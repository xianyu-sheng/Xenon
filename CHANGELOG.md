# 更新日志

本文件记录 Xenon 各版本变更。版本号遵循语义化版本（预 1.0 阶段：
`0.MINOR.PATCH`）。

## [Unreleased]

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

- 离线回归：`1523 passed, 36 deselected`。
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
