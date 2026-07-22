# DeepSeek 收录准备与兼容性证据

> 状态：**收录候选，尚未获得 DeepSeek 官方认证或背书。**

本文用于维护向 DeepSeek 官方 GitHub 组织的
[`awesome-deepseek-agent`](https://github.com/deepseek-ai/awesome-deepseek-agent)
提交的 [Xenon 集成指南 PR #301](https://github.com/deepseek-ai/awesome-deepseek-agent/pull/301)。
进入该清单表示“被官方仓库收录为社区集成”，不等同于“DeepSeek 官方指定工具”。

## 当前 API 兼容基线

核对日期：2026-07-22。

| 官方要求 | Xenon 实现 | 验证位置 |
|----------|------------|----------|
| 正式模型为 `deepseek-v4-pro` / `deepseek-v4-flash` | 在线读取 `/models`；离线只回退到两个正式模型 | `xenon/repl/provider_registry.py` |
| 1M 上下文 | 注册 V4 模型时默认设置 1,000,000 | `xenon/repl/model_registry.py` |
| Max Thinking | V4 Pro 默认 `reasoning_effort=max`，支持按模型配置并透传普通、流式和原生工具请求 | `xenon/repl/model_registry.py`、`xenon/utils/llm_client.py` |
| 思考模式工具续轮 | 保留 `reasoning_content`、assistant `tool_calls` 和匹配 `tool_call_id` 的结果 | `xenon/utils/llm_client.py`、`xenon/engine/base.py` |
| 强制工具选择 | DeepSeek V4 使用 `required` / `none` / 指定函数时，仅对该请求关闭思考模式 | `xenon/utils/llm_client.py` |
| 上下文缓存 usage | 读取命中/未命中 token，按模型显示命中率和费用 | `xenon/utils/deepseek_cache.py` |
| 当前人民币价格 | Flash: 0.02 / 1 / 2；Pro: 0.025 / 3 / 6（hit / miss / output，元/百万 token） | `xenon/utils/deepseek_cache.py` |
| 工具调用 | DeepSeek V4 为 ReAct 主模型时自动启用原生 function calling，失败时分层降级到 JSON schema / 文本协议 | `xenon/engine/react_engine.py`、`xenon/engine/base.py` |

官方依据：

- [DeepSeek 模型与价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)
- [DeepSeek 思考模式与工具续轮](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode)
- [Awesome DeepSeek Agent](https://github.com/deepseek-ai/awesome-deepseek-agent)
- [贡献规范](https://github.com/deepseek-ai/awesome-deepseek-agent/blob/main/CONTRIBUTING.md)

## 本地证据命令

```bash
python -m pip install -e ".[dev]"
ruff check xenon
pytest tests xenon/tests -m "not live and not e2e" -q
pytest tests/e2e -m e2e -q
xenon --version
```

CI 还会在 Python 3.10、3.11、3.12 上重复离线测试，执行覆盖率门槛和发行包校验。
需要真实 DeepSeek Key 或公网的测试均标记为 `live`，不会让外部网络波动污染离线 CI。

## 当前 PR 文案

仓库要求在英文和中文 README 的工具表中各增加一项，并同时提交双语指南。
描述应保持简短、可验证：

```markdown
| **Xenon** | Terminal AI coding agent with DeepSeek V4 reasoning-effort support, native tool calling, cache-cost observability, permission-gated coding tools, and MCP integration. | [Guide](./docs/xenon.md) |
```

PR 标题：

```text
docs: add Xenon — terminal coding agent with DeepSeek V4 support
```

PR 正文应聚焦已经验证的能力：

```text
Xenon is an open-source terminal AI coding agent with direct DeepSeek API
support. It discovers current models from /models, defaults V4 Pro to
reasoning_effort=max, preserves reasoning_content across native tool-call
rounds, and reports context-cache usage and estimated CNY cost. It also includes
permission-gated coding tools, session recovery, and MCP integration.

Repository: https://github.com/xianyu-sheng/Xenon
License: MIT
Platforms: Linux, macOS, Windows
Python: 3.10+
```

## 提交前检查清单

- [x] `main` 分支 CI 全绿，并保留可点击的 CI / coverage badge
- [x] 发布与源码一致的 `v0.7.0` tag 和 GitHub Release
- [x] README 中不使用“官方”“指定”“认证”等未经授权的表述
- [x] README 已改用当前双线输入区、固定底栏、无边框回复的文本示意，不再嵌入旧版 `demo.gif`
- [x] 本机可编辑安装已完成 DeepSeek V4 对话、强制/自动工具调用和 ReAct `read_file` 闭环验证
- [x] `reasoning_effort=max` 已完成普通/流式/原生工具请求单测，并通过真实 V4 Pro 请求
- [ ] 在全新、不引用本地源码的虚拟环境中完成发行包安装验收
- [ ] 提交表格条目时保持英文描述简短、可验证，不使用竞品贬损性比较

## 距离“成熟终端编程工具”的剩余差距

本轮在不改写 Xenon 顶层架构的前提下，已经修复或补齐权限、原子写入、模型恢复、跨轮轨迹、GitHub URL、Plan 失败传播、离线 CI 和 DeepSeek V4 工具协议；用户可见的结构性变化集中在 TUI 布局。这些问题尚未阻塞官方清单收录，但以下能力仍决定长期成熟度：

1. **真实任务成功率**：仓库现有公开评测仍为 20 个任务、45% 成功率；应扩大固定任务集，并把失败分类和版本趋势放进 CI 或定期报告。
2. **系统级隔离**：当前以路径、参数和权限闸门为主，不等于容器/namespace 级 shell 沙箱；高风险无人值守场景仍需更强隔离。
3. **发行工程**：需要稳定的 PyPI 发布、校验和回滚流程；单文件静态二进制、签名和 SBOM 尚未提供。
4. **编辑器协议**：Reasonix 已公开 ACP 接口规范，Xenon 当前仍以 TUI 为主；若要进入 IDE/桌面宿主，需要 ACP 或等价协议层。
5. **跨平台真实回归**：CI 使用 Linux；Windows/macOS 的终端键位、剪贴板、PTY 和全局热键仍需要独立 runner 验证。

完成官方清单 PR 后，若目标进一步升级为合作或官方推荐，应通过 DeepSeek API 文档列出的
`api-service@deepseek.com` 联系官方，并提供版本化评测、活跃用户数据、安全模型和维护承诺。
