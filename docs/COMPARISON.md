# Xenon vs Aider / Claude Code / OpenCode / Crush

> 这是一份**实事求是**的能力对位表。✅ = 有，⚠️ = 部分 / 有限，❌ = 没有，— = 不适用。
> 评测时间 2026-07-08；后续版本可能变化。觉得有出入的，欢迎 issue。

---

## 一句话总结

| 项目 | 一句话 |
| --- | --- |
| **Xenon** | **MCP + 多模型 + 多范式三合一**，开源 terminal coding agent |
| [Aider](https://aider.chat) | 多模型 + git commit 循环，专注 repo 级编辑 |
| [Claude Code](https://claude.com/claude-code) | Claude + MCP 官方 terminal，闭源 |
| [OpenCode](https://opencode.ai) | 多模型 + 50+ provider，TUI，Go 写的 |
| [Crush](https://github.com/charmbracelet/crush) | 多模型 + MCP + LSP 增强，Go 写的 |

---

## 能力矩阵

| 维度 | Xenon | Aider | Claude Code | OpenCode | Crush |
| --- | :---: | :---: | :---: | :---: | :---: |
| **MCP 协议** | ✅ stdio + SSE | ❌ | ✅ 一等公民 | ✅ | ✅ stdio/http/sse |
| **多模型路由** | ✅ 6 provider | ✅ ~10 | ❌ Claude only | ✅ 50+ | ✅ 18+ |
| **多范式引擎** | ✅ **8 种** | ❌ | ❌ | ❌ | ⚠️ 可 mid-session 换 LLM |
| **本地优先** | ✅ Ollama + 本地凭据 | ⚠️ CLI 本地，模型云 | ❌ | ✅ | ✅ Ollama / llama.cpp |
| **工具断路器** | ✅ 3 失败熔断 | ❌ | ❌ | ❌ | ❌ |
| **上下文压缩** | ✅ 6 步 @ 80% | ❌ | ✅ | ❌ | ❌ |
| **空洞回答检测** | ✅ 15 正则 | ❌ | ❌ | ❌ | ❌ |
| **三阶段预算** | ✅ Explore/Execute/Converge | ❌ | ❌ | ❌ | ❌ |
| **开源协议** | ✅ MIT | ✅ Apache 2.0 | ❌ 闭源 | ✅ MIT | ✅ FSL-1.1-MIT |
| **实现语言** | Python 3.10+ | Python | TypeScript | Go | Go |

---

## 各维度详解

### 1. MCP 协议

| 项目 | 能力 |
| --- | --- |
| **Xenon** | stdio + SSE 双传输；子进程用 `select` + 墙钟超时（v0.2.0 B11 修复），不会被 `readline` 无限阻塞；进程退出用 `terminate()` + 兜底 `kill()`，**无僵尸进程**。 |
| Aider | 不支持 MCP。用自定义 plugin 机制扩展工具。 |
| Claude Code | MCP 一等公民，原生支持；Anthropic 自家协议。 |
| OpenCode | 支持 MCP。 |
| Crush | 支持 MCP（stdio/http/sse 三种类型）；不支持自动重启 MCP server。 |

**Xenon 优势**：和 Crush 一样全传输类型支持，但加上 B11 修复的墙钟超时（v0.2.0 真实 commit `xxx` 修复），子进程挂死不会拖死整个 agent。
**Xenon 劣势**：和 Claude Code 相比，MCP server **不自动重启**——子进程挂掉需要 `/mcp add` 重新添加。这是已知后续项（不是技术不能，是优先级让位给 #19 等 v0.3.0 重写）。

### 2. 多模型路由

| 项目 | 能力 |
| --- | --- |
| **Xenon** | 6 provider 一处配置：`openai` / `anthropic` / `deepseek` / `gemini` / `qwen` / `ollama`（含本地模型）；`provider_priority` 角色级优先级；per-provider `httpx.Client` 长连接池复用（v0.2.0 R3 修复）。 |
| Aider | ~10 provider，OpenAI 兼容协议都可接入。 |
| Claude Code | 仅 Claude 系列模型（opus / sonnet / haiku）。 |
| OpenCode | 50+ provider，Catwalk 自动发现。 |
| Crush | 18+ 内置 provider，支持 OpenAI / Anthropic 兼容协议自接入。 |

**Xenon 优势**：和 Aider / Crush 同级（多 provider 接入），但有**断路器自动降级**——3 连续失败自动冷却，比 Crush 多了这个生产级特性。
**Xenon 劣势**：provider 数量不如 OpenCode（50+）和 Crush（18+），但 6 个覆盖了主流 + 本地。

### 3. 多范式引擎（**核心差异化**）

| 项目 | 能力 |
| --- | --- |
| **Xenon** | **8 种推理范式**：`direct` / `react` / `plan-execute` / `reflection` / `novel`（创意写作）+ 3 个组合引擎 `plan-react` / `plan-reflection` / `react-reflection`；同一套 REPL 内 `/mode` 切换。 |
| Aider | 单范式：chat + 工具调用循环。 |
| Claude Code | 单范式：单 agent loop。 |
| OpenCode | 单范式。 |
| Crush | "Flexible: switch LLMs mid-session preserving context" — 这是**换模型**（同范式内），不是换范式。 |

**Xenon 优势**：**这是全表最强的差异化点**。8 种范式意味着：
- 简单问答 → `direct` 省 token
- 工具调用 → `react` 经典循环
- 多步任务 → `plan-execute` 自动分解（PlanDAG 拓扑并行）
- 质量敏感 → `reflection` 独立审查者模型
- 创意写作 → `novel` 长文续写引擎（v0.2.0 Q5 添加）
- 复合场景 → 3 个组合引擎（隔离 ctx + reviewer 模型实现多范式协同）

其他 4 个项目都**没有真正的多范式切换**——Aider / Claude Code / OpenCode 是单 agent loop，Crush 是 mid-session 换 LLM（**换模型不换范式**）。

### 4. 本地优先

| 项目 | 能力 |
| --- | --- |
| **Xenon** | Ollama 本地模型；凭据存 `~/.xenon/credentials.yaml`，不联网；`~/.xenon/` 完整本地状态（compact / memory / sessions）。 |
| Aider | CLI 本地跑，模型走云端。 |
| Claude Code | 闭源 + 云端 only。 |
| OpenCode | 开源，Ollama 支持。 |
| Crush | 开源，Ollama / llama.cpp / LM Studio / LiteLLM / omlx 全支持。 |

**Xenon 优势**：和 Crush 同级（Ollama + 凭据本地 + 状态本地），但多模型 provider 数量是 Crush 的 1/3。
**Xenon 劣势**：本地运行时支持不如 Crush 细（Crush 支持 5 种本地运行时，Xenon 只支持 Ollama）。

### 5. 工具断路器

| 项目 | 能力 |
| --- | --- |
| **Xenon** | `CircuitBreaker` 每工具独立断路器，`failure_threshold=3`（默认 3 连续失败）→ `OPEN` 状态熔断，`cooldown=30s` 冷却（half_open 失败翻倍，max 600s）；进程级 `GLOBAL_BREAKERS` 跨 run 累积。 |
| 其它 4 项 | ❌ 都不做。 |

**Xenon 优势**：**这是全表独有的生产级特性**。其他 4 个项目在 tool 失败时只重试或不重试，**没有熔断 + 指数退避**机制。Xenon 在工具反复失败时会进入 OPEN 状态直接拒绝请求，保护 LLM token 不被空转消耗。

### 6. 上下文压缩

| 项目 | 能力 |
| --- | --- |
| **Xenon** | `Compactor` 6 步结构化压缩器，在 Token 窗口达 80% 触发；引擎内每 5 轮自动压缩抑制 O(n²) 增长（v0.2.0 F4 修复）；持久化到 `~/.xenon/compact/`。 |
| Claude Code | ✅（实现细节未公开）。 |
| 其它 3 项 | ❌。 |

### 7. 空洞回答检测

| 项目 | 能力 |
| --- | --- |
| **Xenon** | `HollowDetector` 三类信号：①`len<5` 快速失败；②`tool>=5 && len<100` 不成比例；③15 个反模式正则 + 组合判定（命中正则 AND（长度不足 OR 结构差））。 |
| 其它 4 项 | ❌。 |

**Xenon 优势**：**全表独有**。这是 Xenon 的"软提示"机制——给 LLM 补救机会而非直接判失败，配合 `BudgetManager.on_hollow_answer()` 触发额外轮次。

### 8. 三阶段预算

| 项目 | 能力 |
| --- | --- |
| **Xenon** | `BudgetManager` 三阶段软预算：EXPLORE（25%，鼓励探索）→ EXECUTE（50%）→ CONVERGE（25%，禁用 7 个纯探索型工具强制收束）；奖励机制（压缩 +N / 空洞 +N，`max_total_multiplier=2×` 封顶）。 |
| 其它 4 项 | ❌。 |

**Xenon 优势**：**全表独有**。这是 Xenon 区别于"调 LLM API 的 CLI 工具"的核心——不只是问 LLM 答啥，而是**有节律地引导 LLM 走向最终答案**。

---

## 怎么选

| 你的场景 | 推荐 |
| --- | --- |
| 我要快速 git commit 风格的代码编辑 | Aider（成熟、git 集成深） |
| 我只用 Claude 模型，要 MCP | Claude Code（官方、一等 MCP） |
| 我要 50+ provider，TUI 美观 | OpenCode |
| 我要 Go 写的快、轻、本地多运行时 | Crush |
| **我要多范式（8 种）+ 6 provider + MCP + 工程化三件套** | **Xenon** |
| 我要研究 Agent 范式（PlanDAG / Reflection / 组合引擎实现） | **Xenon**（这是实验平台） |
| 我要做国产 / 中文 / 本地化 | **Xenon**（DeepSeek / Qwen / Ollama 三件套） |

---

## 已知后续项

- MCP server **不自动重启**（Xenon 唯一不补的 MCP 特性，#19 v0.3.0 路线）
- provider 数量（6）落后 OpenCode（50+）和 Crush（18+），但 6 个覆盖主流
- 本地运行时仅支持 Ollama，Crush 支持 5 种

提交 issue 之前可以先看 [CHANGELOG.md](../CHANGELOG.md) 确认版本。
