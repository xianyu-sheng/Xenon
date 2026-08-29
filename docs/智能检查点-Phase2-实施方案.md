# Phase 2: 语义边界检测 - 实施方案

## 📋 目标

实现智能的语义边界检测器，在模型中断续写时，自动回滚到最近的完整语义单元（代码块、段落、句子），确保续写内容语义连贯。

---

## 🎯 核心问题

**Phase 1 的问题**：
```python
# 不好的续写点
partial = "def calculate(n):\n    if n <= 1:\n        ret"
#                                                   ^^^ 在单词中间截断

continuation = "urn n  # 新模型不知道前面是 'ret'"
# 结果：语义不连贯 ❌
```

**Phase 2 的解决方案**：
```python
# 智能回滚到完整边界
original_partial = "def calculate(n):\n    if n <= 1:\n        ret"
rollback_to = "def calculate(n):\n    if n <= 1:"
incomplete = "        ret"

# 续写提示包含不完整部分
prompt = f"上述代码在 'if n <= 1:' 后中断，不完整部分：'        ret'，请继续"
# 结果：语义连贯 ✅
```

---

## 🏗️ 架构设计

### 核心类

```python
class BoundaryDetector:
    """语义边界检测器"""
    
    def find_last_boundary(
        self, 
        text: str, 
        content_type: ContentType = ContentType.AUTO
    ) -> BoundaryResult:
        """找到最后一个完整的语义边界"""
        
class BoundaryResult:
    """边界检测结果"""
    complete_part: str      # 完整部分（到边界为止）
    incomplete_part: str    # 不完整部分（边界之后）
    boundary_type: str      # 边界类型（code_block/paragraph/sentence）
    boundary_position: int  # 边界位置（字符索引）
    confidence: float       # 检测置信度（0-1）
```

### 边界类型层级

```
优先级（从高到低）：
1. 代码块边界 (Code Block)
   - 完整的函数/类定义
   - 完整的代码块（闭合的 {}）
   - 完整的语句（带分号的语言）

2. Markdown 边界 (Markdown Structure)
   - 完整的代码块（```）
   - 完整的标题
   - 完整的列表项

3. 段落边界 (Paragraph)
   - 双换行符分隔

4. 句子边界 (Sentence)
   - 句号、问号、感叹号
   - 完整的句子结构

5. 词语边界 (Word)
   - 空格、标点符号
   - 最后的保底方案
```

---

## 📝 详细实现

### Step 2.1: 创建数据结构

**文件**: `xenon/utils/semantic_boundary.py`

```python
from enum import Enum
from dataclasses import dataclass

class ContentType(Enum):
    """内容类型"""
    AUTO = "auto"          # 自动检测
    CODE = "code"          # 代码
    MARKDOWN = "markdown"  # Markdown 文档
    TEXT = "text"          # 纯文本

class BoundaryType(Enum):
    """边界类型"""
    CODE_BLOCK = "code_block"      # 代码块
    FUNCTION = "function"          # 函数定义
    CLASS = "class"                # 类定义
    STATEMENT = "statement"        # 语句
    MARKDOWN_CODE = "markdown_code"  # Markdown 代码块
    MARKDOWN_HEADER = "markdown_header"  # Markdown 标题
    PARAGRAPH = "paragraph"        # 段落
    SENTENCE = "sentence"          # 句子
    WORD = "word"                  # 词语
    NONE = "none"                  # 无边界（保留全部）

@dataclass
class BoundaryResult:
    """边界检测结果"""
    complete_part: str
    incomplete_part: str
    boundary_type: BoundaryType
    boundary_position: int
    confidence: float
    metadata: dict = field(default_factory=dict)
