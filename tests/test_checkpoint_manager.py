"""
Phase 3 单元测试 - 检查点管理器。

测试 Checkpoint 和 CheckpointManager 的各种功能。
"""

import time

from xenon.engine.checkpoint_manager import Checkpoint, CheckpointManager


class TestCheckpoint:
    """测试 Checkpoint 数据结构"""

    def test_basic_creation(self):
        """测试基本创建"""
        cp = Checkpoint(
            content="test content",
            tokens=100,
            timestamp=time.time(),
            sequence=0,
        )

        assert cp.content == "test content"
        assert cp.tokens == 100
        assert cp.sequence == 0
        assert len(cp) == 12  # "test content" 长度

    def test_age(self):
        """测试检查点年龄"""
        cp = Checkpoint(
            content="test",
            tokens=10,
            timestamp=time.time() - 5.0,
            sequence=0,
        )

        age = cp.age()
        assert 4.9 <= age <= 5.1  # 允许误差

    def test_to_dict(self):
        """测试转换为字典"""
        cp = Checkpoint(
            content="test",
            tokens=50,
            timestamp=time.time(),
            sequence=1,
            boundary_info={"type": "sentence"},
            metadata={"model": "test-model"},
        )

        data = cp.to_dict()
        assert data["content_length"] == 4
        assert data["tokens"] == 50
        assert data["sequence"] == 1
        assert data["boundary_info"]["type"] == "sentence"
        assert data["metadata"]["model"] == "test-model"


class TestCheckpointManager:
    """测试 CheckpointManager"""

    def test_basic_initialization(self):
        """测试基本初始化"""
        manager = CheckpointManager(interval_tokens=500, max_checkpoints=5)

        assert manager.interval == 500
        assert manager.max_checkpoints == 5
        assert manager.enabled
        assert len(manager) == 0
        assert not manager  # 空时为 False

    def test_should_save_first_checkpoint(self):
        """测试第一个检查点的保存时机"""
        manager = CheckpointManager(interval_tokens=500)

        assert not manager.should_save(100)
        assert not manager.should_save(499)
        assert manager.should_save(500)
        assert manager.should_save(600)

    def test_should_save_subsequent_checkpoints(self):
        """测试后续检查点的保存时机"""
        manager = CheckpointManager(interval_tokens=500)

        # 保存第一个检查点（500 tokens）
        manager.save_checkpoint("content 1", 500)

        # 第二个检查点应该在 1000 tokens 时保存
        assert not manager.should_save(900)
        assert not manager.should_save(999)
        assert manager.should_save(1000)

        # 保存第二个检查点
        manager.save_checkpoint("content 2", 1000)

        # 第三个检查点应该在 1500 tokens 时保存
        assert not manager.should_save(1400)
        assert manager.should_save(1500)

    def test_save_checkpoint(self):
        """测试保存检查点"""
        manager = CheckpointManager()

        cp1 = manager.save_checkpoint("content 1", 500)
        assert cp1.sequence == 0
        assert cp1.tokens == 500
        assert len(manager) == 1

        cp2 = manager.save_checkpoint("content 2", 1000)
        assert cp2.sequence == 1
        assert cp2.tokens == 1000
        assert len(manager) == 2

    def test_save_with_metadata(self):
        """测试保存带元数据的检查点"""
        manager = CheckpointManager()

        cp = manager.save_checkpoint(
            "content",
            500,
            boundary_info={"type": "paragraph"},
            metadata={"model": "test-model"},
        )

        assert cp.boundary_info["type"] == "paragraph"
        assert cp.metadata["model"] == "test-model"

    def test_get_last_checkpoint(self):
        """测试获取最后一个检查点"""
        manager = CheckpointManager()

        # 空时返回 None
        assert manager.get_last_checkpoint() is None

        cp1 = manager.save_checkpoint("content 1", 500)
        assert manager.get_last_checkpoint() == cp1

        cp2 = manager.save_checkpoint("content 2", 1000)
        assert manager.get_last_checkpoint() == cp2

    def test_max_checkpoints_limit(self):
        """测试检查点数量限制"""
        manager = CheckpointManager(interval_tokens=100, max_checkpoints=3)

        # 保存 5 个检查点
        cp1 = manager.save_checkpoint("c1", 100)
        cp2 = manager.save_checkpoint("c2", 200)
        cp3 = manager.save_checkpoint("c3", 300)
        cp4 = manager.save_checkpoint("c4", 400)
        cp5 = manager.save_checkpoint("c5", 500)

        # 应该只保留最近的 3 个
        assert len(manager) == 3
        assert cp1 not in manager.checkpoints
        assert cp2 not in manager.checkpoints
        assert cp3 in manager.checkpoints
        assert cp4 in manager.checkpoints
        assert cp5 in manager.checkpoints

    def test_get_checkpoint_at_tokens(self):
        """测试根据 tokens 获取检查点"""
        manager = CheckpointManager()

        cp1 = manager.save_checkpoint("c1", 500)
        cp2 = manager.save_checkpoint("c2", 1000)
        cp3 = manager.save_checkpoint("c3", 1500)

        # 在各个范围内获取
        assert manager.get_checkpoint_at_tokens(400) is None
        assert manager.get_checkpoint_at_tokens(500) == cp1
        assert manager.get_checkpoint_at_tokens(700) == cp1
        assert manager.get_checkpoint_at_tokens(1000) == cp2
        assert manager.get_checkpoint_at_tokens(1200) == cp2
        assert manager.get_checkpoint_at_tokens(1500) == cp3
        assert manager.get_checkpoint_at_tokens(2000) == cp3

    def test_get_checkpoint_by_sequence(self):
        """测试根据序号获取检查点"""
        manager = CheckpointManager()

        cp0 = manager.save_checkpoint("c0", 100)
        cp1 = manager.save_checkpoint("c1", 200)

        assert manager.get_checkpoint_by_sequence(0) == cp0
        assert manager.get_checkpoint_by_sequence(1) == cp1
        assert manager.get_checkpoint_by_sequence(2) is None

    def test_clear(self):
        """测试清除检查点"""
        manager = CheckpointManager()

        manager.save_checkpoint("c1", 100)
        manager.save_checkpoint("c2", 200)
        manager.save_checkpoint("c3", 300)

        assert len(manager) == 3

        count = manager.clear()
        assert count == 3
        assert len(manager) == 0
        assert manager.get_last_checkpoint() is None

        # 序号应该重置
        cp = manager.save_checkpoint("c4", 100)
        assert cp.sequence == 0

    def test_stats(self):
        """测试统计信息"""
        manager = CheckpointManager(interval_tokens=500, max_checkpoints=5)

        # 空时的统计
        stats = manager.stats()
        assert stats["enabled"]
        assert stats["total_checkpoints"] == 0
        assert stats["interval_tokens"] == 500

        # 保存一些检查点
        manager.save_checkpoint("c1", 500)
        manager.save_checkpoint("c2", 1000)
        manager.save_checkpoint("c3", 1500)

        stats = manager.stats()
        assert stats["total_checkpoints"] == 3
        assert stats["oldest_sequence"] == 0
        assert stats["newest_sequence"] == 2
        assert stats["total_tokens"] == 1500
        assert len(stats["checkpoints"]) == 3

    def test_disabled_manager(self):
        """测试禁用的检查点管理器"""
        manager = CheckpointManager(enabled=False)

        # should_save 应该总是返回 False
        assert not manager.should_save(500)
        assert not manager.should_save(1000)

        # save_checkpoint 应该返回检查点但不保存
        cp = manager.save_checkpoint("content", 500)
        assert cp.sequence == -1  # 禁用标记
        assert len(manager) == 0  # 不保存

    def test_bool_and_len(self):
        """测试布尔值和长度"""
        manager = CheckpointManager()

        assert len(manager) == 0
        assert not manager  # 空时为 False

        manager.save_checkpoint("c1", 100)
        assert len(manager) == 1
        assert manager  # 非空时为 True

    def test_repr(self):
        """测试字符串表示"""
        manager = CheckpointManager(interval_tokens=500, max_checkpoints=3)
        manager.save_checkpoint("c1", 500)

        repr_str = repr(manager)
        assert "CheckpointManager" in repr_str
        assert "checkpoints=1" in repr_str
        assert "interval=500" in repr_str
        assert "max=3" in repr_str


