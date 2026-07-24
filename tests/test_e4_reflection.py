"""
P2-E4 ReflectionEngine 增强 + §8.23 缺陷修复测试。

覆盖：
- §8.23.11 / E4：独立 reviewer_model_priority（执行者与审查者不同模型）。
- §8.23.4/10 / E4：版本回退——返回历史最佳（按 score）输出，max_rounds 耗尽
  且未通过时标注；越改越差时返回最佳而非最后一轮。
- §8.23.3：以 score 为准判通过（pass:false+score:9 不再被否决）。
- §8.23.5：feedback 为空时强制默认，避免空转。
- §8.23.8：_execute 失败兜底（不冒泡炸掉整个 reflection）。
"""

from __future__ import annotations



from xenon.engine.context import AgentContext
from xenon.engine.reflection_engine import ReflectionEngine


class _RecordingCallback:
    def __init__(self):
        self.reviews: list[tuple[int, bool, str]] = []
        self.warnings: list[str] = []
        self.finishes: list[str] = []
        self.errors: list[str] = []

    def on_review(self, score, passed, feedback):
        self.reviews.append((score, passed, feedback))

    def on_warning(self, msg):
        self.warnings.append(msg)

    def on_finish(self, output):
        self.finishes.append(output)

    def on_error(self, msg):
        self.errors.append(msg)

    # 其它回调 no-op
    def __getattr__(self, name):
        return lambda *a, **k: None


def _make_engine(monkeypatch, *, reviewer_model_priority=None, max_rounds=3, pass_threshold=7):
    cb = _RecordingCallback()
    eng = ReflectionEngine(
        ["executor/m"],
        max_rounds=max_rounds,
        pass_threshold=pass_threshold,
        callback=cb,
        reviewer_model_priority=reviewer_model_priority,
    )
    return eng, cb


# ── §8.23.11：独立 reviewer_model_priority ─────────────────


class TestReviewerModelPriority:
    def test_reviewer_uses_independent_model_list(self, monkeypatch):
        """_review 应以 reviewer_model_priority 调 _call_llm（走真实 _call_llm）。"""
        import xenon.engine.base as base_mod
        eng, _ = _make_engine(monkeypatch, reviewer_model_priority=["reviewer/m", "fallback/m"])
        seen_models: list[str] = []

        def fake_chat(model_id, messages, **k):
            seen_models.append(model_id)
            if "执行者输出" in messages[-1]["content"]:
                # review：高分通过
                return '{"pass": true, "score": 9, "feedback": "ok", "issues": []}'
            return "OUTPUT"

        monkeypatch.setattr(base_mod, "chat_completion", fake_chat)
        out = eng.run("做 X", AgentContext())
        assert out == "OUTPUT"  # execute 输出，review 9 分通过
        # execute 用 executor 模型（self.model_priority）
        assert seen_models[0] == "executor/m"
        # review 用 reviewer 模型（reviewer_model_priority 的首位）
        assert seen_models[-1] == "reviewer/m"

    def test_default_reviewer_equals_executor_models(self, monkeypatch):
        eng, _ = _make_engine(monkeypatch)  # 不传 reviewer_model_priority
        assert eng.reviewer_model_priority == ["executor/m"]


# ── §8.23.3：以 score 为准 ─────────────────────────────────


class TestPassScoreConsistency:
    def test_high_score_passes_even_if_pass_false(self, monkeypatch):
        """{pass:false, score:9} 应通过（旧逻辑会误杀）。"""
        eng, cb = _make_engine(monkeypatch, pass_threshold=7, max_rounds=2)
        eng._call_llm = lambda messages, max_tokens=None, **k: (
            "OUTPUT"
            if "执行者输出" not in messages[-1]["content"]
            else '{"pass": false, "score": 9, "feedback": "x", "issues": []}'
        )
        out = eng.run("做 X", AgentContext())
        assert out == "OUTPUT"  # 9 分通过，未进入修正
        assert cb.reviews[-1][1] is True  # passed=True


# ── §8.23.4/10：版本回退 ───────────────────────────────────


