"""L0 证据层：会话事件哈希链 + /evidence 命令自证视图。"""

from __future__ import annotations

import json

from xenon.session.events import (
    GENESIS,
    KEY_FIELD,
    KEYED_VALUE,
    ChainReport,
    SessionEventLog,
    event_hash_hex,
    verify_event_chain,
)


def test_append_writes_hash_and_prev(tmp_path):
    log = SessionEventLog("s1", directory=tmp_path)
    log.append("turn/user", text="hello")
    log.append("tool/result", tool="read_file", success=True)
    events = log.read()
    assert len(events) == 2
    assert events[0]["prev"] == GENESIS
    assert events[1]["prev"] == events[0]["h"]
    for event in events:
        assert event["h"] == event_hash_hex(event)


def test_chain_verifies_ok_and_counts():
    events = [
        {"id": "a", "type": "turn/user", "at": 1.0, "h": "", "prev": ""},
        {"id": "b", "type": "tool/result", "at": 2.0, "h": "", "prev": ""},
    ]
    events[0]["h"] = event_hash_hex(events[0])
    events[0]["prev"] = GENESIS
    events[1]["h"] = event_hash_hex(events[1])
    events[1]["prev"] = events[0]["h"]
    report = verify_event_chain(events)
    assert report.status == "ok"
    assert report.verified == 2
    assert report.legacy == 0
    assert report.broken_at is None
    assert report.head_hash == events[1]["h"]


def test_chain_detects_tampered_content():
    events = [
        {"id": "a", "type": "turn/user", "at": 1.0, "h": "", "prev": ""},
    ]
    events[0]["h"] = event_hash_hex(events[0])
    events[0]["prev"] = GENESIS
    tampered = json.loads(json.dumps(events))
    tampered[0]["type"] = "turn/assistant"  # 篡改内容但保留原哈希
    report = verify_event_chain(tampered)
    assert report.status == "broken"
    assert report.broken_at == 0


def test_chain_detects_broken_prev_link():
    events = [
        {"id": "a", "type": "turn/user", "at": 1.0, "h": "", "prev": ""},
        {"id": "b", "type": "tool/result", "at": 2.0, "h": "", "prev": ""},
    ]
    events[0]["h"] = event_hash_hex(events[0])
    events[0]["prev"] = GENESIS
    events[1]["h"] = event_hash_hex(events[1])
    events[1]["prev"] = "deadbeef"  # 不指向前驱
    report = verify_event_chain(events)
    assert report.status == "broken"
    assert report.broken_at == 1


def test_legacy_events_reported_as_unverifiable():
    legacy = [{"id": "x", "type": "turn/user", "at": 1.0}]
    report = verify_event_chain(legacy)
    assert report.status == "legacy"
    assert report.legacy == 1
    assert report.verified == 0


def test_mixed_legacy_then_hashed_is_partial():
    legacy = [{"id": "x", "type": "turn/user", "at": 1.0}]
    hashed = {"id": "y", "type": "tool/result", "at": 2.0, "h": "", "prev": ""}
    hashed["h"] = event_hash_hex(hashed)
    hashed["prev"] = GENESIS
    report = verify_event_chain([*legacy, hashed])
    assert report.status == "partial"
    assert report.legacy == 1
    assert report.verified == 1


def test_genesis_restart_after_hashed_events_is_broken():
    """链上不允许 genesis 重启（篡改 prev 会被逮住）。"""

    events = [
        {"id": "a", "type": "turn/user", "at": 1.0, "h": "", "prev": ""},
        {"id": "b", "type": "tool/result", "at": 2.0, "h": "", "prev": ""},
    ]
    events[0]["h"] = event_hash_hex(events[0])
    events[0]["prev"] = GENESIS
    events[1]["h"] = event_hash_hex(events[1])
    events[1]["prev"] = GENESIS
    report = verify_event_chain(events)
    assert report.status == "broken"
    assert report.broken_at == 1


def test_chain_report_status_labels():
    assert ChainReport(total=0).status == "empty"
    assert ChainReport(total=1, verified=0, legacy=1).status == "legacy"
    assert ChainReport(total=2, verified=1, legacy=1).status == "partial"
    assert ChainReport(total=2, verified=2).status == "ok"
    assert ChainReport(total=2, verified=1, broken_at=1).status == "broken"