```

### Step 2.2: 实现代码边界检测

```python
class CodeBoundaryDetector:
    """代码边界检测器"""
    
    def detect_function_boundary(self, code: str) -> int:
        """检测完整函数定义的结束位置"""
        # 支持 Python, JavaScript, Java, C++, Go 等
        
    def detect_class_boundary(self, code: str) -> int:
        """检测完整类定义的结束位置"""
        
    def detect_statement_boundary(self, code: str) -> int:
        """检测完整语句的结束位置"""
        # Python: 完整的缩进块
        # JavaScript/Java/C++: 完整的 {} 或 ;
        
    def detect_brace_balance(self, code: str) -> int:
        """检测括号平衡的位置"""
        # {}, [], () 都要匹配
```

### Step 2.3: 实现 Markdown 边界检测

```python
class MarkdownBoundaryDetector:
    """Markdown 边界检测器"""
    
    def detect_code_block_boundary(self, text: str) -> int:
        """检测完整的 Markdown 代码块（```）"""
        
    def detect_header_boundary(self, text: str) -> int:
        """检测完整的标题"""
        # # Header, ## Header, ### Header
        
    def detect_list_item_boundary(self, text: str) -> int:
        """检测完整的列表项"""
        # - item, * item, 1. item
```

### Step 2.4: 实现文本边界检测

```python
class TextBoundaryDetector:
    """文本边界检测器"""
    
    def detect_paragraph_boundary(self, text: str) -> int:
        """检测段落边界（双换行）"""
        
    def detect_sentence_boundary(self, text: str) -> int:
        """检测句子边界"""
        # 中文：。！？
        # 英文：. ! ?
        # 考虑缩写（Mr. Dr.）和小数点
        
    def detect_word_boundary(self, text: str) -> int:
        """检测词语边界（保底方案）"""
        # 空格、标点符号
```

### Step 2.5: 实现主检测器

```python
class BoundaryDetector:
    """语义边界检测器（统一入口）"""
    
    def __init__(self):
        self.code_detector = CodeBoundaryDetector()
        self.markdown_detector = MarkdownBoundaryDetector()
        self.text_detector = TextBoundaryDetector()
    
    def find_last_boundary(
        self, 
        text: str, 
        content_type: ContentType = ContentType.AUTO
    ) -> BoundaryResult:
        """找到最后一个完整的语义边界"""
        
        # 1. 自动检测内容类型
        if content_type == ContentType.AUTO:
            content_type = self._detect_content_type(text)
        
        # 2. 按优先级尝试各种边界
        if content_type == ContentType.CODE:
            # 代码边界优先级最高
            result = self._try_code_boundaries(text)
            if result:
                return result
        
        if content_type == ContentType.MARKDOWN:
            result = self._try_markdown_boundaries(text)
            if result:
                return result
        
        # 3. 文本边界（保底）
        return self._try_text_boundaries(text)
    
    def _detect_content_type(self, text: str) -> ContentType:
        """自动检测内容类型"""
        # 启发式规则：
        # - 有 def/class/function/public 等关键字 → CODE
        # - 有 ```/# /- [ ] 等 Markdown 标记 → MARKDOWN
        # - 其他 → TEXT
```

---

## 🧪 测试用例

### 代码边界测试

```python
def test_python_function_boundary():
    code = """def calculate(n):
    if n <= 1:
        return n
    ret"""  # 不完整
    
    result = detector.find_last_boundary(code, ContentType.CODE)
    
    assert result.complete_part.endswith("return n")
    assert result.incomplete_part == "    ret"
    assert result.boundary_type == BoundaryType.STATEMENT

def test_javascript_function_boundary():
    code = """function calculate(n) {
    if (n <= 1) {
        return n;
    }
    ret"""
    
    result = detector.find_last_boundary(code, ContentType.CODE)
    
    assert result.complete_part.endswith("}")
    assert result.incomplete_part == "    ret"
```

### Markdown 边界测试

```python
def test_markdown_code_block():
    text = """# 标题

```python
def foo():
    return 42
```

这是段落，后面不完"""

    result = detector.find_last_boundary(text, ContentType.MARKDOWN)
    
    assert "```" in result.complete_part
    assert result.incomplete_part == "这是段落，后面不完"
