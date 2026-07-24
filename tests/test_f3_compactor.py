"""F3 验收：Compactor 升级（6 段结构化 + 三层策略 + 安全截断 + 头尾截断 + 持久化 + 备选模型重试）。"""
import xenon.utils.llm_client as llm
from xenon.repl.context_manager import ContextManager


def _add_rounds(cm, n, big_user=None):
    """添加 n 轮 user/assistant 对话；首轮 user 可指定大文本以撑高 token。"""
    for i in range(n):
        cm.add_user_message(big_user if (i == 0 and big_user) else f"问题{i}")
        cm.add_assistant_message(f"回答{i}")


SIX_SEG_RAW = (
    "【原始目标】实现一个压缩器\n"
    "【已完成步骤】已写好 compact 方法\n"
    "【关键约束】必须 6 段\n"
    "【当前文件状态】改了 context_manager.py\n"
    "【剩余待办】写测试\n"
    "【关键数据】阈值 0.6/0.85"
)


# ── 三层策略 ───────────────────────────────────────────────
class TestThreeTierStrategy:
    def test_tier1_skip_below_threshold(self):
        """<60% 且无手动摘要 → 跳过，不改写历史。"""
        cm = ContextManager(max_tokens=128000)
        _add_rounds(cm, 4)  # 8 条小消息，ratio 远 < 0.6
        n_before = len(cm.history)
        result = cm.compact()
        assert "无需压缩" in result
        assert len(cm.history) == n_before

    def test_tier2_llm_six_segment(self, monkeypatch):
        """60-85% → LLM 6 段压缩，history = [system 摘要] + recent。"""
        cm = ContextManager(max_tokens=10000)
        _add_rounds(cm, 4, big_user="x" * 14000)  # ~7000 tok / 10000 = 0.7
        assert 0.6 < cm.usage_ratio() < 0.85

        monkeypatch.setattr(llm, "chat_completion", lambda *a, **k: SIX_SEG_RAW)
        result = cm.compact(model_priority=["m1"])
        assert "【原始目标】" in result
        assert "【关键数据】" in result
        # 1 system 摘要 + recent(6) = 7
        assert len(cm.history) == 7
        assert cm.history[0].role == "system"
        assert "6 段摘要" in cm.history[0].content

    def test_tier3_crisis_no_llm(self, monkeypatch):
        """v0.5.0: 空间危急 (>95%) → Q3 用 _auto_summary 正则兜底，不调 LLM。"""
        # 使用极小的 max_tokens 使 ratio 超 95% 进入 critical
        cm = ContextManager(max_tokens=3000)
        _add_rounds(cm, 4, big_user="x" * 7000)  # ~3500/3000 > 0.95
        assert cm.usage_ratio() > 0.95

        def boom(*a, **k):
            raise AssertionError("危急状态下不应调用 LLM")

        monkeypatch.setattr(llm, "chat_completion", boom)
        result = cm.compact()
        # Q3 危急 → _auto_summary() 正则兜底，输出含"用户需求"等
        assert len(result) > 0
        assert "安全截断" not in result  # 不再是旧的安全截断
        # 压缩后消息数减少
        assert len(cm.history) < 8

    def test_manual_summary_forces_tier2_even_if_low(self, monkeypatch):
        """手动摘要 + 有 older → 即使 ratio 低也执行 Tier 2 写入。"""
        cm = ContextManager(max_tokens=128000)
        _add_rounds(cm, 4)
        n_before = len(cm.history)
        result = cm.compact(summary="手动六段摘要")
        assert result == "手动六段摘要"
        assert len(cm.history) == n_before - 2 + 1  # older(2) 被替换为 1 条 system


# ── 6 段解析 ───────────────────────────────────────────────
class TestParseSixSegments:
    def test_valid_reorders_to_canonical(self):
        cm = ContextManager()
        # 乱序输入
        raw = "【关键数据】id=1\n【原始目标】做X\n【已完成步骤】做Y"
        parsed = cm._parse_six_segments(raw)
        assert parsed is not None
        # 规范顺序：原始目标 在 关键数据 之前
        assert parsed.index("【原始目标】") < parsed.index("【关键数据】")
        # 缺失段补"无"
        assert "【剩余待办】无" in parsed

    def test_no_markers_returns_none(self):
        cm = ContextManager()
        assert cm._parse_six_segments("just plain text no markers") is None

    def test_missing_core_segments_returns_none(self):
        cm = ContextManager()
        # 只有非核心段
        assert cm._parse_six_segments("【关键约束】foo\n【关键数据】bar") is None

    def test_empty_returns_none(self):
        cm = ContextManager()
        assert cm._parse_six_segments("") is None
        assert cm._parse_six_segments("   ") is None