def test_append_resumes_chain_after_reopen(tmp_path):
    """同一文件重开新 SessionEventLog → 链继续（跨进程/重启自证）。"""

    log1 = SessionEventLog("s1", directory=tmp_path)
    log1.append("turn/user", text="first")
    log2 = SessionEventLog("s1", directory=tmp_path)
    log2.append("turn/user", text="second")
    events = log2.read()
    report = verify_event_chain(events)
    assert report.status == "ok"
    assert events[1]["prev"] == events[0]["h"]


def test_evidence_command_current_session(tmp_path, monkeypatch):
    """/evidence 默认展示结构化回合摘要（给人看）。"""

    from xenon.repl.command_groups.evidence import _cmd_evidence

    log = SessionEventLog("sess-1", directory=tmp_path)
    log.append("turn/user", text="调研任务", intent="资料调研")
    log.append("tool/result", tool="github_fetch", success=True)
    log.append("turn/verdict", status="passed", engine="react", steps=4, tools=1, errors=0)
    log.append("turn/assistant", preview="调研结论：这是一个多智能体系统")

    class _Repl:
        _session_events = log

    out = _cmd_evidence(args="", session_state={"_repl": _Repl()})
    assert "sess-1" in out
    assert "链: ✅ 完整" in out
    assert "会话回合" in out
    assert "🎯 资料调研" in out
    assert "github_fetch ×1" in out
    assert "✅ 发布成功" in out
    assert "多智能体系统" in out


def test_evidence_command_raw_view(tmp_path):
    """/evidence raw 保留逐条哈希明细（给机器/审计员看）。"""

    from xenon.repl.command_groups.evidence import _cmd_evidence

    log = SessionEventLog("sess-1", directory=tmp_path)
    log.append("turn/user", text="hi")

    class _Repl:
        _session_events = log

    out = _cmd_evidence(args="raw", session_state={"_repl": _Repl()})
    assert "逐条事件" in out
    assert "turn/user" in out
    assert "h=" in out


def test_evidence_human_view_marks_reminder_pass(tmp_path):
    """passed + 核验理由 → 显示「发布成功 + 附核验提醒」。"""

    from xenon.repl.command_groups.evidence import _cmd_evidence

    log = SessionEventLog("sess-1", directory=tmp_path)
    log.append("turn/user", text="调研")
    log.append(
        "turn/verdict",
        status="passed",
        reasons=["声称创建但未经工具验证的文件: a.md"],
        engine="react",
    )

    class _Repl:
        _session_events = log

    out = _cmd_evidence(args="", session_state={"_repl": _Repl()})
    assert "🟡 发布成功 · 附 1 条核验提醒" in out


def test_evidence_command_global_view(tmp_path, monkeypatch):
    from xenon.repl.command_groups.evidence import _cmd_evidence
    from xenon.session import events as events_mod

    monkeypatch.setattr(
        events_mod, "session_events_dir", lambda: __import__("pathlib").Path(tmp_path)
    )
    log = SessionEventLog("sess-a", directory=tmp_path)
    log.append("turn/user", text="hi")
    SessionEventLog("sess-b", directory=tmp_path).append("turn/user", text="hi")

    out = _cmd_evidence(args="all", session_state={})
    assert "sess-a" in out and "sess-b" in out


def test_evidence_command_specific_session(tmp_path, monkeypatch):
    from xenon.repl.command_groups.evidence import _cmd_evidence
    from xenon.session import events as events_mod

    monkeypatch.setattr(
        events_mod, "session_events_dir", lambda: __import__("pathlib").Path(tmp_path)
    )
    log = SessionEventLog("sess-x", directory=tmp_path)
    log.append("turn/user", text="hi")

    out = _cmd_evidence(args="sess-x", session_state={})
    assert "sess-x" in out


def test_evidence_command_detects_tampering(tmp_path, monkeypatch):
    from xenon.repl.command_groups.evidence import _cmd_evidence
    from xenon.session import events as events_mod

    monkeypatch.setattr(
        events_mod, "session_events_dir", lambda: __import__("pathlib").Path(tmp_path)
    )
    log = SessionEventLog("sess-t", directory=tmp_path)
    log.append("turn/user", text="hi")
    # 直接改写文件内容（不重算哈希）
    path = log.path
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    record["text"] = "篡改后的内容"
    path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    out = _cmd_evidence(args="sess-t", session_state={})
    assert "断裂" in out


def test_keyed_chain_verifies_with_key(tmp_path, monkeypatch):
    monkeypatch.setenv("XENON_EVIDENCE_HMAC_KEY", "test-key-123")
    log = SessionEventLog("s-k1", directory=tmp_path)
    log.append("turn/user", text="hello")
    log.append("tool/result", tool="read_file", success=True)
    events = log.read()
    assert events[0].get(KEY_FIELD) == KEYED_VALUE
    report = verify_event_chain(events, key="test-key-123")
    assert report.status == "ok"
    assert report.mode == "keyed"
    assert report.verified == 2 and report.keyed == 2 and report.unkeyed == 0
    for event in events:
        assert event["h"] == event_hash_hex(event, key="test-key-123")


