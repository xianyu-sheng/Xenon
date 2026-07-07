"""
P3-Q4 code_index / project_context 持久化 + mtime 增量 + 检测安全测试。

覆盖（§8.9.1/5 / §8.21.1/2/3）：
- §8.9.1：code_index 可选磁盘缓存 + mtime/size 增量索引（命中跳过 AST 重解析）。
- §8.9.5：project_context refresh 基于 mtime 复用关键文件内容。
- §8.21.1：detect 限制向上层数 + 遇 $HOME 停（家目录 .git 不当项目根）。
- §8.21.2：_build_file_tree 不跟随符号链接（防循环/扫整盘）。
- §8.21.3：_EXCLUDE_DIRS 的 *.egg-info glob 真正生效（fnmatch）。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from omniagent.repl.project_context import ProjectContext, _is_excluded_dir
from omniagent.utils.code_index import CodeIndex


# ── §8.21.3：fnmatch glob 排除 ───────────────────────────────


class TestExcludeDirFnmatch:
    def test_literal_dirs_excluded(self):
        assert _is_excluded_dir("node_modules")
        assert _is_excluded_dir("__pycache__")
        assert _is_excluded_dir(".git")

    def test_egg_info_glob_now_matches(self):
        """§8.21.3：`*.egg-info` 此前用 `name in set` 永不命中，现在 fnmatch 生效。"""
        assert _is_excluded_dir("foo.egg-info")
        assert _is_excluded_dir("my.pkg.egg-info")

    def test_normal_dir_not_excluded(self):
        assert not _is_excluded_dir("src")
        assert not _is_excluded_dir("tests")


# ── §8.21.2：符号链接不跟随 ─────────────────────────────────


class TestSymlinkSkip:
    def test_file_tree_skips_dir_symlink(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("")
        (tmp_path / "real").mkdir()
        (tmp_path / "real" / "x.py").write_text("x = 1")
        (tmp_path / "link").symlink_to(tmp_path / "real")

        pc = ProjectContext()
        pc.detect(tmp_path)
        assert "link" not in pc.file_tree
        assert "real/" in pc.file_tree

    def test_file_tree_symlink_cycle_no_hang(self, tmp_path):
        """指向根的循环符号链接不应导致无限递归。"""
        (tmp_path / "pyproject.toml").write_text("")
        (tmp_path / "loop").symlink_to(tmp_path)  # 指向根 → 循环

        pc = ProjectContext()
        pc.detect(tmp_path)  # 不应挂起
        assert "loop" not in pc.file_tree

    def test_file_tree_skips_file_symlink(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("")
        (tmp_path / "real.py").write_text("x = 1")
        (tmp_path / "link.py").symlink_to(tmp_path / "real.py")

        pc = ProjectContext()
        pc.detect(tmp_path)
        assert "link.py" not in pc.file_tree
        assert "real.py" in pc.file_tree


# ── §8.21.1：detect 上爬边界 + $HOME 停 ─────────────────────


class TestDetectBoundary:
    def test_detect_stops_at_home(self, tmp_path, monkeypatch):
        """家目录有 .git（dotfiles）时，不从子目录上爬把家目录当项目根。"""
        home = tmp_path / "fakehome"
        home.mkdir()
        (home / ".git").mkdir()  # 家目录是 dotfiles git 仓库
        proj = home / "code" / "myproj"
        proj.mkdir(parents=True)

        monkeypatch.setattr(os.path, "expanduser", lambda p: str(home) if p == "~" else p)

        pc = ProjectContext()
        found = pc.detect(proj)
        # 停在家目录（不当项目根），回退到 cwd，type=unknown
        assert found is False
        assert pc.root == proj.resolve()
        assert pc.project_type == "unknown"

    def test_detect_finds_marker_below_home(self, tmp_path, monkeypatch):
        """$HOME 与 cwd 之间的真实项目标记仍能被检测到。"""
        home = tmp_path / "fakehome"
        home.mkdir()
        (home / ".git").mkdir()
        proj = home / "code" / "myproj"
        proj.mkdir(parents=True)
        (proj / "pyproject.toml").write_text("[project]\nname='x'")

        monkeypatch.setattr(os.path, "expanduser", lambda p: str(home) if p == "~" else p)

        pc = ProjectContext()
        found = pc.detect(proj)
        assert found is True
        assert pc.project_type == "python"
        assert pc.root == proj.resolve()

    def test_detect_caps_upward_levels(self, tmp_path):
        """无标记时最多上爬 5 层后回退，不无限上爬。"""
        deep = tmp_path
        for i in range(7):
            deep = deep / f"d{i}"
        deep.mkdir(parents=True)

        pc = ProjectContext()
        found = pc.detect(deep)
        assert found is False
        assert pc.project_type == "unknown"
        assert pc.root == deep.resolve()

    def test_git_dir_still_detected_when_not_home(self, tmp_path):
        """非家目录的 .git 仍作为项目根（回归保护）。"""
        (tmp_path / ".git").mkdir()
        pc = ProjectContext()
        assert pc.detect(tmp_path) is True
        assert pc.root == tmp_path.resolve()


# ── §8.9.5：project_context 关键文件 mtime 增量 ─────────────


class TestProjectContextKeyFileMtime:
    def test_detect_loads_key_files(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('name = "v1"')
        pc = ProjectContext()
        pc.detect(tmp_path)
        assert "pyproject.toml" in pc.key_files
        assert "v1" in pc.key_files["pyproject.toml"]
        assert "pyproject.toml" in pc._key_file_mtimes

    def test_refresh_skips_unchanged_key_file_reads(self, tmp_path, monkeypatch):
        """mtime 未变 → refresh 不重读关键文件。"""
        (tmp_path / "pyproject.toml").write_text('name = "v1"')
        pc = ProjectContext()
        pc.detect(tmp_path)

        import pathlib
        calls = {"n": 0}
        orig = pathlib.Path.read_text

        def spy(self, *a, **k):
            calls["n"] += 1
            return orig(self, *a, **k)

        monkeypatch.setattr(pathlib.Path, "read_text", spy)
        n_before = calls["n"]
        pc.refresh()
        # pyproject.toml mtime 未变 → 复用缓存，不应触发 read_text
        assert calls["n"] == n_before

    def test_refresh_picks_up_changed_key_file(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('name = "v1"')
        pc = ProjectContext()
        pc.detect(tmp_path)
        assert "v1" in pc.key_files["pyproject.toml"]

        # 改内容 + 主动推进 mtime
        (tmp_path / "pyproject.toml").write_text('name = "v2-with-more-chars"')
        future = time.time() + 10
        os.utime(tmp_path / "pyproject.toml", (future, future))

        pc.refresh()
        assert "v2-with-more-chars" in pc.key_files["pyproject.toml"]

    def test_refresh_drops_deleted_key_file(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("a = 1")
        (tmp_path / "README.md").write_text("# hi")
        pc = ProjectContext()
        pc.detect(tmp_path)
        assert "README.md" in pc.key_files

        (tmp_path / "README.md").unlink()
        pc.refresh()
        assert "README.md" not in pc.key_files
        assert "pyproject.toml" in pc.key_files


# ── §8.9.1：code_index 磁盘缓存 + mtime/size 增量 ───────────


class TestCodeIndexCache:
    def test_no_cache_dir_writes_nothing(self, tmp_path):
        (tmp_path / "a.py").write_text("def f(): pass")
        idx = CodeIndex(tmp_path)  # 默认 cache_dir=None
        idx.build()
        # 不应创建任何缓存目录/文件
        assert not (tmp_path / ".omniagent").exists()

    def test_build_writes_cache_file(self, tmp_path):
        (tmp_path / "a.py").write_text("def f(): pass")
        cache_dir = tmp_path / "cache"
        idx = CodeIndex(tmp_path, cache_dir=cache_dir)
        count = idx.build()
        assert count == 1
        cache_files = list(cache_dir.glob("codeindex-*.json"))
        assert len(cache_files) == 1
        data = json.loads(cache_files[0].read_text(encoding="utf-8"))
        assert data["version"] == 1
        assert len(data["files"]) == 1

    def test_cache_hit_skips_reparse(self, tmp_path, monkeypatch):
        """未变文件命中缓存 → 第二次 build 不调用 index_file（不重解析）。"""
        (tmp_path / "a.py").write_text("def f(): pass")
        cache_dir = tmp_path / "cache"

        calls = {"n": 0}
        orig = CodeIndex.index_file

        def spy(self, fp):
            calls["n"] += 1
            return orig(self, fp)

        monkeypatch.setattr(CodeIndex, "index_file", spy)

        CodeIndex(tmp_path, cache_dir=cache_dir).build()
        assert calls["n"] == 1

        # 第二次 build：文件未变 → 全部命中缓存，不调用 index_file
        CodeIndex(tmp_path, cache_dir=cache_dir).build()
        assert calls["n"] == 1  # 仍为 1，没有新增解析

    def test_mtime_change_reparses(self, tmp_path):
        (tmp_path / "a.py").write_text("def f(): pass")
        cache_dir = tmp_path / "cache"
        CodeIndex(tmp_path, cache_dir=cache_dir).build()

        # 改内容（长度不同 → size 变）+ 推进 mtime
        (tmp_path / "a.py").write_text("def gg(): pass")
        future = time.time() + 10
        os.utime(tmp_path / "a.py", (future, future))

        idx2 = CodeIndex(tmp_path, cache_dir=cache_dir)
        idx2.build()
        assert idx2.find_definition("gg")  # 新符号
        assert not idx2.find_definition("f")  # 旧符号已移除

    def test_cache_restores_searchable_symbols(self, tmp_path):
        (tmp_path / "a.py").write_text("def hello(): pass\nclass Foo: pass")
        cache_dir = tmp_path / "cache"
        CodeIndex(tmp_path, cache_dir=cache_dir).build()

        # 新实例从缓存恢复，不重解析，符号仍可搜
        idx2 = CodeIndex(tmp_path, cache_dir=cache_dir)
        idx2.build()
        assert any(r.name == "hello" for r in idx2.search("hello"))
        assert any(r.name == "Foo" for r in idx2.search("Foo"))

    def test_corrupt_cache_falls_back_to_full_build(self, tmp_path):
        (tmp_path / "a.py").write_text("def f(): pass")
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        # 写入损坏 JSON
        bad = cache_dir / "codeindex-deadbeef.json"
        # 先用一个真实 build 确定正确的缓存文件名
        CodeIndex(tmp_path, cache_dir=cache_dir).build()
        real = next(cache_dir.glob("codeindex-*.json"))
        real.write_text("{ not valid json", encoding="utf-8")

        idx = CodeIndex(tmp_path, cache_dir=cache_dir)
        count = idx.build()  # 不应崩溃
        assert count == 1
        assert idx.find_definition("f")  # 仍能索引到符号

    def test_deleted_file_removed_from_cache(self, tmp_path):
        (tmp_path / "a.py").write_text("def f(): pass")
        (tmp_path / "b.py").write_text("def g(): pass")
        cache_dir = tmp_path / "cache"
        CodeIndex(tmp_path, cache_dir=cache_dir).build()

        (tmp_path / "a.py").unlink()
        idx2 = CodeIndex(tmp_path, cache_dir=cache_dir)
        idx2.build()
        # a.py 已删除，其符号不应残留
        assert not idx2.find_definition("f")
        assert idx2.find_definition("g")
        # 缓存中也应清理
        data = json.loads(next(cache_dir.glob("codeindex-*.json")).read_text(encoding="utf-8"))
        assert not any(p.endswith("a.py") for p in data["files"])