class TestCheckpointRecovery:
    """测试检查点恢复场景"""

    def test_recovery_from_middle_checkpoint(self):
        """测试从中间检查点恢复"""
        manager = CheckpointManager(interval_tokens=500)

        # 模拟生成 1700 tokens 后中断
        manager.save_checkpoint("content at 500", 500)
        manager.save_checkpoint("content at 1000", 1000)
        manager.save_checkpoint("content at 1500", 1500)

        # 中断发生在 1700 tokens
        # 应该恢复到 1500 tokens 的检查点
        recovery_checkpoint = manager.get_checkpoint_at_tokens(1700)

        assert recovery_checkpoint is not None
        assert recovery_checkpoint.tokens == 1500
        assert recovery_checkpoint.content == "content at 1500"

    def test_recovery_without_checkpoint(self):
        """测试没有检查点时的恢复"""
        manager = CheckpointManager(interval_tokens=500)

        # 在 300 tokens 时中断（还没有检查点）
        recovery_checkpoint = manager.get_checkpoint_at_tokens(300)
        assert recovery_checkpoint is None

        # 应该 fallback 到原始的部分内容处理

    def test_recovery_with_one_checkpoint(self):
        """测试只有一个检查点时的恢复"""
        manager = CheckpointManager(interval_tokens=500)

        manager.save_checkpoint("content at 500", 500)

        # 在 700 tokens 时中断
        recovery_checkpoint = manager.get_checkpoint_at_tokens(700)
        assert recovery_checkpoint.tokens == 500

    def test_multiple_recovery_attempts(self):
        """测试多次恢复尝试"""
        manager = CheckpointManager(interval_tokens=500, max_checkpoints=3)

        # 保存检查点
        manager.save_checkpoint("c1", 500)
        manager.save_checkpoint("c2", 1000)
        manager.save_checkpoint("c3", 1500)

        # 第一次恢复
        cp1 = manager.get_last_checkpoint()
        assert cp1.tokens == 1500

        # 清除后再次保存（模拟重试）
        manager.clear()
        manager.save_checkpoint("c4", 500)
        cp2 = manager.get_last_checkpoint()
        assert cp2.tokens == 500
        assert cp2.sequence == 0  # 序号重置


class TestCheckpointWithBoundary:
    """测试检查点与语义边界结合"""

    def test_checkpoint_with_boundary_info(self):
        """测试保存带语义边界信息的检查点"""
        manager = CheckpointManager()

        boundary_info = {
            "type": "function",
            "position": 450,
            "confidence": 0.9,
        }

        cp = manager.save_checkpoint(
            "def foo():\n    return 42\n",
            500,
            boundary_info=boundary_info,
        )

        assert cp.boundary_info["type"] == "function"
        assert cp.boundary_info["confidence"] == 0.9

    def test_recovery_uses_boundary(self):
        """测试恢复时使用边界信息"""
        manager = CheckpointManager()

        # 保存带边界信息的检查点
        manager.save_checkpoint(
            "def foo():\n    return 42\n",
            500,
            boundary_info={"type": "function", "complete": True},
        )

        recovery = manager.get_last_checkpoint()
        assert recovery.boundary_info["complete"]
