# Phase 3: 检查点快照机制 - 实施方案

## 📋 目标

实现周期性检查点保存机制，在流式生成过程中每 N tokens 保存一次快照，当网络中断时能从最近的检查点恢复，而不是从头开始。

---

## 🎯 核心问题

**Phase 1+2 的问题**：
- 只在网络中断的最后一刻保存内容
- 如果中断发生在生成早期，损失较小
- 如果中断发生在生成后期（如 80%），虽然能续写，但已经浪费了大量网络传输

**Phase 3 的解决方案**：
- 每 500 tokens 保存一次检查点
- 网络中断时立即从最近的检查点恢复
- 减少网络抖动造成的损失

---

## 🏗️ 架构设计

### 核心类

```python
@dataclass
class Checkpoint:
    """检查点数据结构"""
    content: str              # 到该点的全部内容
    tokens: int              # 已生成的 token 数
    timestamp: float         # 保存时间
    boundary_info: dict      # 语义边界信息
    sequence: int            # 检查点序号

class CheckpointManager:
    """检查点管理器"""
    
    def __init__(self, interval_tokens: int = 500):
        self.checkpoints: list[Checkpoint] = []
        self.interval = interval_tokens
        self.max_checkpoints = 5  # 最多保留 5 个检查点
    
    def should_save(self, accumulated_tokens: int) -> bool:
        """判断是否应该保存检查点"""
        
    def save_checkpoint(self, content: str, tokens: int) -> Checkpoint:
        """保存检查点（自动淘汰旧的）"""
        
    def get_last_checkpoint(self) -> Checkpoint | None:
        """获取最近的检查点"""
        
    def clear(self) -> None:
        """清除所有检查点"""
```

---

## 📝 详细实现

### Step 3.1: 创建检查点数据结构

**文件**: `xenon/engine/checkpoint_manager.py`

```python
@dataclass
class Checkpoint:
    """检查点快照"""
    content: str
    tokens: int
    timestamp: float
    boundary_info: dict
    sequence: int
    metadata: dict = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "content_length": len(self.content),
            "tokens": self.tokens,
            "timestamp": self.timestamp,
            "sequence": self.sequence,
            "boundary_info": self.boundary_info,
            "metadata": self.metadata,
        }

class CheckpointManager:
    """检查点管理器"""
    
    def __init__(self, interval_tokens: int = 500, max_checkpoints: int = 5):
        self.interval = interval_tokens
        self.max_checkpoints = max_checkpoints
        self.checkpoints: list[Checkpoint] = []
        self.next_sequence = 0
    
    def should_save(self, accumulated_tokens: int) -> bool:
        """判断是否应该保存检查点"""
        if not self.checkpoints:
            return accumulated_tokens >= self.interval
        
        last_checkpoint = self.checkpoints[-1]
        tokens_since_last = accumulated_tokens - last_checkpoint.tokens
        return tokens_since_last >= self.interval
    
    def save_checkpoint(
        self,
        content: str,
        tokens: int,
        boundary_info: dict | None = None,
    ) -> Checkpoint:
        """保存检查点"""
        checkpoint = Checkpoint(
            content=content,
            tokens=tokens,
            timestamp=time.time(),
            boundary_info=boundary_info or {},
            sequence=self.next_sequence,
        )
        
        self.checkpoints.append(checkpoint)
        self.next_sequence += 1
        
        # 淘汰旧检查点（保留最近的 N 个）
        if len(self.checkpoints) > self.max_checkpoints:
            self.checkpoints.pop(0)
        
        return checkpoint
    
    def get_last_checkpoint(self) -> Checkpoint | None:
        """获取最近的检查点"""
        return self.checkpoints[-1] if self.checkpoints else None
    
    def get_checkpoint_at_tokens(self, target_tokens: int) -> Checkpoint | None:
        """获取指定 token 数之前的最近检查点"""
        for checkpoint in reversed(self.checkpoints):
            if checkpoint.tokens <= target_tokens:
                return checkpoint
        return None
    
    def clear(self) -> None:
        """清除所有检查点"""
        self.checkpoints.clear()
        self.next_sequence = 0
    
    def stats(self) -> dict:
        """统计信息"""
        return {
            "total_checkpoints": len(self.checkpoints),
            "interval_tokens": self.interval,
            "max_checkpoints": self.max_checkpoints,
            "checkpoints": [cp.to_dict() for cp in self.checkpoints],
        }
```

---

### Step 3.2: 集成到流式输出

**修改**: `xenon/utils/llm_client.py`

当前 `llm_client.py` 中的流式函数（`_stream_openai_compat` 和 `_stream_anthropic`）需要支持检查点：

```python
def _stream_openai_compat(
    endpoint: ModelEndpoint,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    timeout: float,
    *,
    checkpoint_manager: CheckpointManager | None = None,
) -> Generator[str, None, tuple[str, str]]:
    """流式调用（支持检查点）"""
    
    accumulated_content = ""
    accumulated_tokens = 0
    
    for chunk in stream_response:
        delta = chunk.choices[0].delta.content
        if delta:
            accumulated_content += delta
            accumulated_tokens += estimate_tokens(delta)
            
            # Phase 3: 检查点保存
            if checkpoint_manager and checkpoint_manager.should_save(accumulated_tokens):
                checkpoint_manager.save_checkpoint(
                    accumulated_content,
                    accumulated_tokens
                )
            
            yield delta
    
    return accumulated_content, finish_reason
```

---

### Step 3.3: 在引擎层使用检查点

**修改**: `xenon/engine/base.py`

