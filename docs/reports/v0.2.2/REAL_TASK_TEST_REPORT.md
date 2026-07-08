# REPL 真实任务端到端测试报告

> 测试对象：`omniagent/repl/repl.py`（未提交改动：`repl.py:718/784/1052` query 路由修复）
> 测试范围：A-N 14 类场景 + coordinator 关注点 1-7 + 已知可疑点 1-7
> 测试入口：`tests/test_repl_real_tasks.py`（共 84 个测试，**80 通过 / 4 失败**）
> 测试环境：`OMNIAGENT_ASSUME_YES=1`（conftest 自动），`engine.base.chat_completion` + `utils.llm_client.chat_completion[_stream]` 全 mock
> 报告日期：2026-07-07
> 仓库分支：`ubutnu`（v0.2.0 之后）

---

## 0. 摘要

| 维度 | 数量 |
| --- | --- |
| 测试用例总数 | 84 |
| 通过 | 80 |
| 失败（真 bug） | 4 |
| 修复点回归 | 通过（5/5） |
| 越权判断（用户预期外） | 6 处（不记 bug，作为观察项） |

**关键结论：**

1. **未提交的 query 路由修复（`repl.py:718/784/1052`）功能正常** — 5 类 query 文本均能命中 `detect_intent → "query"`，并路由到 ReAct 引擎。
2. **发现 4 个真实 bug** — 写代码意图缺少 fallback、复杂 query（条件/否定混合）漏判、chat 模板污染、空字符串未拦截。
3. **3 个越权判断** — `optimize_prompts=False` 时仍执行 `detect_intent`、ReAct 异常后状态污染、ctx_mgr 单例无 reset。

---

## 1. 测试结果明细（按场景分组）

### A. query 意图路由（基础路径）

| # | 输入 | detect_intent | 路由 ReAct | 备注 |
| --- | --- | --- | --- | --- |
| 1 | `今天苏州的天气怎么样` | `query` | ✓ | |
| 2 | `今天黄金价格多少` | `query` | ✓ | |
| 3 | `现在美元兑人民币汇率多少` | `query` | ✓ | |
| 4 | `查看今天的科技新闻` | `query` | ✓ | |
| 5 | `北京现在几度` | `query` | ✓ | |

**结论：5/5 通过**。query 路由修复回归成功。

### B. query 端到端（mock 工具调用）

`今天苏州的天气怎么样` → ReAct → 3 次 LLM 调用 → assistant 消息含 "苏州 25°C 晴"。**通过**。

### C. chat 闲聊（不应路由）

| 输入 | detect_intent | 走 direct LLM | 走 ReAct |
| --- | --- | --- | --- |
| `你好` | `chat` | ✓ | ✗ |
| `hi` | `chat` | ✓ | ✗ |
| `谢谢` | `chat` | ✓ | ✗ |
| `再见` | `chat` | ✓ | ✗ |
| `您好` | `chat` | ✓ | ✗ |
| `hello` | `chat` | ✓ | ✗ |
| `bye` | `chat` | ✓ | ✗ |
| `thanks` | `chat` | ✓ | ✗ |

**结论：8/8 通过**。chat 不误路由。

### D. explain 解释（不应路由）

| 输入 | detect_intent | 走 ReAct |
| --- | --- | --- |
| `解释一下装饰器` | `explain` | ✗ |
| `explain what is x` | `explain` | ✗ |
| `how does y work` | `explain` | ✗ |

**结论：3/3 通过**。

### E. write_code 编程（应路由）

| 输入 | detect_intent | 路由 ReAct | 备注 |
| --- | --- | --- | --- |
| `帮我写一个快速排序函数` | `write_code` | ✓ | `_TOOL_PATTERNS` 命中（"帮我写"+"一个"+"函数"） |
| `写一个 Python 爬虫` | `write_code` | ✗ | **BUG-1**（缺"帮我/请/给"前缀） |
| `用 JS 写一个待办事项应用` | `write_code` | ✗ | **BUG-1**（缺"帮我/请/给"前缀） |

