# CLAUDE.md — OmniAgent 项目规范

## Bug 修复原则

### 深度根因分析，而非逐洞补漏

当发现一个 bug 时，**不要**只修复眼前的症状。必须追溯数据流全链路，
找到系统性根因，然后**一次性修复所有同类问题**。

**判断标准**：如果修复只改了 1 个文件/1 个函数，很可能是"补漏"而非
"治本"。系统性修复通常需要跨 2-4 个文件建立正确的信息流动路径。

**方法**：
1. 画出受影响功能的完整数据流（从用户输入 → 中间层 → 最终输出）
2. 找出所有信息断裂点（数据存在但未传递到需要它的地方）
3. 设计统一的集成契约（一个接口/一个数据结构/一个生命周期钩子）
4. 在所有断点处同时修复，确保端到端信息流通

**反例**（禁止）：
- 发现 LLM 猜错 MCP 工具名 → 只改 system prompt
- 发现 MCP 输出为空 → 只改 `_mcp_call` 返回值
- 发现 MCP 重启丢失 → 只在 `/mcp add` 加保存逻辑

**正例**（期望）：
- 画出 MCP 的完整数据流：注册 → 发现 → 提示注入 → 调用 → 结果提取 → 持久化
- 一次性修复所有断裂点，建立统一的 MCP 集成契约

### 通用设计，而非特例枚举

绝不使用封闭集合（如正则枚举"天气|高铁|酒店"）来分类/路由。
使用**基于结构特征**的通用规则（如疑问句式、查询动词、时间敏感度）。

**反例**：
```python
# 每次新增 MCP 工具都要加正则 — 不可持续
r"(?:查|查一下).{0,10}(?:高铁|火车|动车|航班|机票)"
```

**正例**：
```python
# 基于语言结构，不依赖领域关键词 — 任何 MCP 工具自动受益
r"(?:查|搜|找|查询).{0,20}"  # 通用查询动词
```

## MCP 集成架构

### 当前问题

MCP 子系统的各项功能（注册、发现、调用、持久化）是孤立实现的，
缺少统一的集成层。导致多个信息断裂点：

1. **MCP 工具 → LLM 提示词**：引擎的 system prompt 构建时不知道 MCP 工具存在
2. **MCP 结果 → LLM 观察**：`_mcp_call` 的返回值缺少标准化的文本字段
3. **MCP 配置 → 磁盘**：注册表纯内存，无持久化
4. **MCP 命令 → 解析器**：参数解析不支持 `--` 分隔符

### 目标架构

MCP 集成应遵循一个统一的契约：

```
MCPRegistry (唯一真相源)
  ├── 注册/发现 → tool_map {name: (server, tool_def)}
  ├── 持久化 → credentials.yaml _mcp_servers 段
  ├── 启动恢复 → _auto_connect_mcp_servers()
  ├── → 引擎注入: _build_mcp_tools_list() → engine._mcp_tools_list → system_prompt
  └── → 结果提取: _mcp_call 返回 dict 包含 "content" 字段 (与 read_file 一致)
```

### 各引擎对 MCP 的支持要求

所有引擎（ReAct / PlanExecute / Reflection / PlanReact / PlanReflection /
ReactReflection / Novel）在创建后都应调用 `_inject_mcp_tools_into_engine()`，
确保 LLM 在任意范式下都能看到可用的 MCP 工具列表。

## 项目结构

- `omniagent/engine/` — 引擎层（ReAct/PlanExecute/Reflection 等）
- `omniagent/nodes/` — 节点层（ToolNode, ToolExecutor）
- `omniagent/repl/` — REPL 层（命令、模型池、路由、会话）
- `omniagent/mcp/` — MCP 子系统（transport, client, registry）

### 修复后必须真实验证

**每次自认为修好 bug 后，必须真正启动 omniagent 复现原问题场景，
确认修复生效，并跑同类型的其他任务验证无回归。**

**方法**：
1. 复现原 bug 的确切输入，确认不再报错且结果正确
2. 跑 1-2 个同类型但不同参数/场景的变体任务
3. 如果涉及 MCP/工具，确认端到端输出完整（不是"无文本输出"）

**反例**（禁止）：
- 改了代码 → 只跑单元测试 → 提交推送
- 单元测试 1110 全绿 ≠ 真实场景可用

**正例**（期望）：
- 改完 MCP 输出 bug → 启动 omniagent → `/mcp add 12306` →
  "查昆山到上海高铁" → 确认 LLM 收到完整车次数据

## 版本与发布

- 版本号: `__init__.py` + `pyproject.toml` + `repl.py` 兜底值 三处统一
- CHANGELOG: 按版本倒序，每版本记录变更类别和测试结果
- 测试: 1110 个单元测试，`python3 -m pytest tests/ -q`
- 发布: `git tag -a vX.Y.Z` + `gh release create`

### 每次修改后自动安装

**每次 `git commit` Python 代码后，post-commit hook 自动执行
`pip install -e . --break-system-packages`，确保 `omniagent` 命令
始终指向最新代码。**

- Hook 位置: `.git/hooks/post-commit`
- 触发条件: 仅当 `omniagent/` 下有 `.py` 文件变更
- 手动安装: `pip install -e . --break-system-packages`