```python
class BaseEngine:
    def __init__(self, ...):
        self.checkpoint_manager = CheckpointManager(
            interval_tokens=500,
            max_checkpoints=5
        )
    
    def _call_llm_once(self, ...):
        # 在调用前清除旧检查点
        self.checkpoint_manager.clear()
        
        try:
            # 调用 LLM（会自动保存检查点）
            result = chat_completion(
                model_id,
                messages,
                checkpoint_manager=self.checkpoint_manager,
                **options
            )
            return result
            
        except PartialResponseError as e:
            # Phase 3: 从检查点恢复
            last_checkpoint = self.checkpoint_manager.get_last_checkpoint()
            
            if last_checkpoint:
                logger.info(
                    f"📦 从检查点恢复: {last_checkpoint.tokens} tokens, "
                    f"序号 {last_checkpoint.sequence}"
                )
                
                # 使用检查点内容而不是部分内容
                partial_content = last_checkpoint.content
            else:
                # 没有检查点，使用原始部分内容
                partial_content = e.partial.content
            
            # 后续语义边界检测和续写...
```

---

## 🧪 测试用例

### 检查点管理器测试

```python
def test_checkpoint_save_and_retrieve():
    """测试保存和检索检查点"""
    manager = CheckpointManager(interval_tokens=500, max_checkpoints=3)
    
    # 保存第一个检查点
    cp1 = manager.save_checkpoint("content 1", 500)
    assert cp1.sequence == 0
    assert manager.get_last_checkpoint() == cp1
    
    # 保存第二个检查点
    cp2 = manager.save_checkpoint("content 1 + 2", 1000)
    assert cp2.sequence == 1
    assert manager.get_last_checkpoint() == cp2
    
    # 保存超过最大数量
    cp3 = manager.save_checkpoint("content 1 + 2 + 3", 1500)
    cp4 = manager.save_checkpoint("content 1 + 2 + 3 + 4", 2000)
    
    # 应该只保留最近 3 个
    assert len(manager.checkpoints) == 3
    assert manager.checkpoints[0] == cp2  # cp1 被淘汰


def test_checkpoint_should_save():
    """测试检查点保存时机"""
    manager = CheckpointManager(interval_tokens=500)
    
    # 第一次：累积 500 tokens 时应该保存
    assert manager.should_save(500)
    assert not manager.should_save(499)
    
    manager.save_checkpoint("content", 500)
    
    # 第二次：距离上次 500 tokens 时应该保存
    assert not manager.should_save(900)
    assert manager.should_save(1000)


def test_checkpoint_recovery():
    """测试从检查点恢复"""
    manager = CheckpointManager(interval_tokens=500)
    
    # 保存多个检查点
    manager.save_checkpoint("content at 500", 500)
    manager.save_checkpoint("content at 1000", 1000)
    manager.save_checkpoint("content at 1500", 1500)
    
    # 模拟在 1700 tokens 时中断
    # 应该恢复到 1500 tokens 的检查点
    checkpoint = manager.get_checkpoint_at_tokens(1700)
    assert checkpoint.tokens == 1500
    assert checkpoint.content == "content at 1500"
```

---

## 📊 验收标准

### 功能测试
- ✅ 每 500 tokens 自动保存检查点
- ✅ 最多保留 5 个检查点（自动淘汰旧的）
- ✅ 网络中断时从最近检查点恢复
- ✅ 检查点与语义边界结合使用

### 性能测试
- ✅ 检查点保存开销 < 1ms
- ✅ 内存占用合理（每个检查点 < 10KB）
- ✅ 不影响流式输出性能

### 集成测试
- ✅ 与 Phase 1+2 无缝集成
- ✅ 所有现有测试通过

---

## ⏱️ 实施时间表

| 步骤 | 预计时间 | 交付物 |
|------|---------|--------|
| Step 3.1: 数据结构 | 30 分钟 | CheckpointManager + 基本测试 |
| Step 3.2: 流式集成 | 1 小时 | 修改 llm_client.py |
| Step 3.3: 引擎集成 | 45 分钟 | 修改 base.py + 恢复逻辑 |
| Step 3.4: 完整测试 | 45 分钟 | 单元测试 + 集成测试 |
| **总计** | **3 小时** | **完整的检查点快照系统** |

---

## 🚀 实际效果

### 场景 1: 早期中断

```
无检查点（Phase 1+2）:
模型 A 生成 200 tokens → 中断 → 保存 200 tokens
模型 B 续写 1800 tokens
总计: 2000 tokens ✅

有检查点（Phase 3）:
模型 A 生成 200 tokens → 中断 → 没有检查点
模型 B 续写 1800 tokens
总计: 2000 tokens ✅（效果相同）
```

### 场景 2: 中期中断

```
无检查点:
模型 A 生成 1200 tokens → 中断 → 保存 1200 tokens
模型 B 续写 800 tokens
总计: 2000 tokens ✅

有检查点:
模型 A 生成 1200 tokens（保存了 500、1000 检查点）→ 中断
从 1000 tokens 检查点恢复（语义边界）
模型 B 续写 1000 tokens
总计: 2000 tokens ✅（效果类似）
```

### 场景 3: 网络抖动

```
无检查点:
模型 A 生成 800 tokens → 抖动丢失 → 从头开始
模型 B 生成 2000 tokens
总计: 2800 tokens ❌（浪费 800）

有检查点:
模型 A 生成 800 tokens（保存了 500 检查点）→ 抖动
从 500 tokens 检查点恢复
模型 B 续写 1500 tokens
总计: 2000 tokens ✅（节省 800）
```

---

## 🎯 Phase 3 的核心价值

1. **网络抖动容错**: 频繁的小中断不会丢失大量内容
2. **长生成保护**: 长文本生成过程中的任何时刻中断都有保护
3. **内存开销小**: 只保留最近 5 个检查点
4. **透明集成**: 对上层调用者完全透明

---

准备好开始实施了吗？
