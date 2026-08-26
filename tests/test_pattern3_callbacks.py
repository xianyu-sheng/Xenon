"""
测试回调系统的三个关键修复：
1. 并行工具调用的观察结果正确关联（FIFO deque）
2. 错误自动检测（is_error 标记）
3. 孤立观察处理（无对应 action 的 observation）
"""

from __future__ import annotations

from xenon.engine.callbacks import ConsoleCallback, ThinkingPanel


class TestParallelActionsObservationMatching:
    """修复1: 并行工具调用时观察结果正确匹配到对应的 action"""

    def test_parallel_actions_fifo_order(self):
        """多个并行工具调用，观察结果按 FIFO 顺序匹配"""
        panel = ThinkingPanel()

        panel.add_thought("需要同时检查日期和天气")
        panel.add_action("get_date", {})
        panel.add_action("get_weather", {"city": "苏州"})
        panel.add_action("get_temperature", {"city": "北京"})

        # 观察结果按发出顺序返回
        panel.add_observation("2026-08-26")
        panel.add_observation("晴天，31°C")
        panel.add_observation("多云，28°C")

        # 验证每个观察结果匹配到正确的工具
        assert len(panel.steps) == 3
        assert panel.steps[0].action == "get_date"
        assert panel.steps[0].observation == "2026-08-26"
        assert panel.steps[1].action == "get_weather"
        assert panel.steps[1].observation == "晴天，31°C"
        assert panel.steps[2].action == "get_temperature"
        assert panel.steps[2].observation == "多云，28°C"

    def test_parallel_actions_with_single_thought(self):
        """一次思考触发多个并行工具，思考只附着在第一个工具"""
        panel = ThinkingPanel()

        panel.add_thought("并行查询多个数据源")
        panel.add_action("query_db", {"table": "users"})
        panel.add_action("query_api", {"endpoint": "/stats"})

        panel.add_observation("查询到 100 条记录")
        panel.add_observation("API 返回 200 OK")

        # 思考只附着在第一个工具
        assert panel.steps[0].thought == "并行查询多个数据源"
        assert panel.steps[1].thought == ""

        # 每个工具都有自己的观察结果
        assert panel.steps[0].observation == "查询到 100 条记录"
        assert panel.steps[1].observation == "API 返回 200 OK"

    def test_mixed_sequential_and_parallel(self):
        """混合串行和并行工具调用"""
        panel = ThinkingPanel()

        # 第一轮：串行
        panel.add_thought("先读取配置")
        panel.add_action("read_file", {"path": "config.yaml"})
        panel.add_observation("配置加载成功")

        # 第二轮：并行
        panel.add_thought("同时创建两个文件")
        panel.add_action("write_file", {"path": "a.txt"})
        panel.add_action("write_file", {"path": "b.txt"})
        panel.add_observation("a.txt 创建成功")
        panel.add_observation("b.txt 创建成功")

        # 验证顺序
        assert len(panel.steps) == 3
        assert panel.steps[0].action == "read_file"
        assert panel.steps[1].action == "write_file"
        assert panel.steps[1].observation == "a.txt 创建成功"
        assert panel.steps[2].action == "write_file"
        assert panel.steps[2].observation == "b.txt 创建成功"


class TestAutoErrorDetection:
    """修复2: 自动检测观察结果中的错误并设置 is_error 标记"""

    def test_error_keywords_detection(self):
        """检测常见错误关键词"""
        panel = ThinkingPanel()

        test_cases = [
            ("错误：文件不存在", True),
            ("Error: Permission denied", True),
            ("操作失败", True),
            ("File not found", True),
            ("连接超时 timeout", True),
            ("403 Forbidden", True),
            ("429 Too Many Requests", True),
            ("401 Unauthorized", True),
            ("SecurityError: 路径越界", True),
            ("Fatal error occurred", True),
            ("无法连接到服务器", True),
            ("不能执行该操作", True),
        ]

        for i, (observation, should_be_error) in enumerate(test_cases):
            panel.add_action(f"tool_{i}", {})
            step = panel.add_observation(observation)
            assert step.is_error == should_be_error, f"失败: {observation}"

    def test_success_observations_not_marked_as_error(self):
        """成功的观察结果不应被标记为错误"""
        panel = ThinkingPanel()

        success_cases = [
            "文件创建成功",
            "操作完成",
            "返回结果：[1, 2, 3]",
            "查询成功，找到 5 条记录",
            "连接建立",
            "数据更新完成",
        ]

        for i, observation in enumerate(success_cases):
            panel.add_action(f"tool_{i}", {})
            step = panel.add_observation(observation)
            assert not step.is_error, f"误判为错误: {observation}"

    def test_mixed_error_and_success(self):
        """混合错误和成功的观察结果"""
        panel = ThinkingPanel()

        panel.add_action("read_file", {"path": "a.txt"})
        step1 = panel.add_observation("文件读取成功")

        panel.add_action("delete_file", {"path": "b.txt"})
        step2 = panel.add_observation("错误：文件不存在")

        panel.add_action("create_dir", {"path": "output"})
        step3 = panel.add_observation("目录创建成功")

        assert not step1.is_error
        assert step2.is_error
        assert not step3.is_error

    def test_case_insensitive_error_detection(self):
        """错误检测不区分大小写"""
        panel = ThinkingPanel()

        panel.add_action("tool_1", {})
        step1 = panel.add_observation("ERROR: something went wrong")

        panel.add_action("tool_2", {})
        step2 = panel.add_observation("Error: Something Went Wrong")

        panel.add_action("tool_3", {})
        step3 = panel.add_observation("error: something went wrong")

        assert step1.is_error
        assert step2.is_error
        assert step3.is_error


