# 🚀 OmniAgent-CLI

**本地多模型 AI 编程助手 — 像 Claude Code 一样编程，但完全可控**

OmniAgent-CLI 是一个纯 Python 命令行工具，让你在终端里拥有一个 AI 编程助手。它支持多个 AI 模型（DeepSeek、OpenAI、Claude 等），能理解你的项目结构，直接读写代码文件，并且所有配置和数据都保存在本地。

---

## ✨ 核心能力

| 能力 | 说明 |
|------|------|
| 🤖 **多模型支持** | DeepSeek、OpenAI、Claude、小米 MiMo 等 11 个厂商，自动切换 |
| 🧠 **4 种思考模式** | 直接对话、ReAct 推理、计划执行、反思修正 |
| 📁 **项目感知** | 自动识别项目类型（Python/Node/Rust...），加载项目规则 |
| ✏️ **代码编辑** | 直接修改代码文件，支持 LLM 辅助编辑 + 差异预览 |
| 💾 **跨会话记忆** | 记住你的偏好和项目信息，下次对话自动加载 |
| ⚡ **快捷指令** | 自定义命令和技能，一键执行复杂任务 |
| 🔧 **工具执行** | 运行命令、读写文件、搜索代码、Git 操作、网页抓取 |

---

## 📦 安装

```bash
# 克隆项目
git clone https://github.com/xianyu-sheng/omniagent.git
cd omniagent

# 安装（开发模式）
pip install -e .
```

**要求：** Python 3.10+

---

## ⚡ 快速开始

### 1. 配置 API Key

```bash
# 启动配置向导
omniagent
```

进入后输入 `/setup`，按菜单操作即可。支持的厂商：

| 厂商 | 模型示例 |
|------|----------|
| DeepSeek | deepseek-chat, deepseek-coder |
| OpenAI | gpt-4o, gpt-4o-mini |
| Anthropic | claude-sonnet-4-20250514 |
| 小米 MiMo | MiMo-7B-RL |
| 智谱 GLM | glm-4-plus |
| 阿里千问 | qwen-max |
| 更多... | Google, Kimi, 百川, MiniMax, Ollama |

### 2. 开始对话

```bash
# 直接启动
omniagent

# 输入问题即可
You: 帮我写一个 Python 快速排序
```

### 3. 切换模型

```
You: /model
```

选择已配置的模型即可切换。

---

## 🎯 常用命令

| 命令 | 说明 |
|------|------|
| `/setup` | 配置向导（API Key、模型、范式） |
| `/model` | 切换当前模型 |
| `/mode` | 切换思考范式（direct/react/plan-execute/reflection） |
| `/project` | 查看项目上下文（类型、文件树、规则） |
| `/edit <文件> <指令>` | LLM 辅助编辑代码 |
| `/memory` | 管理跨会话记忆 |
| `/shortcut` | 创建/管理快捷指令 |
| `/skill` | 创建/管理技能（LLM + 工具组合） |
| `/compact` | 压缩对话历史（节省 Token） |
| `/save` / `/load` | 保存/加载会话 |
| `/help` | 查看所有命令 |

---

## 🧠 思考范式

OmniAgent 支持 4 种 AI 思考方式，适用于不同场景：

| 范式 | 说明 | 适用场景 |
|------|------|----------|
| **direct** | 直接对话，快速回答 | 日常问答、简单任务 |
| **react** | 思考→行动→观察循环 | 探索性任务、调试 |
| **plan-execute** | 先规划再执行 | 复杂编程、多步骤任务 |
| **reflection** | 执行→审查→修正 | 代码审查、高质量输出 |

```
You: /mode react
```

---

## 📁 项目感知

OmniAgent 会自动检测你的项目：

- **项目类型** — 根据 `pyproject.toml`、`package.json`、`Cargo.toml` 等自动识别
- **文件树** — 构建精简的项目结构视图
- **项目规则** — 加载 `.omniagent/rules.md` 中的自定义规则

创建 `.omniagent/rules.md` 来定制 AI 行为：

```markdown
# 项目规则
- 使用 Python 3.12
- 遵循 PEP 8
- 使用 pytest 测试
- 所有函数必须有类型注解
```

---

## ✏️ 代码编辑

直接在对话中修改代码：

```
You: /edit src/main.py 把所有 print 改为 logging.info
```

AI 会：
1. 读取文件内容
2. 生成修改方案
3. 展示差异对比
4. 等你确认后应用修改

---

## ⚡ 快捷指令

创建常用命令的快捷方式：

```
You: /shortcut create
名称: test
描述: 运行测试
命令: python -m pytest tests/ -v
```

之后只需 `/test` 即可执行。

---

## 🛠 技能系统

创建复杂的多步骤技能：

```
You: /skill create
名称: code_review
描述: 审查代码质量并给出改进建议
模式: 1. 🤖 智能生成
```

AI 会自动生成完整的技能步骤，你可以直接使用或修改。

---

## 📝 自定义规则

在项目根目录创建 `.omniagent/rules.md`：

```markdown
# 代码风格
- 使用 4 空格缩进
- 函数名使用 snake_case
- 类名使用 PascalCase

# 测试要求
- 每个函数都要有单元测试
- 测试覆盖率 > 80%

# Git 规范
- commit message 使用中文
- 格式: <类型>: <描述>
```

---

## 🔒 安全说明

- **API Key 存储在本地** — `~/.omniagent/credentials.yaml`，不会上传到任何地方
- **代码在本地执行** — 所有操作都在你的电脑上完成
- **开源透明** — 所有代码可审查

---

## 📄 许可证

MIT License

---

## 🙏 致谢

感谢以下开源项目：
- [Rich](https://github.com/Textualize/rich) — 终端美化
- [httpx](https://github.com/encode/httpx) — HTTP 客户端
- [PyYAML](https://github.com/yaml/pyyaml) — YAML 解析
