# 执行层完整度清单

> **用途**：每次真实任务完成后，用本表交代“本次做了什么 / 距离完整执行层还差什么”。
> 这是能力清单，不是发布清单；**不发版不代表可以跳过未完成项**。
>
> **状态**：✅ 已验证 · 🟡 部分完成 · ⏳ 未做 · 🧊 需单独决策 · ❌ 明确不做/阻塞

## 1. 当前能力对照（Claude Code / Codex / pi）

| 能力 | 他们的做法 | Xenon 当前 | 剩余差距 |
|---|---|---|---|
| 循环自主性 | Codex “persist until resolved”，不轻易停 | 已撤销 3 次硬停；C13 写任务失败只提示 | 长任务仍需更多真实样本 |
| 持久计划/todo | Codex `update_plan`；Claude TodoWrite | C11 `update_plan`：持久在 AgentContext、原生数组 schema、跨文件/长任务提示、收尾一致性提醒 | 计划 UI 展示、跨会话恢复、单文件长任务触发策略仍需验证 |
| 失败恢复 | 换方法/继续，不因几次失败收工 | C13 最多两次“换方法”提示；查询任务保留收敛 | C4 shell 误伤仍造成无效重试 |
| 原生工具协议 | 全链路 native tool calling | ReAct 对 DeepSeek 开 native；planner + `update_plan` 原生 schema | 文本 JSON 降级路径仍在；截断续写仍有边界 |
| Shell/沙箱 | Codex OS 级沙箱，允许更自然命令 | 审批 + 安全策略；`$()` / `python -c` 常被拦 | **C4**：减少误伤/提供安全替代；仍不放松安全边界 |
| 模型能力 | Claude/GPT agentic coding 训练 | DeepSeek flash/pro | 模型本身是最大上限；C5 数据：flash 默认，不全局切 pro |
| 上下文管理 | 自动压缩、子 agent、长会话 | 有 in-run 压缩、spawn_agent | 未系统对标长上下文/子 agent 效果 |
| 权限/审批 | Codex sandbox + approve 模式 | 权限门 + REPL 审批 | 自主性与安全边界未做长任务压测 |
| 验证闭环 | 跑测试/格式化，失败再修 | 验证提醒（非阻断）+ 事实绑定 | 提醒有误报历史；C15 已修临时脚本误报 |
| 交付闭环 | 实现、测试、提交/PR | 提示词要求真实交付；长任务可 commit | 没有确定性交付契约；PR 流程依赖模型自觉 |
| 多 agent/并行 | Codex 多 agent；pi 可选扩展 | DAG 并行 + spawn_agent | 未验证真实任务收益 |
| 证据/审计 | 相对弱 | **L0/L1/L2 强项** | 保持正交，不拿执行层改动换证据强度 |

## 2. 每次任务后的固定清单

| # | 项目 | 状态 | 说明 / 下一步 |
|---|---|---|---|
| 1 | 循环不早停 | ✅ | 默认不限步数；高置信度 LoopDetector + 无进展检查点 + 1800s 墙钟；低置信度只提示 |
| 2 | 连续失败恢复 | ✅ | 写任务最多两次换方法提示；查询任务收敛 |
| 3 | 持久计划 `update_plan` | ✅ | TaskState 保存版本/历史/补救步骤；计划执行中由 `plan_state` EventBus 更新，结束后打印完整面板；真实 #26 7/7 步骤完成 |
| 4 | Shell/沙箱摩擦 | 🟡 | C4：`run_script` + 自动识别 + `lang` 别名；新增结构化 `run_tests`（pytest/unittest/custom argv、项目 venv 发现、无 shell、真实 exit code）；仍有未加引号 `$变量` 被拦截，复杂 PowerShell 条件仍未归一化（#176） |
| 5 | 真实长任务完成率 | 🟡 可测，不可外推 | `evals/execution_benchmark.py` 已支持 `--repeat` + 聚合；最新 2026-10-24 两次 9-run 样本为 7/9（旧 claim 收尾 bug）与 4/9（其中 5 次 API 402，排除基础设施后 4/4）；false_completion=0；需恢复额度后重跑再发布稳定率 |
| 6 | 模型路由/质量 | 🟡 | C5：flash 默认；pro 未证明完成率优势 |
| 7 | 上下文/压缩/子 agent | 🟡 | 缓存/视图/回查/哈希锚定；native、`/compact`、自动归档、非 native in-run 均 0-LLM；#27 复验 0 截断；真实 save/resume 待验 |
| 8 | 权限/沙箱自主性 | ⏳ | 审批可用，但非 Codex OS 沙箱 |
| 9 | 原生协议/截断续写 | 🟡 | DSML 变体已修；仍有 finish_reason=length 边界 |
| 10 | 验证闭环 | ✅ | TaskState + 统一出口；失败输出进入同一步骤修复；真实 #26 目标测试 2 passed + commit |
| 11 | 交付/commit/PR | 🟡 | TaskState 记录 verify/commit/PR；#167 安全 finalize 只提交本轮已验证、全部暂存的 artifact，默认 `XENON_AUTO_FINALIZE=1` 可关闭；真实 PR 闭环待验 |
| 12 | TTY / 审批 UX | 🟡 | C7：TTY 单行待手测 |
| 13 | `git add -A` 纪律 | ⏳ | C8：提示显式路径 |
| 14 | 证据层新文件“盲写” | 🧊 | C3：单独决策，不混入执行层 |
| 15 | 发布 | ❌ 暂不做 | 能力未达标前不发版，不堆版本号 |

## 3. 使用规则

- 每次任务结束，在回复末尾附上 **第 2 节清单**（可压缩为“已完成 / 未完成”两栏）。
- 新发现的问题追加到本表或 `docs/VERIFICATION_CHECKLIST.md` 的 C 区，不删除旧项。
- 只有真实模型、真实仓库、真实测试验证后才改 ✅。
- 执行层改动不得放宽 L0/L1/L2 证据门。
