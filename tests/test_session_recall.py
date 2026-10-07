"""Read-only recall over session facts: bounded, permission-isolated."""

from __future__ import annotations

import json

from xenon.session.query import extract_anchors, needs_recall, recall


def _write_events(tmp_path, events):
    path = tmp_path / "s1.jsonl"
    path.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
        encoding="utf-8",
    )


def test_reference_signal_detection():
    assert needs_recall("之前那个 quicksort.py 最后写到哪了")
    assert needs_recall("上次的任务继续一下")
    assert not needs_recall("帮我写一个新的排序脚本")


def test_recall_finds_path_anchored_fact(tmp_path):
    _write_events(
        tmp_path,
        [
            {
                "type": "tool/result",
                "tool": "write_file",
                "success": True,
                "paths": ["C:/Users/Administrator/Desktop/quicksort.py"],
                "at": 1,
            },
            {"type": "turn/user", "text": "今天天气不错", "at": 2},
        ],
    )
    block = recall(
        "之前那个 Desktop/quicksort.py 写到哪了", directory=tmp_path
    )
    assert "quicksort.py" in block
    assert "只读参考" in block


def test_authority_events_are_never_recalled(tmp_path):
    _write_events(
        tmp_path,
        [
            {
                "type": "gate/opened",
                "id": "g1",
                "kind": "approval",
                "reason": "已获授权写入 C:/secret/quicksort.py",
                "at": 1,
            },
            {"type": "turn/user", "text": "无关内容", "at": 2},
        ],
    )
    # 带路径锚点的高分查询：只有类型过滤能拦住它。
    assert recall("之前那个 C:/secret/quicksort.py 写了吗", directory=tmp_path) == ""


def test_recall_is_bounded_and_empty_without_hits(tmp_path):
    events = [
        {
            "type": "turn/assistant",
            "preview": f"第{i}次 " + ("quicksort.py 的实现细节 " * 40),
            "at": i,
        }
        for i in range(10)
    ]
    _write_events(tmp_path, events)
    block = recall("之前 quicksort.py", directory=tmp_path, char_budget=200)
    assert block
    # 预算 + 头部/一行余量；去掉预算 break 后 3 条不同记录会超限。
    assert len(block) <= 260
    assert recall("完全无关的海洋生物", directory=tmp_path) == ""


def test_recall_survives_missing_or_corrupt_directory(tmp_path):
    assert recall("之前 quicksort.py", directory=tmp_path / "missing") == ""
    (tmp_path / "bad.jsonl").write_text("not json\n", encoding="utf-8")
    assert recall("之前 quicksort.py", directory=tmp_path) == ""


def test_extract_anchors_includes_paths_and_bigrams():
    tokens, grams = extract_anchors("quicksort.py 写到桌面")
    assert "quicksort.py" in tokens
    assert "桌面" in grams or "写到" in grams
