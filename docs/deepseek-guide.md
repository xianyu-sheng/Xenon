# DeepSeek 缓存最佳实践指南

> 让每一次 API 调用都尽可能命中缓存，享受 **¥0.025/百万 token** 的极致性价比。

---

## 目录

1. [DeepSeek 缓存是什么？](#1-deepseek-缓存是什么)
2. [缓存命中条件](#2-缓存命中条件)
3. [提示词对齐策略](#3-提示词对齐策略)
4. [Xenon 三层监控体系](#4-xenon-三层监控体系)
5. [CacheTracker 编程接口](#5-cachetracker-编程接口)
6. [费用对比：省了多少？](#6-费用对比省了多少)
7. [命中率骤降诊断](#7-命中率骤降诊断)
8. [快速检查清单](#8-快速检查清单)

---

## 1. DeepSeek 缓存是什么？

DeepSeek API 实现了**自动上下文缓存**（Context Caching）。当你重复发送相同的 prompt 前缀时，DeepSeek 服务端会自动复用之前的计算结果，只对新增内容进行计算。

### 定价差异

| 类型 | 单价（¥ / 百万 token） | 相比 miss 节省 |
|------|------------------------|---------------|
| **缓存命中** (cache hit) | ¥0.025 | **99.2%** |
| 缓存未命中 (cache miss) | ¥3.0 | — |
| 输出 (output) | ¥6.0 | — |

> 一次 1000 token 的输入，命中缓存仅需 ¥0.000025，而未命中需 ¥0.003——**相差 120 倍**。

### 对开发者的影响

如果你在开发 AI 应用（Agent、Chatbot、代码助手），system prompt 和工具定义通常占据 2000-5000 token，且**每次调用都完全相同**。只要 prompt 结构对齐，这些固定部分就能持续命中缓存，带来巨大的成本节省。

---

## 2. 缓存命中条件

DeepSeek 的缓存判定基于 **prompt 前缀匹配**：

```
请求 1: [system_prompt] [tool_defs] [user_msg_A]
请求 2: [system_prompt] [tool_defs] [user_msg_B]
                                    ↑ 从这里开始不同
                                    ↑ system_prompt + tool_defs 命中缓存
```

### 会破坏缓存的情况

```
请求 1: [system_prompt] [current_time: 2025-07-20 09:00] [user_msg]
请求 2: [system_prompt] [current_time: 2025-07-20 09:01] [user_msg]
                         ↑ 时间变了！整个前缀失效
```

**任何前缀中的变化都会导致该位置之后的所有 token 缓存失效。**

---

## 3. 提示词对齐策略

### 核心原则：固定内容前置，动态内容后置

```
✅ 正确结构：
┌─────────────────────────┐
│ system_prompt（固定）     │  ← 每次相同，持续命中
│ tool_definitions（固定）  │  ← 每次相同，持续命中
│ safety_rules（固定）      │  ← 每次相同，持续命中
├─────────────────────────┤
│ user_message（可能变化）  │  ← 从这里开始不同
│ context: {current_time}  │  ← 动态内容放最后
│ context: {working_dir}   │
└─────────────────────────┘

❌ 错误结构：
┌─────────────────────────┐
│ context: {current_time}  │  ← 时间变了！
│ system_prompt（固定）     │  ← 虽然固定，但前面的时间已破坏缓存
│ user_message             │
└─────────────────────────┘
```

### Xenon 自动优化

Xenon 的 `PromptOptimizer` 会自动检测并重组消息顺序：

- `optimize_messages_for_cache()` — 把 tool schema、system prompt 核心固定部分前置，时间戳、路径、用户变量等动态内容后置
- `_is_dynamic_content()` — 正则检测日期/时间/路径/模板变量

**你不需要手动调整任何东西。** 优化器在每次对话时自动运行，`/cost` 命令和底部 toolbar 会实时反馈效果。

---

## 4. Xenon 三层监控体系

Xenon 提供了三层缓存可见性，全部基于本地确定性计算，**不额外消耗 LLM API**：

### L1：底部 Toolbar（实时）

```
💾96% 💰<0.01 💡92%    auto deepseek-v4-pro · direct    ░░░░░░░░ 2.6% · 5m · 00:03 · ⚡
```

| 指标 | 说明 |
|------|------|
| 💾96% | 当前会话缓存命中率 |
| 💰0.02 | 累计预估费用（元） |
| 💡92% | 相比全未命中节省百分比 |

每次 API 调用完成后自动刷新，零延迟。

### L2：`/cost` 命令（完整面板）

```
╭── /cost ─────────────────────────────────────╮
│ 模型: deepseek-v4-pro                         │
│   调用次数: 2                                  │
│   Input: 6,747 tokens  Output: 718 tokens     │
│   缓存命中: 3,712 (55.0%)                      │
│   缓存未命中: 3,035 (45.0%)                    │
│   预估费用: ¥0.0135                            │
│   节省: ¥0.0110 (44%) vs 全未命中              │
╰───────────────────────────────────────────────╯
```

在对话中随时输入 `/cost` 查看详细的按模型 breakdown。

### L3：退出省钱报告（自动）

```
╭── 📊 本次会话省钱报告 ─────────────────────────╮
│ deepseek-v4-pro  2 次 · 7,465t · 💾55% · ¥0.0135 │
│                                                  │
│ 合计  7,465 tokens · 💾55% · ¥0.0135 · 💡省 ¥0.0110 (44%) │
╰──────────────────────────────────────────────────╯
再见！
```

`/exit` 或 `Ctrl+C` 两次退出时自动打印整次会话的总账单。

---

## 5. CacheTracker 编程接口

如果你在代码中使用 Xenon 的 LLM 客户端，可以通过 `CacheTracker` 编程式获取缓存数据：

```python
from xenon.utils.deepseek_cache import CacheTracker
from xenon.utils.llm_client import register_response_callback, chat_completion

# 创建 tracker（自动注册为全局回调）
tracker = CacheTracker()

# 正常调用 LLM——tracker 自动记录
response = chat_completion(
    "deepseek/deepseek-v4-pro",
    messages=[{"role": "user", "content": "你好"}],
    max_tokens=100,
)

# 实时查询
print(tracker.cache_hit_rate)        # 0.96 → 96%
print(tracker.estimated_cost_yuan)   # 0.0135
print(tracker.savings_pct)           # 44

# 按模型查看
for model_id in tracker.all_models:
    snap = tracker.model_snapshot(model_id)
    print(f"{model_id}: {snap['cache_hit_rate']:.1%}")

# 清理
tracker.close()
```

### 命中率骤降检测

```python
alert = tracker.check_hit_rate_drop()
if alert:
    print(f"⚠️ 命中率骤降: {alert['recent_rate']:.0%} "
          f"(vs 历史 {alert['older_rate']:.0%}，下降 {alert['drop_pct']:.0%})")
    # 可能原因：system prompt 变更、提示词结构变化
    print(f"建议: {alert['suggestion']}")
```

- 滚动窗口对比（最近 N 次 vs 前 N 次）
- 阈值：下降超过 40% 触发告警
- 自动生成修复建议（如检查 system prompt hash 是否变化）

---

## 6. 费用对比：省了多少？

假设一个典型开发场景：每次对话的 system prompt + 工具定义约 **3000 token**，用户消息约 **200 token**，模型输出约 **500 token**，每天 **50 次调用**。

### 未优化（命中率 0%）

| 项目 | 计算 | 费用/天 |
|------|------|---------|
| Input miss | 3000 × 50 = 150K token × ¥3.0/1M | ¥0.45 |
| Output | 500 × 50 = 25K token × ¥6.0/1M | ¥0.15 |
| **合计/天** | | **¥0.60** |

### 优化后（命中率 94%）

| 项目 | 计算 | 费用/天 |
|------|------|---------|
| Input hit | 3000 × 50 × 0.94 = 141K token × ¥0.025/1M | ¥0.0035 |
| Input miss | 3000 × 50 × 0.06 + 200 × 50 = 19K token × ¥3.0/1M | ¥0.057 |
| Output | 25K token × ¥6.0/1M | ¥0.15 |
| **合计/天** | | **¥0.21** |

> 每天节省 ¥0.39，**月度节省 ¥11.7，年度节省 ¥142**。如果你有 10 个用户、100 个用户，节省量线性放大。

### 更大规模场景

| 日调用量 | 未优化费用/月 | 优化后费用/月 | 月节省 |
|----------|-------------|-------------|--------|
| 50 | ¥18 | ¥6.3 | ¥11.7 |
| 500 | ¥180 | ¥63 | ¥117 |
| 5,000 | ¥1,800 | ¥630 | **¥1,170** |
| 50,000 | ¥18,000 | ¥6,300 | **¥11,700** |

---

## 7. 命中率骤降诊断

如果你发现缓存命中率突然下降，按以下步骤排查：

### Step 1：检查 system prompt 哈希

```python
# CacheTracker 内部用 SHA256 追踪 system prompt
print(f"当前 hash: {tracker.system_hash}")
```

如果 hash 与之前记录的不同，说明 system prompt 内容变了——这是命中率下降的最常见原因。

`check_hit_rate_drop()` 方法会自动检测最近 5 分钟内是否发生过 system prompt 变更，并在告警建议中指出。

### Step 2：检查动态内容是否污染前缀

```python
from xenon.repl.prompt_optimizer import _is_dynamic_content

# 检查 system prompt 中是否有时间戳、路径等动态内容
for line in system_prompt.split("\n"):
    if _is_dynamic_content(line):
        print(f"⚠️ 动态内容污染前缀: {line}")
```

### Step 3：检查对话历史是否过长

DeepSeek 的缓存基于前缀匹配。如果对话历史积累到 50 轮以上，即使 system prompt 相同，前面部分的历史差异也可能影响缓存判定。

**建议：** 定期 `/compact` 压缩对话上下文，保留 system prompt + 工具定义 + 最近 N 轮对话。

### Step 4：确认 DeepSeek API 版本

缓存功能需要 DeepSeek API 支持。确认使用的是 `/v1/chat/completions` 端点（Xenon 默认使用）。

---

## 8. 快速检查清单

在 Xenon 中，你只需要记住：

- [ ] **看 toolbar** — `💾` 后面的数字就是命中率。正常应该 >80%
- [ ] **低于 40%？** — 输入 `/cost` 看详细 breakdown
- [ ] **提示词优化了吗？** — 如果看到 `💡 提示词已优化` 消息，系统已自动重组你的 prompt
- [ ] **退出时看一眼** — `/exit` 会打印本次会话总共省了多少钱
- [ ] **对话太长？** — `/compact` 压缩上下文，恢复缓存命中率

---

## 延伸阅读

- [DeepSeek API 官方文档](https://platform.deepseek.com/api-docs/)
- [Xenon CacheTracker 源码](../xenon/utils/deepseek_cache.py)
- [Xenon PromptOptimizer 源码](../xenon/repl/prompt_optimizer.py)
- [Xenon `/cost` 命令实现](../xenon/repl/commands.py)
- [Xenon 视觉桥接器源码](../xenon/tools/vision_bridge.py) — 让 DeepSeek 通过模型池"看见"图片

---

*最后更新：2025-07-20 · Xenon v0.6.1*
