"""P3-Q2 run_id/call_id 链路追踪测试。"""

from __future__ import annotations

import logging

from xenon.engine.trace import new_run_id, new_call_id, prefix, trace_logger


# --------------------------- ID 生成 ---------------------------

def test_new_run_id_length_and_hex():
    rid = new_run_id()
    assert len(rid) == 8
    int(rid, 16)  # 是合法 hex


def test_new_call_id_length_and_hex():
    cid = new_call_id()
    assert len(cid) == 6
    int(cid, 16)


def test_new_run_id_uniqueness():
    ids = {new_run_id() for _ in range(500)}
    assert len(ids) == 500  # 极小概率冲突也不应在 500 内


def test_new_call_id_uniqueness():
    ids = {new_call_id() for _ in range(500)}
    assert len(ids) == 500


# --------------------------- prefix() ---------------------------

def test_prefix_run_only():
    assert prefix("abc12345") == "[abc12345]"


def test_prefix_run_and_call():
    assert prefix("abc12345", "deadbe") == "[abc12345/deadbe]"


def test_prefix_none_run_uses_placeholder():
    assert prefix(None) == "[?]"


def test_prefix_none_call_omits_slash():
    # 有 run_id 无 call_id → 不带斜杠
    assert prefix("abc12345", None) == "[abc12345]"
    # 都缺 → 仅 [?]
    assert prefix(None, None) == "[?]"


# --------------------------- trace_logger ---------------------------

def test_trace_logger_emits_prefix(caplog):
    caplog.set_level(logging.INFO, logger="xenon.trace")
    tl = trace_logger("abc12345", "deadbe")
    tl.info("hello")
    assert any("[abc12345/deadbe]" in r.message for r in caplog.records)
    assert any("hello" in r.message for r in caplog.records)


# --------------------------- BaseEngine._begin_run / _call_llm ---------------------------

def _make_engine():
    from xenon.engine.react_engine import ReActEngine
    return ReActEngine(["test/model"])


def test_begin_run_sets_run_id(caplog):
    caplog.set_level(logging.INFO, logger="xenon.engine")
    eng = _make_engine()
    assert eng.run_id is None
    rid = eng._begin_run()
    assert eng.run_id == rid
    assert len(rid) == 8
    # 日志带 [run_id] 前缀 + "run 开始"
    assert any(f"[{rid}]" in r.message and "run 开始" in r.message
               for r in caplog.records)


def test_begin_run_generates_different_ids_per_run():
    eng = _make_engine()
    r1 = eng._begin_run()
    r2 = eng._begin_run()
    assert r1 != r2
    assert eng.run_id == r2


def test_call_llm_logs_carry_run_call_prefix(caplog):
    """_call_llm 内每次调用生成 call_id，失败日志带 [run_id/call_id] 前缀。"""
    import xenon.engine.base as base_mod

    eng = _make_engine()
    eng._begin_run()
    rid = eng.run_id

    def boom(*a, **kw):
        raise RuntimeError("boom")

    orig = base_mod.chat_completion
    base_mod.chat_completion = boom
    try:
        caplog.set_level(logging.DEBUG, logger="xenon.engine.base")
        try:
            eng._call_llm([{"role": "user", "content": "hi"}])
            assert False, "应抛 RuntimeError"
        except RuntimeError:
            pass
    finally:
        base_mod.chat_completion = orig

    # 所有 _call_llm 日志都带同一 [run_id/...] 前缀
    engine_logs = [r.message for r in caplog.records
                   if r.name == "xenon.engine.base"]
    prefixed = [m for m in engine_logs if f"[{rid}/" in m]
    assert prefixed, f"未找到带 [{rid}/call_id] 前缀的日志: {engine_logs}"
    # 失败日志内容存在
    assert any("失败" in m for m in prefixed)


def test_react_run_sets_run_id():
    """ReAct run() 开头调 _begin_run，run_id 在 run 内非 None。"""
    from xenon.engine.react_engine import ReActEngine
    eng = ReActEngine(["m1"])

    seen_run_id = {}

    def fake_llm(messages, **kw):
        seen_run_id["rid"] = eng.run_id
        return "Final Answer: ok"

    eng._call_llm = fake_llm
    eng.max_iterations = 1
    eng.run("hi")
    assert seen_run_id["rid"] is not None
    assert len(seen_run_id["rid"]) == 8


def test_call_llm_call_id_differs_across_calls(caplog):
    """两次 _call_llm 调用生成不同 call_id（前缀中的 call_id 段不同）。"""
    import xenon.engine.base as base_mod

    eng = _make_engine()
    eng._begin_run()
    rid = eng.run_id

    def boom(*a, **kw):
        raise RuntimeError("boom")

    orig = base_mod.chat_completion
    base_mod.chat_completion = boom
    call_ids = []
    try:
        caplog.set_level(logging.DEBUG, logger="xenon.engine.base")
        for _ in range(2):
            try:
                eng._call_llm([{"role": "user", "content": "hi"}])
            except RuntimeError:
                pass
    finally:
        base_mod.chat_completion = orig

    import re
    for r in caplog.records:
        if r.name == "xenon.engine.base" and f"[{rid}/" in r.message:
            m = re.search(rf"\[{rid}/([0-9a-f]{{6}})\]", r.message)
            if m:
                call_ids.append(m.group(1))
    # 两次调用至少出现两个不同 call_id
    assert len(set(call_ids)) >= 2, f"call_id 未变化: {call_ids}"
