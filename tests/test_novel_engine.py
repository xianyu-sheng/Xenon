"""Tests for NovelEngine — 小说创作引擎。"""
from omniagent.engine.callbacks import EngineCallback
from omniagent.engine.context import AgentContext
from omniagent.engine.novel_engine import NovelEngine


# ── B9: 续写误判 ──────────────────────────────────────────
class _FakeNovelManager:
    """仅记录 _auto_update_context 写入的 operation。"""

    def __init__(self):
        self.calls = []

    def update_context(self, slug, operation, detail):
        self.calls.append((slug, operation, detail))


class _EmptyTracker:
    def has_executions(self):
        return False

    def get_history(self):
        return []


class TestAutoUpdateContextClassification:
    """B9: 多字关键词（续写/润色/扩写）必须优先于单字'写'。"""

    @staticmethod
    def _operation(user_input: str) -> str:
        # _auto_update_context 只依赖 self.manager 与 tracker，可绕过 __init__
        engine = NovelEngine.__new__(NovelEngine)
        engine.manager = _FakeNovelManager()
        engine._auto_update_context("slug", user_input, "ans", _EmptyTracker())
        return engine.manager.calls[-1][1]

    def test_xuxie_not_misjudged_as_chapter_write(self):
        """'续写' 不能被单字'写'误判为章节写作。"""
        assert self._operation("请续写第三章") == "续写"

    def test_kuoxie_not_misjudged(self):
        assert self._operation("帮我扩写这一段") == "扩写"

    def test_runse(self):
        assert self._operation("润色一下开头") == "润色修改"

    def test_dagang_takes_priority(self):
        assert self._operation("写一个大纲") == "大纲规划"

    def test_first_chapter_still_chapter_write(self):
        assert self._operation("请写第一章") == "章节写作"


# ── B10: final_answer 落盘门控 ────────────────────────────
class _FakeProject:
    slug = "test"
    title = "测试小说"

    def get_all_context(self):
        return ""


class _FakeNovelManagerForRun:
    def detect_novel(self, user_input):
        return _FakeProject()

    def list_novels(self):
        return []

    def update_context(self, slug, operation, detail):
        pass


class _RecordingCallback(EngineCallback):
    def __init__(self):
        self.warnings: list[str] = []
        self.finishes: list[str] = []

    def on_think(self, thought): pass
    def on_act(self, action, action_input): pass
    def on_observe(self, observation): pass
    def on_step(self, *a, **k): pass
    def on_step_done(self, *a, **k): pass
    def on_review(self, *a, **k): pass
    def on_error(self, error): pass

    def on_warning(self, warning):
        self.warnings.append(warning)

    def on_finish(self, result):
        self.finishes.append(result)


class TestFinalAnswerPersistenceGating:
    def test_blocks_final_answer_without_persistence(self):
        """B10: 未落盘时 final_answer 被门控，耗尽迭代而非提前返回正文。"""
        cb = _RecordingCallback()
        engine = NovelEngine(
            ["m1"],
            max_iterations=3,
            novel_manager=_FakeNovelManagerForRun(),
            callback=cb,
        )
        engine._call_llm = lambda messages: '{"thought":"t","final_answer":"正文内容"}'
        result = engine.run("写第一章", AgentContext())
        assert "达到最大迭代次数" in result
        assert any("保存到文件" in w or "write_file" in w for w in cb.warnings)

    def test_allows_final_answer_after_write(self):
        """B10: 调用过 write_file 后 final_answer 放行。"""
        cb = _RecordingCallback()
        engine = NovelEngine(
            ["m1"],
            max_iterations=5,
            novel_manager=_FakeNovelManagerForRun(),
            callback=cb,
        )
        state = {"i": 0}
        responses = [
            '{"thought":"t","final_answer":"正文"}',  # 1: 未落盘 → 纠偏继续
            '{"thought":"t","action":"write_file","action_input":{"file_path":"ch1.md","content":"正文"}}',
            '{"thought":"t","final_answer":"已完成"}',  # 3: 已落盘 → 放行
        ]

        def fake_llm(messages):
            i = state["i"]
            state["i"] += 1
            return responses[i] if i < len(responses) else '{"final_answer":"已完成"}'

        def fake_exec(action, action_input, ctx, tracker):
            tracker.record(action, action_input, True, "ok")
            return "ok"

        engine._call_llm = fake_llm
        engine._execute_tool = fake_exec
        result = engine.run("写第一章", AgentContext())
        assert result == "已完成"
        # 第 1 次 final_answer 被门控挡住，所以总共经历了 3 次 LLM 调用
        assert state["i"] == 3