class TestOrphanObservationHandling:
    """修复3: 处理没有对应 action 的孤立观察结果"""

    def test_orphan_observation_creates_new_step(self):
        """没有对应 action 的观察结果应创建新步骤"""
        panel = ThinkingPanel()

        panel.add_action("tool_a", {})
        panel.add_observation("结果 A")

        # 引擎合成的观察（如预算拒绝），没有对应的 action
        panel.add_observation("预算不足，拒绝执行")

        # 验证创建了独立步骤
        assert len(panel.steps) == 2
        assert panel.steps[0].action == "tool_a"
        assert panel.steps[0].observation == "结果 A"
        assert panel.steps[1].action == ""  # 孤立观察没有 action
        assert panel.steps[1].observation == "预算不足，拒绝执行"

    def test_orphan_observation_with_pending_thought(self):
        """孤立观察可以附着待处理的思考"""
        panel = ThinkingPanel()

        panel.add_thought("准备执行工具")
        panel.add_action("tool_a", {})
        panel.add_observation("工具 A 完成")

        panel.add_thought("评估是否继续")
        # 引擎决定不调用工具，直接给出观察
        panel.add_observation("经评估，无需进一步操作")

        assert len(panel.steps) == 2
        assert panel.steps[0].thought == "准备执行工具"
        assert panel.steps[1].thought == "评估是否继续"
        assert panel.steps[1].action == ""
        assert panel.steps[1].observation == "经评估，无需进一步操作"

    def test_multiple_orphan_observations(self):
        """多个孤立观察应各自创建步骤"""
        panel = ThinkingPanel()

        panel.add_action("tool_1", {})
        panel.add_observation("结果 1")

        # 连续的孤立观察
        panel.add_observation("系统提示：内存使用率 80%")
        panel.add_observation("系统提示：磁盘空间充足")

        panel.add_action("tool_2", {})
        panel.add_observation("结果 2")

        assert len(panel.steps) == 4
        assert panel.steps[0].action == "tool_1"
        assert panel.steps[1].action == ""
        assert panel.steps[1].observation == "系统提示：内存使用率 80%"
        assert panel.steps[2].action == ""
        assert panel.steps[2].observation == "系统提示：磁盘空间充足"
        assert panel.steps[3].action == "tool_2"

    def test_orphan_observation_does_not_corrupt_other_steps(self):
        """孤立观察不会破坏已有步骤的完整性"""
        panel = ThinkingPanel()

        # 正常流程
        panel.add_thought("步骤 1")
        panel.add_action("tool_a", {"param": "value_a"})

        # 孤立观察插入
        panel.add_observation("意外的系统消息")

        # 继续正常流程
        panel.add_action("tool_b", {"param": "value_b"})
        panel.add_observation("工具 A 的结果")  # 这应该匹配 tool_a
        panel.add_observation("工具 B 的结果")  # 这应该匹配 tool_b

        assert len(panel.steps) == 3
        # 孤立观察消耗了第一个 action 的 observation 位置
        assert panel.steps[0].action == "tool_a"
        assert panel.steps[0].observation == "意外的系统消息"
        # 后续观察正确匹配
        assert panel.steps[1].action == "tool_b"
        assert panel.steps[1].observation == "工具 A 的结果"
        assert panel.steps[2].action == ""
        assert panel.steps[2].observation == "工具 B 的结果"


class TestConsoleCallbackIntegration:
    """集成测试：ConsoleCallback 正确使用 ThinkingPanel 的修复功能"""

    def test_console_callback_parallel_actions(self):
        """ConsoleCallback 正确处理并行工具调用"""
        cb = ConsoleCallback(verbose=False)

        cb.on_think("并行查询")
        cb.on_act("tool_1", {"arg": "a"})
        cb.on_act("tool_2", {"arg": "b"})
        cb.on_observe("观察 1")
        cb.on_observe("观察 2")

        panel = cb.get_thinking_panel()
        assert panel is not None
        assert panel.tool_call_count == 2
        assert panel.steps[0].observation == "观察 1"
        assert panel.steps[1].observation == "观察 2"

    def test_console_callback_auto_error_detection(self):
        """ConsoleCallback 自动检测错误"""
        cb = ConsoleCallback(verbose=False)

        cb.on_act("failing_tool", {})
        cb.on_observe("Error: operation failed")

        panel = cb.get_thinking_panel()
        assert panel is not None
        assert panel.steps[0].is_error

    def test_console_callback_orphan_observation(self):
        """ConsoleCallback 正确处理孤立观察"""
        cb = ConsoleCallback(verbose=False)

        cb.on_act("tool_a", {})
        cb.on_observe("结果 A")
        cb.on_observe("系统通知：预算警告")

        panel = cb.get_thinking_panel()
        assert panel is not None
        assert len(panel.steps) == 2
        assert panel.steps[1].action == ""
        assert "预算警告" in panel.steps[1].observation
