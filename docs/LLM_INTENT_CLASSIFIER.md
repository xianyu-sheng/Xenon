# LLM 意图分类器

## 概述

LLM 意图分类器是 Xenon 意图识别系统的增强组件。当传统的正则表达式分类器无法识别用户意图时，会自动回退到 LLM 分类器进行二次判断。

## 设计原则

1. **快速响应**：使用轻量级快速模型（如 Claude Haiku、GPT-4o-mini），控制在 200ms 内
2. **准确分类**：提供清晰的分类标准和示例，确保分类准确性
3. **优雅降级**：LLM 不可用或失败时优雅降级到正则结果，不影响主流程
4. **可配置**：用户可通过配置文件或环境变量启用/禁用

## 工作流程

```
用户输入
    ↓
正则分类器 (detect_intent)
    ↓
  有结果? ──Yes→ 返回意图
    ↓
   No
    ↓
LLM 分类器 (classify_intent_with_llm)
    ↓
  有结果 且 置信度 >= 阈值? ──Yes→ 返回意图
    ↓
   No
    ↓
返回 None (无法识别)
```

## 配置方式

### 方式 1：配置文件 (~/.xenon/config.yaml)

```yaml
intent_classifier:
  # 启用 LLM 意图分类器
  enabled: true

  # 指定使用的模型（留空使用默认）
  # 推荐快速小模型：
  # - anthropic/claude-3-5-haiku-20241022
  # - openai/gpt-4o-mini
  # - deepseek/deepseek-v4-flash
  model: "anthropic/claude-3-5-haiku-20241022"

  # 置信度阈值（0-1），低于此值返回 None
  confidence_threshold: 0.7

  # 单次调用超时（秒）
  timeout: 5.0
```

### 方式 2：环境变量

```bash
# 启用 LLM 分类器
export XENON_INTENT_CLASSIFIER_ENABLED=true

# 指定模型
export XENON_INTENT_CLASSIFIER_MODEL="anthropic/claude-3-5-haiku-20241022"

# 置信度阈值
export XENON_INTENT_CLASSIFIER_CONFIDENCE=0.7

# 超时设置
export XENON_INTENT_CLASSIFIER_TIMEOUT=5.0
```

## 支持的意图类别

LLM 分类器支持与正则分类器相同的 12 个意图类别：

| 意图类别 | 描述 | 示例 |
|---------|------|------|
| `write_code` | 编写新代码 | "写一个排序函数" |
| `debug` | 调试修复 bug | "这段代码报错了" |
| `explain` | 解释代码 | "这段代码是什么意思" |
| `refactor` | 重构优化 | "优化这段代码的性能" |
| `write_test` | 编写测试 | "给这个函数写测试" |
| `design` | 架构设计 | "设计一个用户系统" |
| `convert` | 转换迁移 | "把这段 Python 代码转成 Go" |
| `novel` | 小说创作 | "续写这个故事" |
| `query` | 实时查询 | "今天北京天气" |
| `research` | 资料调研 | "调研最好的 Web 框架" |
| `write_doc` | 编写文档 | "写一份 API 文档" |
| `chat` | 闲聊对话 | "你好" |

## 使用示例

### 编程方式

```python
from xenon.repl.llm_intent_classifier import classify_intent_with_llm

# 基本用法
intent = classify_intent_with_llm("帮我实现一个快速排序")
print(intent)  # 输出: "write_code"

# 带上下文
context = [
    {"role": "user", "content": "我在做数据分析"},
    {"role": "assistant", "content": "好的，需要什么帮助？"},
]
intent = classify_intent_with_llm("帮我处理这些数据", context_messages=context)
```

### 自动集成

LLM 分类器已经自动集成到 `DifficultyEstimator` 中，无需手动调用：

```python
from xenon.repl.difficulty_estimator import DifficultyEstimator

estimator = DifficultyEstimator()
profile = estimator.estimate("一些正则无法识别的复杂请求")

# 如果正则分类器返回 None，会自动尝试 LLM 分类器
print(profile.intent)  # 可能是 LLM 分类的结果
```