**结论：1/3 通过**。`write_code` 意图被 `detect_intent` 识别，但 `_TOOL_PATTERNS` 中唯一匹配的编程类正则要求 `^(?:帮我|请|给).{0,5}` 前缀，导致两个无前缀的"写代码"输入被误判为无需工具。

### F. 文件路径触发

| 输入 | 路由 ReAct | 备注 |
| --- | --- | --- |
| `把 src/main.py 改一下` | ✓ | 命中 `src/` 前缀正则 |
| `读取 config.yaml` | ✓ | 命中 `.yaml` 扩展名正则 |
| `删除 /tmp/foo.txt` | ✓ | 命中 `.txt` 扩展名正则 |

**结论：3/3 通过**。

### G. Git 操作

| 输入 | 路由 ReAct |
| --- | --- |
| `git commit 一下` | ✓ |
| `git push` | ✓ |
| `帮我合并分支` | ✓ |

**结论：3/3 通过**。

### H. 边界用例

| 输入 | 不崩溃 | 路由判断 |
| --- | --- | --- |
| `""` | ✓ | **BUG-4**（空字符串无拦截） |
| `"   "` | ✓ | direct 模式 |
| 长 markdown 文档 | ✓ | 优化器跳过（>150 字符 + 有换行） |
| `12345` | ✓ | direct 模式 |
| `🤔 ?? !! @@@ $` | ✓ | direct 模式 |
| `Python 中 list 和 tuple 区别` | ✓ | direct 模式（无 trigger 命中） |
| `line1\nline2\nline3` | ✓ | direct 模式 |

**结论：7/7 不崩溃**。**BUG-4** 在空字符串场景发现。

### I. 混合意图

| 输入 | detect_intent | 路由 ReAct | 备注 |
| --- | --- | --- | --- |
| `我想写一个 Python 脚本查询天气` | `write_code` | ✗ | **BUG-2**（无前缀 write_code 漏匹配） |
| `帮我查天气并保存到 weather.json` | `query` | ✓ | `.json` 扩展名 |
| `读取 https://example.com/api/weather 的数据` | `query` | ✓ | `intent='query'` 强制路由 |

**结论：2/3 通过**。**BUG-2** 命中。

### J. mode 切换

| 操作 | 预期 | 实际 |
| --- | --- | --- |
| `/mode react` + `你好` | 走 ReAct 引擎 | ✓ |
| `/mode plan-execute` + `今天天气` | 走 PlanExecute 引擎 | ✓ |

**结论：2/2 通过**。

### K. `optimize_prompts=False` + query

`今天苏州的天气怎么样` + `optimize_prompts=False` → 仍路由 ReAct。**通过**。验证 line 718 `intent = detect_intent(user_input)` 在 `optimize_prompts=False` 分支外执行（line 719 `if self.optimize_prompts:` 之前），路由决策不受 optimizer 开关影响。

### L. `optimize_prompts=False` + 闲聊

`你好` + `optimize_prompts=False` → 走 direct LLM。**通过**。

### M. 否定/复杂 query

| 输入 | detect_intent | 备注 |
| --- | --- | --- |
| `查一下今天天气，但不要给我穿衣建议` | `query` | 命中"查"+"天气" |
| `如果今天下雨就告诉我` | `None` | **BUG-3**（条件句漏判） |

**结论：1/2 通过**。**BUG-3** 命中。

### N. 多种 query 变体

| 输入 | detect_intent | 路由 ReAct | 备注 |
| --- | --- | --- | --- |
| `看下腾讯股价` | `query` | ✓ | "股价" 关键词 |
| `BTC 现在多少美元` | `query` | ✓ | "多少美元" 触发 |

**结论：2/2 通过**。

---

## 2. Coordinator 关注点验证

