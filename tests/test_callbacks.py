"""
Engine Callback 测试。
"""

from __future__ import annotations

from omniagent.engine.callbacks import ConsoleCallback, EngineCallback, SilentCallback


class TestSilentCallback:
    """SilentCallback 事件记录测试。"""

    def test_records_think(self):
        cb = SilentCallback()
        cb.on_think("思考中...")
        assert cb.events == [("think", "思考中...")]

    def test_records_act(self):
        cb = SilentCallback()
        cb.on_act("write_file", {"file_path": "a.py", "content": "x"})
        assert cb.events == [("act", ("write_file", {"file_path": "a.py", "content": "x"}))]

    def test_records_observe(self):
        cb = SilentCallback()
        cb.on_observe("文件已创建")
        assert cb.events == [("observe", "文件已创建")]

    def test_records_step(self):
        cb = SilentCallback()
        cb.on_step(1, 3, "创建文件")
        assert cb.events == [("step", (1, 3, "创建文件"))]

    def test_records_step_done(self):
        cb = SilentCallback()
        cb.on_step_done(1, True, "成功")
        assert cb.events == [("step_done", (1, True, "成功"))]

    def test_records_review(self):
        cb = SilentCallback()
        cb.on_review(8, True, "质量不错")
        assert cb.events == [("review", (8, True, "质量不错"))]

    def test_records_error(self):
        cb = SilentCallback()
        cb.on_error("连接失败")
        assert cb.events == [("error", "连接失败")]

    def test_records_warning(self):
        cb = SilentCallback()
        cb.on_warning("磁盘空间不足")
        assert cb.events == [("warning", "磁盘空间不足")]

    def test_records_finish(self):
        cb = SilentCallback()
        cb.on_finish("最终结果")
        assert cb.events == [("finish", "最终结果")]

    def test_multiple_events(self):
        cb = SilentCallback()
        cb.on_step(1, 2, "任务A")
        cb.on_act("write_file", {"file_path": "a.py"})
        cb.on_observe("成功")
        cb.on_step_done(1, True, "完成")
        assert len(cb.events) == 4


class TestEngineCallback:
    """EngineCallback 基类默认空实现测试。"""

    def test_default_noop(self):
        cb = EngineCallback()
        # 不应抛出异常
        cb.on_think("test")
        cb.on_act("test", {})
        cb.on_observe("test")
        cb.on_step(1, 1, "test")
        cb.on_step_done(1, True, "test")
        cb.on_review(5, False, "test")
        cb.on_error("test")
        cb.on_warning("test")
        cb.on_finish("test")


class TestConsoleCallback:
    """ConsoleCallback 输出测试。"""

    def test_verbose_think(self, capsys):
        cb = ConsoleCallback(verbose=True)
        cb.on_think("这是一个思考过程")
        captured = capsys.readouterr()
        assert "🤔" in captured.out
        assert "思考过程" in captured.out

    def test_non_verbose_think(self, capsys):
        cb = ConsoleCallback(verbose=False)
        cb.on_think("这是一个思考过程")
        captured = capsys.readouterr()
        assert captured.out == ""  # 非 verbose 不输出 think

    def test_act_always_shown(self, capsys):
        cb = ConsoleCallback(verbose=False)
        cb.on_act("write_file", {"file_path": "a.py"})
        captured = capsys.readouterr()
        assert "🔧" in captured.out
        assert "write_file" in captured.out

    def test_observe_verbose(self, capsys):
        cb = ConsoleCallback(verbose=True)
        cb.on_observe("文件已创建")
        captured = capsys.readouterr()
        assert "👀" in captured.out

    def test_observe_non_verbose(self, capsys):
        cb = ConsoleCallback(verbose=False)
        cb.on_observe("文件已创建")
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_step_shown(self, capsys):
        cb = ConsoleCallback()
        cb.on_step(1, 3, "创建文件")
        captured = capsys.readouterr()
        assert "📋" in captured.out
        assert "1/3" in captured.out

    def test_review_shown(self, capsys):
        cb = ConsoleCallback()
        cb.on_review(8, True, "质量好")
        captured = capsys.readouterr()
        assert "🔍" in captured.out
        assert "8/10" in captured.out

    def test_error_shown(self, capsys):
        cb = ConsoleCallback()
        cb.on_error("出错了")
        captured = capsys.readouterr()
        assert "❌" in captured.out
