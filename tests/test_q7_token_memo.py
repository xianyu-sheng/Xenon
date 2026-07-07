"""P3-Q7 token 估算 memoization + CJK 范围扩展 + 注释代码统一测试。"""

from __future__ import annotations

import copy

from omniagent.repl.context_manager import (
    ContextManager, ConversationTurn, _estimate_tokens, _CJK_RE,
)


# --------------------------- estimate_tokens 基础（向后兼容） ---------------------------

def test_estimate_tokens_empty():
    assert _estimate_tokens("") == 0
    assert ContextManager().estimate_tokens("") == 0


def test_estimate_tokens_english_words():
    cm = ContextManager()
    assert cm.estimate_tokens("hello world foo bar") >= 4


def test_estimate_tokens_cjk_basic():
    cm = ContextManager()
    assert cm.estimate_tokens("你好世界") >= 6


def test_estimate_tokens_floor_is_len_half():
    """§8.26.2 注释代码统一：无空格长串至少 len//2（非 len/3）。"""
    s = "abcdefghij"  # 10 chars, 1 word, no CJK, not code-heavy
    # words=1, char_based=10//2=5 → max(1, 1, 5)=5
    assert _estimate_tokens(s) == 5


def test_estimate_tokens_code_heavy():
    s = "{a=1;b=2;c=3;d=4;e=5;}"  # 大量 {} ; =
    # code_chars > 2% → max(words*2, chars*0.4)
    result = _estimate_tokens(s)
    assert result >= int(len(s) * 0.4)


# --------------------------- CJK 范围扩展 ---------------------------

def test_cjk_regex_includes_hiragana():
    assert len(_CJK_RE.findall("こんにちは")) == 5


def test_cjk_regex_includes_katakana():
    assert len(_CJK_RE.findall("カタカナ")) == 4


def test_cjk_regex_includes_hangul():
    assert len(_CJK_RE.findall("안녕하세요")) == 5


def test_cjk_regex_includes_ext_a():
    # U+3400 㐀（CJK 扩展 A）
    assert len(_CJK_RE.findall("㐀㐀㐀")) == 3


def test_estimate_tokens_hiragana_counted_as_cjk():
    """扩展前假名被当英文（≈2 token），扩展后按 CJK（≈10 token）。"""
    result = _estimate_tokens("こんにちは")  # 5 假名
    assert result >= 8  # CJK 分支：max(words=1, 5*2=10, char_based=2) = 10


def test_estimate_tokens_hangul_counted_as_cjk():
    result = _estimate_tokens("안녕하세요")
    assert result >= 8


# --------------------------- ConversationTurn.token_count 缓存 ---------------------------

def test_turn_token_count_computed():
    t = ConversationTurn(role="user", content="hello world")
    assert t.token_count > 0


def test_turn_token_count_caches(monkeypatch):
    """首次访问计算并缓存，第二次不重算。"""
    calls = []

    def counting(text):
        calls.append(text)
        return _estimate_tokens.__wrapped__(text) if hasattr(_estimate_tokens, "__wrapped__") else 42

    import omniagent.repl.context_manager as cm_mod
    monkeypatch.setattr(cm_mod, "_estimate_tokens", counting)
    t = ConversationTurn(role="user", content="some content here")
    v1 = t.token_count
    v2 = t.token_count
    assert v1 == v2
    assert len(calls) == 1  # 只计算一次


def test_turn_token_count_empty():
    t = ConversationTurn(role="system", content="")
    assert t.token_count == 0


def test_turn_token_count_not_in_repr():
    t = ConversationTurn(role="user", content="hi")
    _ = t.token_count  # 触发缓存
    r = repr(t)
    assert "_token_count" not in r


def test_turn_equal_ignores_cache():
    """compare=False：缓存状态不影响相等性。"""
    t1 = ConversationTurn(role="user", content="hi")
    t2 = ConversationTurn(role="user", content="hi")
    _ = t1.token_count  # t1 已缓存，t2 未缓存
    assert t1 == t2


def test_deepcopy_preserves_cache():
    t = ConversationTurn(role="user", content="hello world")
    _ = t.token_count
    t2 = copy.deepcopy(t)
    assert t2.token_count == t.token_count  # 缓存值被复制


# --------------------------- current_token_usage 用缓存 ---------------------------

def test_current_token_usage_sums_cached(monkeypatch):
    """current_token_usage 求和缓存值，多次调用不重算（O(n) 免重算）。"""
    import omniagent.repl.context_manager as cm_mod

    calls = [0]
    real = _estimate_tokens

    def counting(text):
        calls[0] += 1
        return real(text)

    monkeypatch.setattr(cm_mod, "_estimate_tokens", counting)

    cm = ContextManager()
    cm.add_user_message("first message")
    cm.add_assistant_message("second message here")
    cm.add_user_message("third")

    u1 = cm.current_token_usage()
    u2 = cm.current_token_usage()
    u3 = cm.current_token_usage()

    assert u1 == u2 == u3
    # 3 个 turn 各算一次，多次 current_token_usage 不再触发计算
    assert calls[0] == 3


def test_current_token_usage_consistent_with_estimate():
    cm = ContextManager()
    cm.add_user_message("hello world foo bar")
    cm.add_assistant_message("你好世界")
    manual = sum(cm.estimate_tokens(t.content) for t in cm.history)
    assert cm.current_token_usage() == manual