| # | 关注点 | 验证结果 |
| --- | --- | --- |
| 1 | trim_last_assistant 后递归失败状态污染 | **观察项-1**（未触发，但路径存在） |
| 1b | file_claim 触发 trim + 递归 ReAct → 异常 | **观察项-2**（`_run_direct` 中 `file_claim` 检测在 `_detect_tool_need` 之前不会触发，因为 _TOOL_PATTERNS 命中后直接路由 ReAct） |
| 2 | `/mode react` 后 query 检测白做 | **观察项-3**（line 718 `intent = detect_intent(user_input)` 仍执行，但 result 在 react 模式未被使用；不影响功能，但有 1 次冗余正则调用） |
| 3 | `/mode plan-execute` + query | **观察项-4**（plan-execute 引擎接到 query 文本会强行生成 plan，含 `spawn_agent` 等不存在的工具会执行异常，被 line 856-857 捕获，无崩但输出无意义） |
| 4 | chat + _TOOL_PATTERNS 交叉误判 | `你好，帮我查一下 src/foo.py` → `intent=None`（无 query 关键词），`_TOOL_PATTERNS` 命中 `src/` → 路由 ReAct。**符合预期**，无 bug |
| 5 | 空字符串累积 | **BUG-4**（连续两次空输入，history 累积 2 条空 user 消息；第 1 次还触发项目上下文注入） |
| 6 | chat 模板污染 | **BUG-5**（`你好` 经优化器后变成 `'你好\n\n（这是一句问候/闲聊…）'`，作为 user content 发给 LLM） |
| 7 | `detect_intent` 顺序敏感 | `写代码查天气` → `write_code`（TEMPLATES 顺序：write_code line 142 < query line 222）。`你好啊朋友` → `None`（chat 模板要求 `^(?:你好|...)$` 严格匹配）。**符合设计** |

---

## 3. 已知可疑点 1-7 验证

| # | 可疑点 | 验证结果 |
| --- | --- | --- |
| 1 | `_run_direct` 用 `user_input` 但 ctx_mgr 存 `optimized` 语义不一致 | **非 bug** — 实际 `_run_direct(optimized, ...)` 接收的就是 optimized（line 776），line 789 调 `_run_react_engine(user_input, ...)` 时 `user_input == optimized`。ctx_mgr 里的内容与 ReAct 看到的历史一致。 |
| 2 | `_run_direct` 递归 `_run_react_engine` + spawn_agent 无限递归 | **非 bug** — `_run_react_engine` 内部不调 `_run_direct`；递归仅在 `_run_direct` 的 `file_claim/denial` 路径（line 809/817）发生，且 `_run_react_engine` 不会再次返回 `file_claim/denial` 关键词（仅在 direct 路径中检测）。 |
| 2b | 同上（变体） | 同上 |
| 3 | `_TOOL_PATTERNS` 无 query 关键词 | **确认** — `_TOOL_PATTERNS` 中 0 条匹配"天气/价格/汇率/黄金/BTC/股价/今天/查/新闻"。这正是本次 query 修复的原因。 |
| 4 | `optimize_prompts=False` 时 `intent = detect_intent(user_input)` 仍执行 | **确认** — `_run_direct` 签名 `(user_input, model_ids, intent=None)`，`intent` 参数透传；line 718 `intent = self._detect_intent(user_input)` 在 `if self.optimize_prompts:` 之外。 |
| 5 | 中文 query `今天天气怎么样` 回归 | **通过** — `detect_intent("今天天气怎么样") == "query"`，路由 ReAct 成功（修复前为 `chat`，修复后正确）。 |
| 6 | ctx_mgr 单例无 reset | **观察项-5**（连续 query 累积 history，第 1 次 4 条 → 第 2 次 7 条；不影响路由判断，因 `detect_intent` 基于当前 `user_input`，不依赖 history） |
| 7 | ReAct 异常时 ctx_mgr 状态污染 | **观察项-6**（确认存在 — engine 抛 RuntimeError 时 line 840-841 仅 print+return，user 消息已 add（line 745），无 assistant 消息；下一轮 user 进来时 history 出现 user-only 序列） |