def test_keyed_chain_tamper_breaks_with_key(tmp_path, monkeypatch):
    monkeypatch.setenv("XENON_EVIDENCE_HMAC_KEY", "test-key-123")
    log = SessionEventLog("s-k2", directory=tmp_path)
    log.append("turn/user", text="hello")
    log.append("turn/user", text="second")
    events = log.read()
    tampered = json.loads(json.dumps(events))
    tampered[1]["text"] = "篡改"
    report = verify_event_chain(tampered, key="test-key-123")
    assert report.status == "broken"
    assert report.broken_at == 1


def test_keyed_chain_reorder_breaks_with_key(tmp_path, monkeypatch):
    monkeypatch.setenv("XENON_EVIDENCE_HMAC_KEY", "test-key-123")
    log = SessionEventLog("s-k3", directory=tmp_path)
    log.append("turn/user", text="first")
    log.append("turn/user", text="second")
    events = log.read()
    report = verify_event_chain([events[1], events[0]], key="test-key-123")
    assert report.status == "broken"


def test_keyed_chain_recomputed_without_key_breaks(tmp_path, monkeypatch):
    """攻击者无密钥整链重算（明文 sha256）→ 核验必失败。"""
    monkeypatch.setenv("XENON_EVIDENCE_HMAC_KEY", "test-key-123")
    log = SessionEventLog("s-k4", directory=tmp_path)
    log.append("turn/user", text="first")
    log.append("turn/user", text="second")
    events = json.loads(json.dumps(log.read()))
    for event in events:
        event["h"] = event_hash_hex(event)  # 无密钥重算
    report = verify_event_chain(events, key="test-key-123")
    assert report.status == "broken"
    assert report.broken_at == 0


def test_keyed_chain_without_key_does_not_silently_pass(tmp_path, monkeypatch):
    """缺密钥的核验者面对 keyed 链：不算通过，计入 keyed_pending。"""
    monkeypatch.setenv("XENON_EVIDENCE_HMAC_KEY", "test-key-123")
    log = SessionEventLog("s-k5", directory=tmp_path)
    log.append("turn/user", text="first")
    log.append("turn/user", text="second")
    events = log.read()
    report = verify_event_chain(events)
    assert report.verified == 0
    assert report.keyed == 2 and report.keyed_pending == 2
    assert report.status == "partial"
    assert report.mode == "keyed"


def test_mixed_chain_reports_partial(tmp_path, monkeypatch):
    """链先无密钥写入、后设密钥：混合模式 → partial，/evidence 会说明。"""
    log = SessionEventLog("s-mix", directory=tmp_path)
    log.append("turn/user", text="first")
    monkeypatch.setenv("XENON_EVIDENCE_HMAC_KEY", "test-key-123")
    log.append("turn/user", text="second")
    events = log.read()
    report = verify_event_chain(events, key="test-key-123")
    assert report.status == "partial"
    assert report.mode == "mixed"
    assert report.verified == 2
    assert report.keyed == 1 and report.unkeyed == 1
    # 缺密钥时新事件不可验，但旧链接仍在
    report2 = verify_event_chain(events)
    assert report2.keyed_pending == 1
    assert report2.status == "partial"


def test_hmac_key_never_written_to_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("XENON_EVIDENCE_HMAC_KEY", "super-secret-key")
    log = SessionEventLog("s-disk", directory=tmp_path)
    log.append("turn/user", text="hi")
    content = log.path.read_text(encoding="utf-8")
    assert "super-secret-key" not in content
    assert "hmac-sha256" in content


def test_keyed_chain_resumes_after_reopen(tmp_path, monkeypatch):
    """设密钥后重开日志继续写：新事件 keyed，跨重启链仍连续。"""
    monkeypatch.setenv("XENON_EVIDENCE_HMAC_KEY", "test-key-123")
    log1 = SessionEventLog("s-resume", directory=tmp_path)
    log1.append("turn/user", text="first")
    log2 = SessionEventLog("s-resume", directory=tmp_path)
    log2.append("turn/user", text="second")
    events = log2.read()
    report = verify_event_chain(events, key="test-key-123")
    assert report.status == "ok"
    assert events[1]["prev"] == events[0]["h"]