```

### 文本边界测试

```python
def test_paragraph_boundary():
    text = """第一段内容。
这是第一段的第二句。

第二段内容。这是第二段的第二句"""

    result = detector.find_last_boundary(text, ContentType.TEXT)
    
    assert result.complete_part.endswith("这是第一段的第二句。")
    assert "第二段" in result.incomplete_part
```

---

## 🔌 集成到 Phase 1

### 修改 `xenon/engine/base.py`

```python
# 在 _call_llm_once 中，捕获 PartialResponseError 后：

except PartialResponseError as e:
    partial = e.partial
    
    if partial.is_valid(min_length=100):
        # Phase 2: 语义边界检测
        from xenon.utils.semantic_boundary import BoundaryDetector, ContentType
        
        detector = BoundaryDetector()
        boundary_result = detector.find_last_boundary(
            partial.content,
            content_type=ContentType.AUTO
        )
        
        # 使用回滚后的完整部分
        complete_content = boundary_result.complete_part
        incomplete_part = boundary_result.incomplete_part
        
        logger.info(
            tp(
                f"⚡ 检测到边界: {boundary_result.boundary_type.value}, "
                f"完整部分: {len(complete_content)} 字符, "
                f"不完整部分: {len(incomplete_part)} 字符"
            )
        )
        
        # 构造更智能的续写 messages
        continuation_messages = list(original_messages) + [
            {"role": "assistant", "content": complete_content},
            {
                "role": "user",
                "content": self._build_continuation_prompt(
                    boundary_result, incomplete_part
                ),
            },
        ]
```

### 智能续写提示

```python
def _build_continuation_prompt(
    self, 
    boundary: BoundaryResult, 
    incomplete: str
) -> str:
    """根据边界类型构造智能续写提示"""
    
    if boundary.boundary_type == BoundaryType.CODE_BLOCK:
        return (
            f"上述代码在完整的代码块后中断。"
            f"不完整部分：`{incomplete}`。"
            f"请继续完成代码，保持语义连贯。"
        )
    
    elif boundary.boundary_type == BoundaryType.SENTENCE:
        return (
            f"上述内容在完整的句子后中断。"
            f"不完整部分："{incomplete}"。"
            f"请继续完成剩余内容。"
        )
    
    else:
        return (
            f"[上一个模型因网络问题中断，已生成完整内容如上。"
            f"不完整部分：{incomplete}。"
            f"请从上述内容继续完成剩余部分，保持语义连贯。]"
        )
```

---

## 📊 验收标准

### 功能测试
- ✅ 准确识别 Python/JavaScript/Java 代码块边界
- ✅ 准确识别 Markdown 代码块/标题边界
- ✅ 准确识别段落和句子边界
- ✅ 自动检测内容类型准确率 > 95%

### 质量测试
- ✅ 续写内容语义连贯性 > 95%
- ✅ 边界检测性能 < 10ms（对于 < 100KB 内容）
- ✅ 无误判导致的内容丢失

### 集成测试
- ✅ 与 Phase 1 无缝集成
- ✅ 不影响现有功能
- ✅ 所有现有测试通过

---

## ⏱️ 实施时间表

| 步骤 | 预计时间 | 交付物 |
|------|---------|--------|
| Step 2.1: 数据结构 | 30 分钟 | BoundaryResult, ContentType, BoundaryType |
| Step 2.2: 代码边界检测 | 1 小时 | CodeBoundaryDetector + 10 个测试 |
| Step 2.3: Markdown 检测 | 45 分钟 | MarkdownBoundaryDetector + 8 个测试 |
| Step 2.4: 文本边界检测 | 30 分钟 | TextBoundaryDetector + 6 个测试 |
| Step 2.5: 主检测器 | 45 分钟 | BoundaryDetector + 自动类型检测 |
| Step 2.6: 集成到 base.py | 30 分钟 | 修改续写逻辑 + 智能提示 |
| Step 2.7: 完整测试 | 30 分钟 | 端到端测试 + 边界探索 |
| **总计** | **4 小时** | **完整的语义边界检测系统** |

---

## 🚀 开始实施

准备好了吗？让我开始从 Step 2.1 实施！