---

## 4. 发现的 Bug

### B-1: `write_code` 意图路由缺失

**严重度：P2**

**复现步骤：**
```bash
cd /home/xianyu-sheng/omniagent
.venv/bin/pytest tests/test_repl_real_tasks.py::TestWriteCodeIntent::test_write_code_routes_to_react -v
```

**预期 vs 实际：**
- 预期：`写一个 Python 爬虫` / `用 JS 写一个待办事项应用` 在 direct 模式下，识别为 `write_code` 意图后应路由到 ReAct 引擎（编程任务需工具执行）。
- 实际：`_detect_tool_need(text, intent="write_code")` 返回 `False`，走 direct LLM 模式，让 LLM 凭空"写"代码，不创建任何文件。

**根因：**
`omniagent/repl/repl.py:1051-1066` `_detect_tool_need` 方法：
```python
if intent == "query":
    return True
for pattern in cls._TOOL_PATTERNS:
    if pattern.search(text):
        return True
return False
```

仅对 `query` 意图做硬编码兜底。`write_code` 意图仍需通过 `_TOOL_PATTERNS` 匹配，而 `_TOOL_PATTERNS:1044-1046` 唯一编程类正则要求前缀 `^(?:帮我|请|给).{0,5}(?:写|做|创建|实现|开发|搭|建).{0,20}(?:一个|个|项目|工程|脚本|程序|代码)`。

- `帮我写一个快速排序函数` → 命中（"帮我" + "写" + "一个" + "函数"）
- `写一个 Python 爬虫` → 不命中（缺前缀）
- `用 JS 写一个待办事项应用` → 不命中（缺前缀，且"待办事项应用"不在触发词列表）
- `我想写一个 Python 脚本查询天气` → 不命中（"我想"不在"帮我/请/给"内）

**修复建议：**
在 `_detect_tool_need` 中对 `write_code` 意图也做兜底（与 `query` 同样处理）：
```python
if intent in ("query", "write_code"):
    return True
```

或扩展 `_TOOL_PATTERNS` 第 27 条正则：
```python
re.compile(r"(?:帮我?|请|给|想).{0,5}(?:写|做|创建|实现|开发|搭|建).{0,20}(?:一个|个|项目|工程|脚本|程序|代码|函数|应用|app)", re.I)
```

---

### B-2: 条件句 query 漏判

**严重度：P3**

**复现步骤：**
```python
from omniagent.repl.prompt_optimizer import detect_intent
print(detect_intent("如果今天下雨就告诉我"))  # 输出: None
```

**预期 vs 实际：**
- 预期：`如果今天下雨就告诉我` 包含"今天"和"下雨"两个 query 关键词，意图应识别为 `query`。
- 实际：返回 `None`，被识别为通用对话，走 direct LLM 模式，无法调用工具。

**根因：**
`omniagent/repl/prompt_optimizer.py:222-241` query 模板 trigger_patterns：
```python
r"(?:查询|查|看).{0,10}(?:天气|气温|温度|时间|日期|汇率|股价|新闻|黄金|金价|价格|行情)",
r"(?:天气|气温|温度).{0,10}(?:怎么样|如何|多少|预报)",
r"(?:黄金|金价|价格|股价|汇率|行情).{0,10}(?:多少|查询|怎么样|如何|今日|今天)",
r"(?:多少度|几度|热不热|冷不冷)",
r"该穿什么",
r"(?:穿什么|穿衣).{0,10}(?:合适|建议|好)",
r"(?:weather|forecast|temperature|time|date|gold|price).{0,15}",
r"(?:今天|今日|现在).{0,6}(?:黄金|金价|价格)",
```

没有"如果…就…"条件句匹配规则。`如果今天下雨就告诉我` 不含查/看等显式 trigger 词。

**修复建议：**
在 query 模板 trigger_patterns 添加：
```python
r"(?:如果|要是|假如|万一).{0,20}(?:下雨|天气|温度|下雨|气温|暴雨|雪).{0,15}(?:就|就告诉我|提醒我|告诉我|提醒)",
```

