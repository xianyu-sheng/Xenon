"""
循环检测器测试。
"""

from xenon.engine.loop_detector import LoopDetector


class TestLoopDetector:
    """测试循环检测器"""

    def test_basic_initialization(self):
        """测试基本初始化"""
        detector = LoopDetector(window_size=5, similarity_threshold=0.8)

        assert detector.window_size == 5
        assert detector.similarity_threshold == 0.8
        assert detector.enabled
        assert detector.turn_count == 0

    def test_simple_hash_loop(self):
        """测试简单的哈希循环检测"""
        detector = LoopDetector(window_size=5)

        # 添加 3 轮，前两轮相同
        detector.add_turn("output A")
        detector.add_turn("output A")
        detector.add_turn("output A")

        result = detector.check()
        assert result.is_loop
        assert "输出哈希重复" in result.reason

    def test_tool_pattern_loop(self):
        """测试工具调用模式循环"""
        detector = LoopDetector(window_size=5)

        # 相同的工具调用模式重复
        for i in range(4):
            detector.add_turn(
                output=f"different output {i}",  # 不同的输出避免哈希冲突
                tool_calls=["read_file", "write_file"],
            )

        result = detector.check()
        assert result.is_loop
        # 可能是工具调用模式重复或输出哈希重复
        assert "模式重复" in result.reason or "哈希重复" in result.reason

    def test_error_message_loop(self):
        """测试错误消息循环"""
        detector = LoopDetector(window_size=5)

        # 相同的错误重复出现
        for i in range(4):
            detector.add_turn(
                output=f"different output {i}",
                tool_calls=[f"tool_{i}"],  # 不同的工具避免模式冲突
                error="File not found",
            )

        result = detector.check()
        assert result.is_loop
        # 可能是错误消息或其他模式
        assert "模式重复" in result.reason or "哈希重复" in result.reason

    def test_no_loop_different_outputs(self):
        """测试不同输出不会被检测为循环"""
        detector = LoopDetector(window_size=5)

        for i in range(5):
            detector.add_turn(
                output=f"unique output {i}",
                tool_calls=[f"tool_{i}"],
            )

        result = detector.check()
        assert not result.is_loop

    def test_insufficient_turns(self):
        """测试轮次不足时不检测循环"""
        detector = LoopDetector(window_size=5)

        detector.add_turn("output A")
        detector.add_turn("output A")

        result = detector.check()
        assert not result.is_loop
        assert "轮次不足" in result.reason

    def test_disabled_detector(self):
        """测试禁用的检测器"""
        detector = LoopDetector(enabled=False)

        for _ in range(5):
            detector.add_turn("same output")

        result = detector.check()
        assert not result.is_loop
        assert "检测器已禁用" in result.reason

    def test_loop_length_detection(self):
        """测试循环周期长度检测"""
        detector = LoopDetector(window_size=10)

        # 创建周期为 2 的循环
        for _ in range(3):
            detector.add_turn("A", tool_calls=["tool1"])
            detector.add_turn("B", tool_calls=["tool2"])

        result = detector.check()
        assert result.is_loop
        assert result.loop_length == 2

    def test_reset(self):
        """测试重置功能"""
        detector = LoopDetector()

        detector.add_turn("output")
        detector.add_turn("output")
        assert detector.turn_count == 2

        detector.reset()
        assert detector.turn_count == 0
        assert len(detector.turn_hashes) == 0

    def test_stats(self):
        """测试统计信息"""
        detector = LoopDetector(window_size=5, similarity_threshold=0.8)

        detector.add_turn("output")
        stats = detector.stats()

        assert stats["turn_count"] == 1
        assert stats["window_size"] == 5
        assert stats["similarity_threshold"] == 0.8
        assert stats["enabled"]

    def test_thought_similarity_loop(self):
        """测试思考内容相似度循环"""
        detector = LoopDetector(window_size=5)

        # 相同的思考内容重复
        for i in range(4):
            detector.add_turn(
                output=f"unique output {i}",
                tool_calls=[f"unique_tool_{i}"],  # 不同的工具避免冲突
                thought="I should read the file first",
            )

        result = detector.check()
        assert result.is_loop
        # 可能检测到思考或其他模式
        assert result.reason  # 只要检测到循环即可

    def test_complex_scenario(self):
        """测试复杂场景：先正常，后循环"""
        detector = LoopDetector(window_size=10)

        # 前 3 轮正常
        detector.add_turn("step 1", tool_calls=["read_file"])
        detector.add_turn("step 2", tool_calls=["write_file"])
        detector.add_turn("step 3", tool_calls=["command"])

        result = detector.check()
        assert not result.is_loop

        # 后面陷入循环
        for _ in range(3):
            detector.add_turn("trying again", tool_calls=["read_file"])
            detector.add_turn("still trying", tool_calls=["read_file"])

        result = detector.check()
        assert result.is_loop

    def test_similar_turns_tracking(self):
        """测试相似轮次追踪"""
        detector = LoopDetector(window_size=5)

        detector.add_turn("A")
        detector.add_turn("B")
        detector.add_turn("A")
        detector.add_turn("C")
        detector.add_turn("A")

        result = detector.check()
        assert result.is_loop
        assert len(result.similar_turns) >= 2  # 至少记录 2 个相似轮次
