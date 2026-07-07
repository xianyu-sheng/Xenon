"""P2-E1 DirectoryScout 测试。

只用临时目录，绝不碰真实用户配置文件。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from omniagent.engine.scout import DirectoryScout, DEFAULT_EXCLUDE


# --------------------------- scan() ---------------------------

def test_scan_nonexistent_root_returns_none():
    scout = DirectoryScout(project_root="/nonexistent/path/xyz", max_depth=1)
    assert scout.scan() is None


def test_scan_empty_dir_returns_none():
    with tempfile.TemporaryDirectory() as d:
        scout = DirectoryScout(project_root=d, max_depth=1)
        assert scout.scan() is None


def test_scan_returns_tree_with_files():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "README.md").write_text("hi")
        (Path(d) / "main.py").write_text("print('hi')")
        scout = DirectoryScout(project_root=d, max_depth=1)
        result = scout.scan()
        assert result is not None
        assert result["file_count"] == 2
        assert "README.md" in result["tree"]
        assert "main.py" in result["tree"]


def test_scan_respects_max_depth():
    # 语义：max_depth=N 表示进入 N 层子目录内容。
    # depth0=根条目，depth1=一级子目录内容，depth2=二级子目录内容。
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "top.py").write_text("x")
        sub = root / "sub"
        sub.mkdir()
        (sub / "deep.py").write_text("y")
        deepsub = sub / "deeper"
        deepsub.mkdir()
        (deepsub / "very_deep.py").write_text("z")

        # max_depth=0：只看根目录条目（子目录名列出但不展开）
        scout0 = DirectoryScout(project_root=d, max_depth=0)
        r0 = scout0.scan()
        assert r0 is not None
        assert "top.py" in r0["tree"]
        assert "sub" in r0["tree"]
        assert "deep.py" not in r0["tree"]

        # max_depth=1：进一层，看得到 deep.py，但看不到 very_deep.py
        scout1 = DirectoryScout(project_root=d, max_depth=1)
        r1 = scout1.scan()
        assert r1 is not None
        assert "deep.py" in r1["tree"]
        assert "very_deep.py" not in r1["tree"]


def test_scan_excludes_common_dirs():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "keep.py").write_text("x")
        nm = root / "node_modules"
        nm.mkdir()
        (nm / "huge.js").write_text("y")
        git = root / ".git"
        git.mkdir()
        (git / "config").write_text("z")
        scout = DirectoryScout(project_root=d, max_depth=2)
        result = scout.scan()
        assert result is not None
        assert "keep.py" in result["tree"]
        assert "node_modules" not in result["tree"]
        assert ".git" not in result["tree"]
        assert "huge.js" not in result["tree"]


def test_scan_excludes_egg_info_glob():
    # §8.21.3：*.egg-info 在 set 里原用 exact match 失效，fnmatch 应正确匹配
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "setup.py").write_text("x")
        egg = root / "mypkg.egg-info"
        egg.mkdir()
        (egg / "PKG-INFO").write_text("y")
        scout = DirectoryScout(project_root=d, max_depth=2)
        result = scout.scan()
        assert result is not None
        assert "setup.py" in result["tree"]
        assert "mypkg.egg-info" not in result["tree"]
        assert "PKG-INFO" not in result["tree"]


def test_scan_does_not_follow_symlinks():
    # §8.21.2：不跟随符号链接（防循环/扫全盘）
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "real.py").write_text("x")
        # 指向自身的符号链接（循环）
        link = root / "loop"
        link.symlink_to(root)
        scout = DirectoryScout(project_root=d, max_depth=3)
        result = scout.scan()
        assert result is not None
        # 符号链接本身不出现
        assert "loop" not in result["tree"]
        assert "real.py" in result["tree"]


def test_scan_symlink_root_returns_none():
    with tempfile.TemporaryDirectory() as d:
        link = Path(d).parent / ("scout_link_" + os.urandom(3).hex())
        link.symlink_to(d)
        try:
            scout = DirectoryScout(project_root=str(link), max_depth=1)
            assert scout.scan() is None
        finally:
            link.unlink(missing_ok=True)


def test_scan_caps_entries_per_dir():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for i in range(10):
            (root / f"f{i}.py").write_text("x")
        scout = DirectoryScout(project_root=d, max_depth=1, max_entries_per_dir=3)
        result = scout.scan()
        assert result is not None
        # 截断标记
        assert "more in" in result["tree"]
        # 只列了 3 个文件
        listed = [ln for ln in result["tree"].splitlines() if ln.startswith("📄")]
        assert len(listed) == 3


def test_scan_sorts_dirs_first():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "zfile.py").write_text("x")
        (root / "adir").mkdir()
        (root / "afile.py").write_text("y")
        scout = DirectoryScout(project_root=d, max_depth=1)
        result = scout.scan()
        lines = [ln.strip() for ln in result["tree"].splitlines()]
        # 目录在前
        assert lines.index("📁 adir") < lines.index("📄 afile.py")


def test_default_exclude_is_frozenset():
    assert isinstance(DEFAULT_EXCLUDE, frozenset)
    assert ".git" in DEFAULT_EXCLUDE
    assert "node_modules" in DEFAULT_EXCLUDE


# --------------------------- scout_from_history() ---------------------------

def test_scout_from_history_extracts_paths():
    scout = DirectoryScout(project_root="/nonexistent")
    msgs = [
        {"role": "assistant", "content": "Thought: let me list"},
        {"role": "user", "content": "Observation:\nsrc/main.py\nsrc/utils.py\nREADME.md"},
    ]
    result = scout.scout_from_history(msgs)
    assert result is not None
    assert "src/main.py" in result
    assert "README.md" in result


def test_scout_from_history_none_when_no_observation():
    scout = DirectoryScout(project_root="/nonexistent")
    msgs = [{"role": "assistant", "content": "Thought: thinking"}]
    assert scout.scout_from_history(msgs) is None


def test_scout_from_history_none_when_empty():
    scout = DirectoryScout(project_root="/nonexistent")
    assert scout.scout_from_history(None) is None
    assert scout.scout_from_history([]) is None


def test_scout_from_history_uses_most_recent():
    scout = DirectoryScout(project_root="/nonexistent")
    msgs = [
        {"role": "user", "content": "Observation:\nold.py"},
        {"role": "user", "content": "Observation:\nnew.py"},
    ]
    result = scout.scout_from_history(msgs)
    assert "new.py" in result
    assert "old.py" not in result


# --------------------------- inject() ---------------------------

def test_inject_prepends_tree_when_scan_has_data():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "README.md").write_text("hi")
        scout = DirectoryScout(project_root=d, max_depth=1)
        out = scout.inject("请帮我读 README")
        assert out.startswith("[项目结构预览")
        assert "README.md" in out
        assert "请帮我读 README" in out


def test_inject_appends_list_files_hint_when_no_data():
    scout = DirectoryScout(project_root="/nonexistent/xyz")
    out = scout.inject("帮我改 main.py")
    assert "list_files" in out
    assert "帮我改 main.py" in out


def test_inject_uses_history_when_no_scan():
    scout = DirectoryScout(project_root="/nonexistent/xyz")
    msgs = [{"role": "user", "content": "Observation:\nsrc/main.py"}]
    out = scout.inject("帮我改 main.py", messages=msgs)
    assert "[历史已扫描的文件" in out
    assert "src/main.py" in out
    # 有历史时不再追加 list_files 强制提示
    assert "请第一步先调用 list_files" not in out


def test_inject_scan_takes_precedence_over_history():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "real.py").write_text("x")
        scout = DirectoryScout(project_root=d, max_depth=1)
        msgs = [{"role": "user", "content": "Observation:\nold.py"}]
        out = scout.inject("task", messages=msgs)
        assert "[项目结构预览" in out
        assert "real.py" in out
        # 扫描数据优先，不用历史
        assert "old.py" not in out


def test_inject_preserves_original_input_intact():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "f.py").write_text("x")
        scout = DirectoryScout(project_root=d, max_depth=1)
        original = "复杂任务: 请重构\n  这部分代码"
        out = scout.inject(original)
        assert original in out


def test_inject_custom_exclude_dirs():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "keep.py").write_text("x")
        custom = root / "secret"
        custom.mkdir()
        (custom / "key.txt").write_text("y")
        scout = DirectoryScout(
            project_root=d, max_depth=2, exclude_dirs=frozenset({"secret"})
        )
        result = scout.scan()
        assert result is not None
        assert "keep.py" in result["tree"]
        assert "secret" not in result["tree"]


# --------------------------- ReAct 集成（opt-in project_root） ---------------------------

class _RecordingLLM:
    """假 _call_llm：记录收到的消息，立即返回 Final Answer。"""

    def __init__(self):
        self.seen_messages = None

    def __call__(self, messages, **kwargs):
        self.seen_messages = messages
        return "Final Answer: done"


def _make_engine(project_root=None):
    from omniagent.engine.react_engine import ReActEngine
    eng = ReActEngine(["m1"], project_root=project_root)
    return eng


def test_react_scout_off_by_default():
    # 无 project_root → _scout 为 None，run 不扫描、不改 user_input
    eng = _make_engine()
    assert eng._scout is None


def test_react_scout_injects_tree_when_project_root_set():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "README.md").write_text("hi")
        eng = _make_engine(project_root=d)
        assert eng._scout is not None
        recorder = _RecordingLLM()
        eng._call_llm = recorder  # patch
        eng.max_iterations = 1
        eng.run("帮我读 README")
        # 第一轮 LLM 调用收到的 user 消息含项目结构预览
        user_msg = next(m for m in recorder.seen_messages if m["role"] == "user")
        assert "[项目结构预览" in user_msg["content"]
        assert "README.md" in user_msg["content"]


def test_react_scout_appends_hint_when_root_empty():
    with tempfile.TemporaryDirectory() as d:
        eng = _make_engine(project_root=d)
        recorder = _RecordingLLM()
        eng._call_llm = recorder
        eng.max_iterations = 1
        eng.run("帮我改 main.py")
        user_msg = next(m for m in recorder.seen_messages if m["role"] == "user")
        # 空目录 → 追加 list_files 强制提示
        assert "list_files" in user_msg["content"]
        assert "[项目结构预览" not in user_msg["content"]