或更宽泛的"实时数据关键词"正则：
```python
r"(?:今天|今日|现在|目前).{0,6}(?:天气|温度|气温|下雨|下雪|晴|阴|多云)",
```

---

### B-3: `detect_intent("")` 返回 `None` 但 `optimize_prompts=True` 仍走完整流程

**严重度：P2**

**复现步骤：**
```python
from omniagent.repl.repl import REPL
from omniagent.repl.model_registry import ModelRegistry
import json, omniagent.engine.base as eb, omniagent.utils.llm_client as lc
eb.chat_completion = lambda *a, **kw: json.dumps({"thought": "t", "final_answer": "ok"})
lc.chat_completion = lambda *a, **kw: "ok"
lc.chat_completion_stream = lambda *a, **kw: (yield "ok")
reg = ModelRegistry()
reg.add_model("openai/gpt-4o", "gpt4", api_key="sk-test", base_url="https://api.test.com")
reg.assign_role("planner", ["gpt4"])
repl = REPL(registry=reg, streaming=False)
repl._handle_chat("")  # 空字符串
# history 中会出现 user: '' 一条
```

**预期 vs 实际：**
- 预期：空字符串输入应被拦截（`run()` 主循环在 line 165 有 `if not user_input: continue`），`_handle_chat("")` 至少应输出提示并 return。
- 实际：
  1. 仍触发完整 `_handle_chat` 流程（add_user_message("") + LLM 调用 + ctx_mgr 累积空消息）。
  2. 第 1 次空输入触发项目上下文注入（一次性副作用）。
  3. 连续两次空输入 → history 累积 2 条空 user 消息 + 1 条系统 prompt。
  4. 空 user 消息作为 LLM 输入的一部分发送，浪费 token 并可能干扰后续路由判断。

**根因：**
`omniagent/repl/repl.py:697` `_handle_chat` 方法：
```python
def _handle_chat(self, user_input: str) -> None:
    # ── Prompt 优化（按需） ──────────────────────────
    intent = self._detect_intent(user_input)
    ...
```

没有 `if not user_input or not user_input.strip(): return` 防护。`run()` 主循环（line 165）有防护，但 `_handle_chat` 是独立可调用的方法。

**修复建议：**
在 `_handle_chat` 入口添加：
```python
def _handle_chat(self, user_input: str) -> None:
    if not user_input or not user_input.strip():
        console.print("[dim]⚠️ 空输入已忽略[/dim]")
        return
    ...
```

---

### B-4: chat 模板污染 user content

**严重度：P3**

**复现步骤：**
```python
from omniagent.repl.prompt_optimizer import optimize_prompt
opt, hint, was_opt = optimize_prompt("你好")
print(repr(opt))
# 输出: '你好\n\n（这是一句问候/闲聊，简洁友好地回应即可，不要展开成长篇技术回答。）'
```

**预期 vs 实际：**
- 预期：闲聊类短输入不应被结构化模板污染。`optimize_prompt` 返回的应与原文一致（或不调用 `assess_quality` 触发优化）。
- 实际：被追加"（这是一句问候/闲聊，简洁友好地回应即可，不要展开成长篇技术回答。）"指令，且作为 user content 发给 LLM。
- 副作用：LLM 收到奇怪的指令上下文。`system_hint="你是一个友好的助手。对问候和致谢给出简短、自然的回应。"` 已通过 `add_system_message` 注入（第 732/737 行），重复。

**根因：**
`omniagent/repl/prompt_optimizer.py:265-278` chat 模板：
```python
PromptTemplate(
    intent="chat",
    trigger_patterns=[...],
    template=(
        "{task}\n\n"
        "（这是一句问候/闲聊，简洁友好地回应即可，不要展开成长篇技术回答。）"
    ),
    system_hint="你是一个友好的助手。对问候和致谢给出简短、自然的回应。",
)
```