class TestVersionRollback:
    def test_returns_best_version_when_max_rounds_exhausted(self, monkeypatch):
        """越改越差：返回最佳（最高分）版本而非最后一轮。"""
        eng, cb = _make_engine(monkeypatch, max_rounds=3, pass_threshold=8)
        outputs = ["good_v1", "bad_v2", "worse_v3"]
        scores = [6, 3, 2]
        state = {"exec_i": 0, "last_idx": -1}

        def fake_call_llm(messages, max_tokens=None, **k):
            is_review = "执行者输出" in messages[-1]["content"]
            if not is_review:
                idx = state["exec_i"]
                state["exec_i"] += 1
                state["last_idx"] = idx
                return outputs[idx]
            # review 评最近一次输出的分数，均不通过
            return f'{{"pass": false, "score": {scores[state["last_idx"]]}, "feedback": "改", "issues": []}}'

        eng._call_llm = fake_call_llm
        out = eng.run("做 X", AgentContext())
        # 最佳是 v1（score 6）
        assert "good_v1" in out
        assert "worse_v3" not in out  # 不是最后一轮
        assert "达到最大修正轮次" in out  # 标注未通过
        assert any("最高评分 6" in w for w in cb.warnings)

    def test_max_rounds_marker_in_output(self, monkeypatch):
        eng, cb = _make_engine(monkeypatch, max_rounds=1, pass_threshold=9)
        eng._call_llm = lambda messages, max_tokens=None, **k: (
            "OUT"
            if "执行者输出" not in messages[-1]["content"]
            else '{"pass": false, "score": 5, "feedback": "f", "issues": []}'
        )
        out = eng.run("做 X", AgentContext())
        assert "⚠️ 达到最大修正轮次" in out
        assert "OUT" in out


# ── §8.23.5：feedback 空强制默认 ───────────────────────────


class TestFeedbackEmpty:
    def test_empty_feedback_uses_default(self, monkeypatch):
        """review 返回空 feedback 时，下一轮 execute 收到默认反馈而非空。"""
        eng, _ = _make_engine(monkeypatch, max_rounds=2, pass_threshold=9)
        seen_feedback: list[str] = []

        def fake_call_llm(messages, max_tokens=None, **k):
            last = messages[-1]["content"]
            if "执行者输出" in last:
                # review：空 feedback
                return '{"pass": false, "score": 5, "feedback": "", "issues": []}'
            # execute：记录收到的反馈
            if "审查反馈" in last:
                seen_feedback.append(last)
            return "OUT"

        eng._call_llm = fake_call_llm  # type: ignore[assignment]
        eng.run("做 X", AgentContext())
        # 第二轮 execute 的消息应含默认反馈"请改进输出质量"
        assert seen_feedback, "第二轮 execute 应被调用"
        assert "请改进输出质量" in seen_feedback[-1]


# ── §8.23.8：_execute 失败兜底 ─────────────────────────────


class TestExecuteExceptionFallback:
    def test_execute_failure_returns_best_or_empty(self, monkeypatch):
        """_execute 抛异常时，reflection 不崩溃，返回已生成的最佳输出。"""
        eng, cb = _make_engine(monkeypatch, max_rounds=3, pass_threshold=8)
        state = {"i": 0}

        def fake_call_llm(messages, max_tokens=None, **k):
            is_review = "执行者输出" in messages[-1]["content"]
            if is_review:
                return '{"pass": false, "score": 6, "feedback": "f", "issues": []}'
            i = state["i"]
            state["i"] += 1
            if i == 0:
                return "FIRST_OUTPUT"
            raise RuntimeError("LLM 挂了")

        eng._call_llm = fake_call_llm
        out = eng.run("做 X", AgentContext())
        # 第二轮 execute 失败 → 返回第一轮的最佳（FIRST_OUTPUT，score 6）
        assert "FIRST_OUTPUT" in out
        assert any("执行阶段失败" in w for w in cb.warnings)

    def test_execute_failure_first_round_returns_empty(self, monkeypatch):
        eng, cb = _make_engine(monkeypatch, max_rounds=2, pass_threshold=8)

        def fake_call_llm(messages, max_tokens=None, **k):
            if "执行者输出" in messages[-1]["content"]:
                return '{"pass": false, "score": 5, "feedback": "f", "issues": []}'
            raise RuntimeError("立即挂")

        eng._call_llm = fake_call_llm
        out = eng.run("做 X", AgentContext())
        # 第一轮就失败，无最佳版本，返回空 + 警告
        assert out == ""
        assert any("执行阶段失败" in w for w in cb.warnings)


# ── 通过路径回归 ──────────────────────────────────────────


class TestPassPathRegression:
    def test_first_round_pass_returns_immediately(self, monkeypatch):
        eng, cb = _make_engine(monkeypatch, max_rounds=3, pass_threshold=7)
        eng._call_llm = lambda messages, max_tokens=None, **k: (
            "DONE"
            if "执行者输出" not in messages[-1]["content"]
            else '{"pass": true, "score": 9, "feedback": "好", "issues": []}'
        )
        out = eng.run("做 X", AgentContext())
        assert out == "DONE"
        assert cb.finishes == ["DONE"]
        assert cb.reviews[-1] == (9, True, "好")
