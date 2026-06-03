# OmniAgent-CLI 操作手册

> 版本: 0.1.0 | 更新日期: 2026-06-01

---

## 目录

1. [安装与配置](#1-安装与配置)
2. [快速开始](#2-快速开始)
3. [交互模式 (REPL)](#3-交互模式-repl)
4. [斜杠命令参考](#4-斜杠命令参考)
5. [批量执行模式](#5-批量执行模式)
6. [YAML 工作流配置](#6-yaml-工作流配置)
7. [思考范式说明](#7-思考范式说明)
8. [常见用法示例](#8-常见用法示例)
9. [故障排除](#9-故障排除)

---

## 1. 安装与配置

### 1.1 安装

```bash
# 克隆项目
git clone <repo-url>
cd omniagent-cli

# 安装（开发模式）
pip install -e ".[dev]"
```

### 1.2 配置全局凭证

API Key 统一存储在用户主目录下，所有工作区共享。

**方式一：凭证文件（推荐）**

创建 `~/.omniagent/credentials.yaml`：

```yaml
# Windows: %USERPROFILE%\.omniagent\credentials.yaml
# Linux/Mac: ~/.omniagent/credentials.yaml
openai: "sk-your-openai-key"
anthropic: "sk-ant-your-anthropic-key"
deepseek: "sk-your-deepseek-key"
```

**方式二：环境变量**

```bash
export OPENAI_API_KEY="sk-..."
export ANTHROPIC_API_KEY="sk-ant-..."
export DEEPSEEK_API_KEY="sk-..."
```

> 优先级：环境变量 > 凭证文件

### 1.3 验证安装

```bash
omniagent --help
```

---

## 2. 快速开始

### 2.1 启动交互模式

```bash
# 直接启动（使用默认配置）
omniagent chat

# 指定模型
omniagent chat -m anthropic/claude-3-5-sonnet

# 指定多个模型（自动 Fallback）
omniagent chat -m anthropic/claude-3-5-sonnet openai/gpt-4o deepseek/deepseek-coder

# 指定思考范式
omniagent chat -m anthropic/claude-3-5-sonnet --mode plan-execute
```

### 2.2 第一次对话

启动后，直接输入文字即可与 AI 对话：

```
You> 帮我写一个 Python 快速排序算法
```

### 2.3 运行工作流

```bash
# 批量执行
omniagent run config/default_flow.yaml --init-context task="写一个Hello World"

# 预览工作流结构
omniagent run config/default_flow.yaml --dry-run
```

---

## 3. 交互模式 (REPL)

### 3.1 两种输入方式

| 输入 | 行为 |
|------|------|
| 普通文本 | 发送给 AI 模型进行多轮对话 |
| `/command args` | 执行斜杠命令 |

### 3.2 会话生命周期

```
启动 → 配置模型 → 对话 → 保存会话 → 退出
                ↕
           /undo 回退
           /compact 压缩
```

### 3.3 退出方式

- `Ctrl+C` — 中断当前操作
- `Ctrl+D` — 退出程序

---

## 4. 斜杠命令参考

### 4.1 模型管理

#### `/set_model` — 添加或修改模型

```
/set_model <别名> <提供商/模型名> [参数...]
```

**示例：**

```
/set_model claude anthropic/claude-3-5-sonnet
/set_model gpt openai/gpt-4o
/set_model deepseek deepseek/deepseek-coder
/set_model local ollama/llama3 base_url=http://localhost:11434
/set_model custom openai/gpt-4o api_key=sk-xxx base_url=https://proxy.example.com/v1
```

**支持的参数：**
- `api_key=xxx` — 覆盖全局凭证
- `base_url=xxx` — 自定义 API 端点

#### `/remove_model` — 移除模型

```
/remove_model <别名>
```

#### `/models` — 查看所有模型

```
/models
```

输出示例：
```
已注册模型:

  [claude] anthropic/claude-3-5-sonnet
  [gpt] openai/gpt-4o
  [deepseek] deepseek/deepseek-coder

角色分配:
  planner: claude -> gpt
  coder: deepseek -> gpt
```

#### `/set_role` — 设置角色模型优先级

```
/set_role <角色名> <别名1> [别名2] [别名3] ...
```

**示例：**

```
/set_role planner claude gpt          # 规划优先用 claude，失败切 gpt
/set_role coder deepseek gpt          # 编码优先用 deepseek
/set_role reviewer gpt claude         # 审查用 gpt
```

---

### 4.2 思考范式

#### `/mode` — 切换或查看范式

```
/mode                    # 查看当前范式及可用列表
/mode plan-execute       # 切换到规划-执行模式
/mode react              # 切换到 ReAct 模式
/mode reflection         # 切换到反思模式
/mode plan-react         # 切换到规划+ReAct 模式
```

---

### 4.3 上下文管理

#### `/context` — 查看当前状态

```
/context
```

输出示例：
```
上下文状态:

  消息总数: 12
  用户消息: 6
  助手消息: 6
  估算 Token: 4,521 / 128,000 (3.5%)
  可回退次数: 3
  需要压缩: 否

AgentContext 变量:
  task: 写一个快速排序算法
  plan: 1. 定义 partition 函数...
```

#### `/compact` — 压缩对话历史

```
/compact                      # 自动摘要压缩
/compact 这是自定义的摘要文本    # 使用自定义摘要
```

> 当 Token 使用率超过 80% 时会自动提醒。

#### `/undo` — 回退对话

```
/undo     # 回退到上一个保存点
```

> 每次用户输入前自动保存快照，支持多次回退。

#### `/clear` — 清空历史

```
/clear
```

---

### 4.4 会话管理

#### `/save` — 保存会话

```
/save my-session
```

保存内容：对话历史、AgentContext 变量、模型配置。

#### `/load` — 加载会话

```
/load my-session
```

#### `/sessions` — 列出所有会话

```
/sessions
```

---

### 4.5 执行与调试

#### `/ask` — 向指定模型单次提问

```
/ask claude 什么是快速排序？
/ask deepseek 用 Python 写一个二分查找
```

> 不进入多轮对话历史，适合临时查询。

#### `/run` — 执行工作流

```
/run                                    # 使用当前范式的默认模板
/run config/default_flow.yaml           # 指定工作流文件
/run config/default_flow.yaml --init task=写排序算法
```

#### `/config` — 查看/保存配置

```
/config                    # 查看当前配置
/config save my-config.yaml # 保存到文件
```

#### `/code` — 生成代码并写入文件

```
/code <任务描述> [--file path] [--run] [--lang python]
```

**示例：**

```
/code 写一个快速排序算法
/code 写一个 HTTP 服务器 --file server.py --run
/code 创建一个 REST API --file api.py --lang python
```

> 一键生成代码 → 写入文件 → 可选运行。`--run` 会在生成后自动执行。

#### `/stream` — 切换流式输出

```
/stream          # 查看当前状态
/stream on       # 开启流式输出（默认）
/stream off      # 关闭流式输出
```

> 流式模式下，模型回复会逐字显示，而非等待全部生成完。

#### `/help` — 查看帮助

```
/help              # 列出所有命令
/help set_model    # 查看某个命令的详细用法
```

---

## 5. 批量执行模式

```bash
# 基本用法
omniagent run <workflow.yaml> [--init-context KEY=VALUE ...]

# 示例
omniagent run config/default_flow.yaml --init-context task="写一个计算器程序"

# 预览模式（不实际执行）
omniagent run config/default_flow.yaml --dry-run

# 详细日志
omniagent run config/default_flow.yaml -v --init-context task="..."
```

### 5.1 代码执行工作流

项目内置了两个代码执行工作流模板：

**简化版（推荐入门）：**

```bash
omniagent run config/simple_code_flow.yaml \
  --init-context task="写一个 Python 快速排序" \
  --init-context work_dir="./my_project"
```

流程：`生成代码 → 写入文件 → 运行 → 错误自动修复 → 最终报告`

**完整版（含测试和审查）：**

```bash
omniagent run config/code_execution_flow.yaml \
  --init-context task="写一个 REST API" \
  --init-context work_dir="./api_project" \
  --init-context retry_count=0
```

流程：`分析任务 → 生成代码 → 写入文件 → 运行测试 → 代码审查 → 修复循环 → 最终报告`

### 5.2 交互模式中快速生成代码

```bash
omniagent chat -m deepseek/deepseek-coder
```

```
You> /code 写一个快速排序 --run
You> /code 创建一个 Flask API --file app.py --run
You> /code 写单元测试 --file tests/test_sort.py
```

---

## 6. YAML 工作流配置

### 6.1 完整结构

```yaml
version: "1.1"
workflow: "工作流名称"

# 全局模型优先级
models:
  角色名:
    - "提供商/模型1"
    - "提供商/模型2"

# 起始节点
start_node: "节点ID"

# 节点定义
nodes:
  - id: "节点ID"
    type: "llm"           # llm | tool | router
    model: "角色名"        # LLMNode: 引用全局模型或直接指定
    prompt: "提示词"       # LLMNode: 支持 {变量} 替换
    system_prompt: "..."   # LLMNode: 系统提示词
    output_slot: "变量名"  # 结果写入 context 的 key
    next: "下一节点ID"     # 静态跳转
    temperature: 0.7       # LLMNode: 温度
    max_tokens: 4096       # LLMNode: 最大 token

    # ToolNode 专用
    action: "命令"         # 要执行的命令

    # RouterNode 专用
    rules:                 # 条件规则列表
      - condition:
          key: "变量名"
          op: "=="         # == | != | > | >= | < | <= | contains | is_truthy | is_falsy
          value: "期望值"
        next: "跳转节点ID"
    default_next: "默认跳转"
```

### 6.2 节点类型详解

#### LLMNode — 大模型调用

```yaml
- id: "generate_code"
  type: "llm"
  model: "coder"                    # 引用 models.coder
  # 或直接指定: model: "deepseek/deepseek-coder"
  # 或列表: model: ["anthropic/claude-3-5-sonnet", "openai/gpt-4o"]
  system_prompt: "你是一个 Python 专家"
  prompt: |
    任务: {task}
    请生成代码：
  output_slot: "code"
  next: "run_tests"
  temperature: 0.3
```

#### ToolNode — 命令执行

```yaml
- id: "run_tests"
  type: "tool"
  action: "python -m pytest tests/ -v"
  output_slot: "test_result"
  next: "check_result"
  timeout: 120
```

#### RouterNode — 条件路由

```yaml
- id: "check_result"
  type: "router"
  rules:
    - condition:
        key: "test_result"
        op: "contains"
        value: "passed"
      next: "success"
    - condition:
        key: "retry_count"
        op: ">="
        value: 3
      next: "give_up"
  default_next: "fix_code"
```

---

## 7. 思考范式说明

### 7.1 Plan-and-Execute（默认）

```
[强模型规划] → [便宜模型逐步执行] → [审查]
```

**适用场景：** 复杂任务、需要拆解的编程任务
**模型策略：** 规划用贵模型，执行用便宜模型

### 7.2 ReAct（思考-行动-观察）

```
[思考] → [行动] → [观察] → [思考] → ...
```

**适用场景：** 探索性任务、需要试错的场景
**模型策略：** 统一使用中等模型

### 7.3 Reflection（反思）

```
[执行] → [自我审查] → [修正] → [再审查]
```

**适用场景：** 需要高质量输出、代码重构
**模型策略：** 执行和审查可用不同模型

### 7.4 Plan-React（规划+ReAct）

```
[强模型规划] → [ReAct 逐步执行] → [总结]
```

**适用场景：** 兼顾策略与灵活的复杂任务
**模型策略：** 规划用贵模型，执行用中等模型

---

## 8. 常见用法示例

### 8.1 快速提问

```bash
omniagent chat -m anthropic/claude-3-5-sonnet
```

```
You> 什么是快速排序？请用 Python 实现
```

### 8.2 多模型 Fallback

```bash
omniagent chat -m anthropic/claude-3-5-sonnet openai/gpt-4o deepseek/deepseek-coder
```

```
You> /set_role planner claude gpt deepseek
You> 帮我写一个 Web 爬虫
```

> 如果 Claude 限流，自动切到 GPT-4o，再不行切 DeepSeek。

### 8.3 运行时切换模型

```
You> /set_model gemini google/gemini-pro
You> /set_role planner gemini
You> /models    # 确认配置
You> 重新帮我规划这个任务
```

### 8.4 保存和恢复会话

```
You> 帮我设计一个数据库方案
You> ...（多轮对话）...
You> /save db-design-session
You> /exit

# 下次
omniagent chat
You> /load db-design-session
You> 继续上次的设计
```

### 8.5 对话压缩

```
You> ...（很长的对话）...
You> /context    # 查看 token 用量
You> /compact    # 压缩历史
You> 继续刚才的工作
```

---

## 9. 故障排除

### 9.1 模型调用失败

**现象：** `❌ 所有模型均调用失败`

**排查：**
1. 检查凭证：`cat ~/.omniagent/credentials.yaml`
2. 检查网络：能否访问 API 端点
3. 检查 Key 是否有效/过期
4. 用 `/models` 确认模型配置正确

### 9.2 凭证文件找不到

**现象：** `未找到 xxx 的 API Key`

**解决：**
```bash
# 创建目录
mkdir -p ~/.omniagent

# 创建凭证文件
cat > ~/.omniagent/credentials.yaml << 'EOF'
openai: "sk-your-key"
anthropic: "sk-ant-your-key"
EOF
```

### 9.3 工作流执行失败

**现象：** `节点 xxx 执行失败`

**排查：**
1. 用 `--dry-run` 预览工作流结构
2. 用 `-v` 查看详细日志
3. 检查 YAML 语法是否正确
4. 确认 `output_slot` 和 `next` 引用的节点 ID 存在

### 9.4 Token 超限

**现象：** 模型返回 context length 相关错误

**解决：**
```
You> /context     # 查看用量
You> /compact     # 压缩历史
```