模板将指令内联到 user content，而不是仅通过 `system_hint` 注入。导致 user 消息被污染。

**修复建议：**
chat 模板不应修改 user content，仅依赖 `system_hint`：
```python
template="{task}",  # 不追加指令
system_hint="你是一个友好的助手。对问候和致谢给出简短、自然的回应。",
```

或 `optimize_prompt` 对 chat 类输入直接 `return user_input, system_hint, False`（不做优化）。

---

## 5. 观察项（非 bug，但需关注）

### 观察项-1: trim_last_assistant 后递归失败状态污染（潜在）

- 路径：`repl.py:805-818` → `_run_react_engine`（line 809/817）
- 当前测试未触发（因 `_run_direct` 走 `_detect_tool_need` 时若命中即直接 `_run_react_engine`，不再经过 direct LLM，file_claim/denial 检测不到）
- 但当 `_detect_tool_need` 不命中、direct LLM 返回拒答/file_claim 时，trim + 递归 ReAct；若 ReAct 抛异常，user 消息已 add（line 745），assistant 消息被 trim，下一轮 history 出现 user-only 序列
- 验证：构造 `_detect_tool_need=False` + `_detect_file_claim=True` + ReAct 抛异常（需精细 mock，测试中通过 `test_suspicious_7` 类似方式间接确认）
- **建议**：在 line 818 后捕获异常时，弹错并清理 user 消息

### 观察项-2: ReAct 引擎异常时 user 消息无对应 assistant 响应

- 路径：`repl.py:836-841`
- 行为：`engine.run()` 抛异常 → `add_assistant_message` 跳过 → history 出现 user-only
- 影响：后续输入时 `ctx_mgr.get_messages()` 含孤立 user 消息；LLM 看到无上下文的"裸"user 消息
- 验证：`test_suspicious_7_react_exception_leaves_user_msg` 已确认
- **建议**：在 line 841 后调用 `self.ctx_mgr.trim_last_user()` 或 `add_assistant_message("[错误] ReAct 失败: ...")`

### 观察项-3: `/mode react` 后 query 检测白做

- 路径：`repl.py:718` 始终执行 `detect_intent`
- 行为：react 模式（line 760）走 `_run_react_engine` 不读 `intent`，检测白做
- 影响：1 次冗余正则匹配（10+ trigger × text 长度）
- **建议**：低优先，可忽略

### 观察项-4: plan-execute 引擎接到 query 文本生成无意义 plan

- 路径：`repl.py:843-857` + `plan_execute_engine.run`
- 行为：query 文本被强制解释为编程任务，LLM 生成 `[{step: 1, tool: spawn_agent, ...}]` 等不存在的工具；执行异常被 catch，无崩但输出无意义
- 影响：用户体验差（plan-execute 模式下 query 不可用）
- **建议**：在 plan-execute 引擎入口添加 query 路由决策（类似 ReAct），或让 `_handle_chat` 优先用 ReAct 处理 query

### 观察项-5: ctx_mgr 单例无 reset

- 路径：`REPL.__init__` line 70 + `_handle_chat` line 745
- 行为：连续 query 累积 history，第 1 次 4 条 → 第 2 次 7 条
- 影响：长会话可能触发 compact（line 702）；不影响路由判断
- **建议**：保持现状；用户可主动 `/compact` 或 `/clear`

### 观察项-6: `_handle_chat` 边界处理不完整

- 路径：`repl.py:697`
- 行为：空字符串、纯空格、纯数字等都进入完整流程
- 影响：浪费 LLM 调用、增加 history 噪声
- **建议**：入口加 `if not user_input.strip(): return` 防护

---

## 6. 修复点回归（query 路由修复）

