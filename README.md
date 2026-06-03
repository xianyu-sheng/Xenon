# OmniAgent-CLI

> 可配置的多模型 AI 编程 Agent 调度引擎

一个纯 Python 本地命令行工具，白盒化、解耦且配置驱动。通过 YAML 配置文件定义 Agent 的思考范式流转图（ReAct, Plan-and-Execute 等），支持多模型优先级轮询与自动降级。

## 核心特性

- **全局凭证解耦** — API Key 统一存储在 `~/.omniagent/credentials.yaml`，各工作区只配流转逻辑
- **多模型 Fallback** — 按优先级尝试多个模型，限流或报错自动切换
- **图驱动范式** — YAML 定义节点拓扑，引擎动态调度执行
- **原子化节点** — LLM 调用、文件读写、命令执行、条件路由，各司其职
- **交互式 REPL** — 多轮对话、流式输出、斜杠命令
- **代码执行闭环** — 生成代码 → 写入文件 → 运行测试 → 自动修复
- **白盒透明** — 每一步执行过程、模型选择、Context 变化完全可见

## 快速开始

### 1. 安装

```bash
git clone <repo-url>
cd omniagent-cli
pip install -e ".[dev]"
```

### 2. 配置全局凭证

创建 `~/.omniagent/credentials.yaml`：

```yaml
openai: "sk-your-openai-key"
anthropic: "sk-ant-your-anthropic-key"
deepseek: "sk-your-deepseek-key"
```

### 3. 启动交互模式

```bash
# 启动交互式 REPL
omniagent chat -m anthropic/claude-3-5-sonnet openai/gpt-4o

# 指定思考范式
omniagent chat -m deepseek/deepseek-coder --mode react
```

### 4. 在 REPL 中使用

```
You> /set_model claude anthropic/claude-3-5-sonnet
You> /set_model gpt openai/gpt-4o
You> /set_role planner claude gpt
You> 帮我写一个快速排序算法
You> /code 写一个 HTTP 服务器 --file server.py --run
You> /compact
You> /save my-session
```

### 5. 批量执行工作流

```bash
# Plan-and-Execute 工作流
omniagent run config/default_flow.yaml --init-context task="写一个计算器"

# 代码执行工作流（生成→写入→运行→修复）
omniagent run config/simple_code_flow.yaml \
  --init-context task="写快速排序" \
  --init-context work_dir="./my_project"
```

## 项目结构

```
omniagent-cli/
├── pyproject.toml                      # 项目元数据与依赖
├── README.md
├── docs/
│   └── OPERATION_GUIDE.md              # 详细操作手册
├── config/
│   ├── default_flow.yaml               # Plan-and-Execute 工作流
│   ├── simple_code_flow.yaml           # 简化代码执行工作流
│   ├── code_execution_flow.yaml        # 完整代码执行工作流
│   └── credentials.example.yaml        # 凭证模板
├── omniagent/
│   ├── __init__.py
│   ├── main.py                         # CLI 入口
│   ├── engine/
│   │   ├── context.py                  # 全局上下文总线
│   │   └── scheduler.py               # DAG 图调度器
│   ├── nodes/
│   │   ├── base.py                     # BaseNode 抽象基类
│   │   ├── llm_node.py                # LLM 调用节点（多模型 Fallback）
│   │   ├── tool_node.py               # 工具节点（命令/文件读写）
│   │   └── router_node.py             # 条件路由节点
│   ├── repl/
│   │   ├── repl.py                     # 交互式主循环（流式输出）
│   │   ├── commands.py                 # 斜杠命令处理器
│   │   ├── context_manager.py          # 对话历史与 Token 管理
│   │   ├── model_registry.py           # 运行时模型管理
│   │   └── session.py                  # 会话持久化
│   └── utils/
│       ├── llm_client.py              # 多厂商 LLM 适配器（支持流式）
│       └── config_parser.py           # YAML 配置解析器
└── tests/
    ├── test_core.py                    # 核心模块测试
    ├── test_repl.py                    # REPL 模块测试
    └── test_tools.py                   # 工具节点测试
```

## 斜杠命令

| 命令 | 功能 |
|------|------|
| `/set_model <alias> <provider/model>` | 添加/修改模型 |
| `/models` | 查看所有模型 |
| `/set_role <role> <alias1> [alias2]` | 设置角色优先级 |
| `/mode [name]` | 切换思考范式 |
| `/code <任务> [--file path] [--run]` | 生成代码并写入文件 |
| `/stream [on\|off]` | 切换流式输出 |
| `/compact` | 压缩对话历史 |
| `/undo` | 回退对话 |
| `/save <name>` / `/load <name>` | 保存/加载会话 |
| `/context` | 查看上下文状态 |
| `/ask <alias> <question>` | 向指定模型提问 |
| `/help` | 查看帮助 |

## 节点类型

| 节点 | 职责 | 关键配置 |
|------|------|----------|
| **LLMNode** | 调用大语言模型，支持多模型 Fallback | `model`, `prompt`, `output_slot` |
| **ToolNode** | 命令执行 / 文件读写 | `action_type`, `action`, `file_path`, `content` |
| **RouterNode** | 条件路由，决定下一跳 | `rules`, `default_next` |

## 运行测试

```bash
pytest tests/ -v
```

## 详细文档

- [操作手册](docs/OPERATION_GUIDE.md) — 完整的使用指南

## License

MIT
