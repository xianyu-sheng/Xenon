"""TurnTree：结构层（树/节点状态/续接通道/事件重建）。"""

from __future__ import annotations

from xenon.repl.model_registry import ModelRegistry
from xenon.repl.repl import REPL
from xenon.session.tree import FUSED, INTERRUPTED, PASSED, TurnTree


def test_append_and_chain():
    tree = TurnTree()
    n1 = tree.append_turn("修 bug")
    n2 = tree.append_turn("继续改")
    assert [n.seq for n in tree.chain()] == [1, 2]
    assert tree.tail() is n2
    assert n2.parent is n1


def test_finish_updates_status_and_fields():
    tree = TurnTree()
    n = tree.append_turn("跑 CI")
    tree.finish(n, INTERRUPTED, engine="react", verdict_reasons=["402 欠费"], steps=3)
    assert n.status == INTERRUPTED
    assert n.engine == "react"
    assert n.steps == 3
    assert n.is_resumable


def test_tail_resumable_and_status_suffix():
    tree = TurnTree()
    assert tree.tail_resumable() is None  # root 不可续接
    tree.root.status = INTERRUPTED
    assert tree.tail_resumable() is None  # root 永不续接（即使状态可续接）
    tree.root.status = PASSED
    n = tree.append_turn("修 bug")
    assert tree.tail_resumable() is None  # running 不可续接
    assert tree.status_suffix() == ""  # running 不进状态块
    tree.finish(n, PASSED)
    assert tree.status_suffix() == ""  # passed 不进状态块
    tree.finish(n, FUSED, verdict_reasons=["原地循环"])
    assert tree.tail_resumable() is n
    assert "fused" in tree.status_suffix()
    assert "继续" in tree.status_suffix()


def test_ancestor_and_move_keeps_branch():
    tree = TurnTree()
    n1 = tree.append_turn("a")
    tree.append_turn("b")
    tree.finish(tree.tail(), PASSED)
    assert tree.ancestor_at(1) is n1
    tree.move_to(n1)
    n2b = tree.append_turn("b2")
    assert n2b.seq == 2
    assert tree.chain() == [n1, n2b]
    assert len(n1.children) == 2  # 原枝保留（供 /fork 复用）


def test_rebuild_from_events():
    events = [
        {"type": "turn/user", "turn_id": "t1", "text": "修 bug"},
        {
            "type": "turn/verdict",
            "turn_id": "t1",
            "status": "interrupted",
            "reasons": ["所有模型均调用失败"],
            "engine": "react",
        },
    ]
    tree = TurnTree.rebuild_from_events(events)
    tail = tree.tail()
    assert tail.turn_id == "t1"
    assert tail.status == "interrupted"
    assert tail.is_resumable


def test_repl_resolve_bare_continuation_resumes_interrupted(monkeypatch, tmp_path):
    monkeypatch.setenv("XENON_SESSION_EVENTS_DIR", str(tmp_path))
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    repl = REPL(registry=registry, streaming=False)
    repl._turn_tree_rebuilt = True  # 跳过事件重建，直接使用新树
    n = repl._turn_tree.append_turn("修 bug")
    repl._turn_tree.finish(n, INTERRUPTED, verdict_reasons=["所有模型均调用失败: 402"])

    notice, resumed = repl._resolve_bare_continuation("继续")
    assert notice is None
    assert "续接上一轮任务" in resumed
    assert "402" in resumed


def test_repl_resolve_bare_continuation_without_resumable_tail(monkeypatch, tmp_path):
    monkeypatch.setenv("XENON_SESSION_EVENTS_DIR", str(tmp_path))
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    repl = REPL(registry=registry, streaming=False)
    repl._turn_tree_rebuilt = True
    n = repl._turn_tree.append_turn("闲聊")
    repl._turn_tree.finish(n, PASSED)

    notice, text = repl._resolve_bare_continuation("继续")
    assert notice is not None
    assert "没有可继续" in notice
    assert text == "继续"


def test_finish_turn_node_writes_verdict_event(monkeypatch, tmp_path):
    monkeypatch.setenv("XENON_SESSION_EVENTS_DIR", str(tmp_path))
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    repl = REPL(registry=registry, streaming=False)
    repl._turn_tree_rebuilt = True
    repl._turn_tree.append_turn("跑 CI")
    repl._finish_turn_node("passed", engine="react")
    events = repl._session_events.read()
    verdicts = [e for e in events if e["type"] == "turn/verdict"]
    assert len(verdicts) == 1
    assert verdicts[0]["status"] == "passed"
    assert repl._turn_tree.tail().status == "passed"


def test_task_state_block_includes_tail_status(monkeypatch, tmp_path):
    monkeypatch.setenv("XENON_SESSION_EVENTS_DIR", str(tmp_path))
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    repl = REPL(registry=registry, streaming=False)
    repl._turn_tree_rebuilt = True
    n = repl._turn_tree.append_turn("修 bug")
    repl._turn_tree.finish(n, INTERRUPTED, verdict_reasons=["引擎异常"])
    block = repl._task_state_block()
    assert "interrupted" in block
    assert "继续" in block


def test_finish_turn_node_records_artifacts_from_panel(monkeypatch, tmp_path):
    monkeypatch.setenv("XENON_SESSION_EVENTS_DIR", str(tmp_path))
    registry = ModelRegistry()
    registry.add_model("openai/test", "test")
    repl = REPL(registry=registry, streaming=False)
    repl._turn_tree_rebuilt = True

    class _Step:
        action = "write_file"
        is_error = False
        action_input = {"file_path": "out.py"}
        observation = "ok"

    class _Panel:
        steps = [_Step()]
        errors: list = []
        tool_call_count = 1

    node = repl._turn_tree.append_turn("写文件")
    repl._finish_turn_node("passed", engine="react", panel=_Panel())
    assert node.artifacts == ["out.py"]
    # 状态块单一来源：产物来自树尾节点
    block = repl._task_state_block()
    assert "out.py" in block


def test_paths_from_panel_skips_failures_and_dedups():
    from xenon.repl.turn_helpers import paths_from_panel

    class _S:
        def __init__(self, action, path, ok=True):
            self.action = action
            self.action_input = {"file_path": path}
            self.is_error = not ok
            self.observation = ""

    class _P:
        steps = [_S("write_file", "a.py"), _S("write_file", "a.py"), _S("edit_file", "b.py", ok=False)]

    assert paths_from_panel(_P()) == ["a.py"]