| 验证点 | 修复前 | 修复后 | 测试位置 |
| --- | --- | --- | --- |
| `今天苏州的天气怎么样` | `chat` | `query` → 路由 ReAct | `TestQueryIntentRouting` |
| `今天黄金价格多少` | `chat` | `query` → 路由 ReAct | `TestQueryIntentRouting` |
| `现在美元兑人民币汇率多少` | `chat` | `query` → 路由 ReAct | `TestQueryIntentRouting` |
| `查看今天的科技新闻` | `chat` | `query` → 路由 ReAct | `TestQueryIntentRouting` |
| `北京现在几度` | `chat` | `query` → 路由 ReAct | `TestQueryIntentRouting` |
| `今天天气怎么样` | `chat` | `query` | `test_suspicious_5_chinese_query_regression` |
| `optimize_prompts=False` + query | （应仍路由）| 仍路由 ReAct | `TestOptimizePromptsOffQuery` |
| `non-query`（chat/explain）+ 无工具关键词 | 不路由 | 不路由 | `test_non_query_intent_not_auto_triggered` |

**8/8 通过**。未提交的 query 路由修复**无回归**。

---

## 7. 汇总

### 7.1 Bug 严重度分布

| 严重度 | 数量 | 列表 |
| --- | --- | --- |
| P0 | 0 | — |
| P1 | 0 | — |
| P2 | 2 | B-1（write_code 路由缺失）、B-3（空字符串未拦截） |
| P3 | 2 | B-2（条件句 query 漏判）、B-4（chat 模板污染） |

### 7.2 是否由用户新加的未提交修改引入的回归

| Bug | 来源 |
| --- | --- |
| B-1 | **与新修改相关** — 修复 query 路由时未同步处理 write_code（同样的硬编码兜底可加）。修复建议也提到在 `if intent in ("query", "write_code")` 一并处理。`【query 修复回归】` |
| B-2 | 与新修改**无关**（prompt_optimizer.py trigger_patterns 一直未覆盖条件句） |
| B-3 | 与新修改**无关**（`_handle_chat` 一直无空字符串防护） |
| B-4 | 与新修改**无关**（prompt_optimizer.py chat 模板一直内联指令到 user content） |

**1 个 bug 与 query 修复相关**（B-1），其余 3 个为既有 bug。

### 7.3 观察项汇总

| 观察项 | 严重度 | 建议处理 |
| --- | --- | --- |
| 观察项-1（trim + 递归失败） | P2 | 后续迭代加 try/except 清理 |
| 观察项-2（ReAct 异常状态污染） | P2 | 后续迭代加 user 消息清理 |
| 观察项-3（react 模式冗余检测） | P3 | 忽略 |
| 观察项-4（plan-execute 模式不处理 query） | P3 | 后续迭代 |
| 观察项-5（ctx_mgr 单例） | P3 | 保持现状 |
| 观察项-6（_handle_chat 边界） | P3 | 与 B-3 合并修复 |

---

## 8. 测试文件

- **新增**：`/home/xianyu-sheng/omniagent/tests/test_repl_real_tasks.py`（84 个测试用例）
- **未修改任何生产代码**
- **运行命令**：
  ```bash
  .venv/bin/pytest tests/test_repl_real_tasks.py -v
  .venv/bin/pytest tests/test_repl_real_tasks.py -v -s  # 含 print 输出
  ```

---

## 9. 结论

1. **未提交修改（`repl.py:718/784/1052`）质量合格**：query 路由修复 8/8 回归通过，无功能 bug。
2. **发现 2 个 P2 真 bug**（B-1 写代码路由缺失、B-3 空字符串未拦截）和 **2 个 P3 真 bug**（B-2 条件句漏判、B-4 chat 模板污染）。
3. **B-1 与新修改相关**，建议合并修复：`if intent in ("query", "write_code"): return True`。
4. **6 个观察项**（不记 bug，但应在后续迭代中处理）：trim+递归失败、ReAct 异常状态污染、react 模式冗余检测、plan-execute 模式 query 路由、ctx_mgr 单例、_handle_chat 边界防护。
5. **建议立即修复**：B-1（最小改动，1 行代码）+ B-3（3 行代码）。