## 性能考虑

### 延迟

- **正则分类器**：<1ms（几乎无延迟）
- **LLM 分类器**：50-200ms（取决于模型和网络）

### 成本

使用快速小模型的成本极低：

- **Claude 3.5 Haiku**：~$0.0001 / 次（约 200 tokens）
- **GPT-4o-mini**：~$0.00003 / 次
- **DeepSeek V4 Flash**：~$0.000014 / 次

一天分类 1000 次，成本不到 $0.1。

### 缓存优化

- 系统提示词固定，可以被 prompt caching 缓存
- 实际每次调用只需发送用户输入（通常 <100 tokens）

## 调试

### 查看分类日志

```python
import logging

# 启用调试日志
logging.getLogger("xenon.repl.llm_intent_classifier").setLevel(logging.DEBUG)
```

日志会显示：
- 分类成功：意图、置信度、耗时
- 分类失败：错误原因
- 置信度过低：具体值和阈值

### 测试分类效果

```bash
# 运行单元测试
pytest tests/test_llm_intent_classifier.py -v

# 运行集成测试（需要真实 API）
pytest tests/test_llm_intent_classifier.py::TestIntentClassifierIntegration -v
```

## 故障排查

### LLM 分类器不生效

1. **检查是否启用**
   ```bash
   # 查看配置
   cat ~/.xenon/config.yaml | grep -A5 "intent_classifier"
   ```

2. **检查 API Key**
   ```bash
   # 确保对应模型的 API Key 已配置
   cat ~/.xenon/credentials.yaml
   ```

3. **查看日志**
   ```python
   import logging
   logging.basicConfig(level=logging.DEBUG)
   ```

### LLM 返回无效结果

- LLM 返回的 JSON 格式错误
- LLM 返回的意图类别不在支持列表中

这些情况下会自动降级，返回 `None`，并记录警告日志。

### 分类太慢

1. **切换到更快的模型**
   ```yaml
   model: "deepseek/deepseek-v4-flash"  # 最快
   ```

2. **调整超时时间**
   ```yaml
   timeout: 3.0  # 降低超时
   ```

3. **禁用 LLM 分类器**
   ```yaml
   enabled: false  # 只使用正则分类器
   ```

## 扩展开发

### 添加新的意图类别

1. 在 `INTENT_CATEGORIES` 中添加新类别：
   ```python
   INTENT_CATEGORIES = {
       # ... 现有类别
       "new_intent": "新意图的描述",
   }
   ```

2. 在 `prompt_optimizer.py` 中添加对应的正则规则

3. 系统提示词会自动包含新类别

### 自定义分类逻辑

可以继承 `LLMIntentClassifier` 并重写方法：

```python
from xenon.repl.llm_intent_classifier import LLMIntentClassifier

class CustomClassifier(LLMIntentClassifier):
    def _build_system_prompt(self):
        # 自定义系统提示词
        return "你的自定义提示词..."

    def _parse_response(self, response_text):
        # 自定义响应解析逻辑
        result = super()._parse_response(response_text)
        # 添加额外处理
        return result
```

## 最佳实践

1. **生产环境建议启用**：提升意图识别准确率，成本极低
2. **选择快速模型**：优先 Haiku 或 4o-mini，响应快成本低
3. **合理设置阈值**：置信度阈值 0.7 是个好的起点
4. **监控分类效果**：定期查看日志，评估分类准确性
5. **离线环境可禁用**：无网络环境下禁用 LLM 分类器

## 相关文件

- `xenon/repl/llm_intent_classifier.py` - LLM 分类器实现
- `xenon/repl/difficulty_estimator.py` - 集成点
- `xenon/repl/system_config.py` - 配置定义
- `tests/test_llm_intent_classifier.py` - 单元测试
- `~/.xenon/config.yaml` - 用户配置文件