# ── 头尾截断 ───────────────────────────────────────────────
class TestHeadTailTruncate:
    def test_long_text_omits_middle(self):
        cm = ContextManager()
        text = "A" * 1000
        out = cm._head_tail_truncate(text)
        assert "省略中间" in out
        assert out.startswith("A")
        assert out.endswith("A")

    def test_short_text_unchanged(self):
        cm = ContextManager()
        text = "短文本"
        assert cm._head_tail_truncate(text) == text


# ── 安全截断 ───────────────────────────────────────────────
class TestSafeTruncation:
    def test_keeps_system_plus_recent(self):
        cm = ContextManager()
        cm.add_system_message("主提示词")
        for i in range(8):
            cm.add_user_message(f"u{i}")
        out = cm._safe_truncation()
        # 1 system + max(5, min(20, int(8*0.3)=2))=5 recent = 6
        assert len(out) == 6
        assert out[0].role == "system"
        assert out[1].content == "u3"  # 最近 5 条: u3..u7

    def test_clamps_to_max_20(self):
        cm = ContextManager()
        for i in range(30):
            cm.add_user_message(f"u{i}")
        out = cm._safe_truncation()
        # int(30*0.3)=9, max(5,min(20,9))=9
        assert len(out) == 9
        assert out[-1].content == "u29"

    def test_keeps_min_5(self):
        cm = ContextManager()
        for i in range(3):
            cm.add_user_message(f"u{i}")
        out = cm._safe_truncation()
        # 只有 3 条，keep=min(5,3)=3
        assert len(out) == 3


# ── 备选模型重试 + 兜底 ────────────────────────────────────
class TestFallbackRetry:
    def test_first_model_fails_second_succeeds(self, monkeypatch):
        cm = ContextManager(max_tokens=10000)
        _add_rounds(cm, 4, big_user="x" * 14000)
        calls = {"n": 0}

        def fake_chat(model_id, *a, **k):
            calls["n"] += 1
            if model_id == "m1":
                raise RuntimeError("m1 down")
            return SIX_SEG_RAW

        monkeypatch.setattr(llm, "chat_completion", fake_chat)
        result = cm.compact(model_priority=["m1", "m2"])
        assert "【原始目标】" in result
        assert calls["n"] == 2  # m1 失败后重试 m2

    def test_all_models_fail_falls_back_to_auto(self, monkeypatch):
        cm = ContextManager(max_tokens=10000)
        _add_rounds(cm, 4, big_user="x" * 14000)

        def fake_chat(model_id, *a, **k):
            raise RuntimeError("all down")

        monkeypatch.setattr(llm, "chat_completion", fake_chat)
        result = cm.compact(model_priority=["m1", "m2"])
        # _auto_summary 输出含"用户需求"或"涉及文件"等，且不含【】段标
        assert "【原始目标】" not in result
        assert len(result) > 0

    def test_parse_failure_triggers_next_model(self, monkeypatch):
        """LLM 返回非 6 段文本 → 解析失败 → 尝试下一个模型。"""
        cm = ContextManager(max_tokens=10000)
        _add_rounds(cm, 4, big_user="x" * 14000)
        calls = []

        def fake_chat(model_id, *a, **k):
            calls.append(model_id)
            if model_id == "m1":
                return "非结构化纯文本，没有段标记"
            return SIX_SEG_RAW

        monkeypatch.setattr(llm, "chat_completion", fake_chat)
        result = cm.compact(model_priority=["m1", "m2"])
        assert "【原始目标】" in result
        assert calls == ["m1", "m2"]


# ── 持久化 ─────────────────────────────────────────────────
class TestPersistMarkdown:
    def test_writes_markdown_snapshot(self, tmp_path, monkeypatch):
        cm = ContextManager(max_tokens=10000)
        cm.persist_dir = tmp_path
        cm.session_id = "sess-f3"
        _add_rounds(cm, 4, big_user="x" * 14000)
        monkeypatch.setattr(llm, "chat_completion", lambda *a, **k: SIX_SEG_RAW)

        cm.compact(model_priority=["m1"])
        files = list(tmp_path.glob("compact-*.md"))
        assert len(files) == 1
        content = files[0].read_text(encoding="utf-8")
        assert "sess-f3" in content
        assert "【原始目标】" in content

    def test_persist_failure_does_not_break_compact(self, monkeypatch):
        cm = ContextManager(max_tokens=10000)
        cm.persist_dir = None
        cm.session_id = None
        _add_rounds(cm, 4, big_user="x" * 14000)
        monkeypatch.setattr(llm, "chat_completion", lambda *a, **k: SIX_SEG_RAW)
        # 即使持久化路径不可写，压缩主流程仍完成
        result = cm.compact(model_priority=["m1"])
        assert "【原始目标】" in result
