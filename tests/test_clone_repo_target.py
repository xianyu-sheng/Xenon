"""D-3 回归：clone_repo 支持显式 target_dir（克隆到工作区），且不覆盖用户内容。"""

from __future__ import annotations

from xenon.engine.context import AgentContext
from xenon.nodes.tool_node import ToolNode


def test_clone_repo_rejects_non_empty_non_git_target(tmp_path) -> None:
    target = tmp_path / "work"
    target.mkdir()
    (target / "notes.txt").write_text("keep me", encoding="utf-8")

    node = ToolNode(
        "t",
        action_type="clone_repo",
        repo="owner/repo",
        target_dir=str(target),
    )
    result = node.execute(AgentContext({}))

    assert result["success"] is False
    assert "非空" in result["error"]
    # 用户内容未被删除
    assert (target / "notes.txt").read_text(encoding="utf-8") == "keep me"


def test_clone_repo_accepts_existing_git_checkout(tmp_path, monkeypatch) -> None:
    """已有 git checkout 的显式目标不应被 fetch/merge（那是用户的仓库）。"""

    target = tmp_path / "repo"
    (target / ".git").mkdir(parents=True)
    (target / "main.py").write_text("print(1)", encoding="utf-8")

    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = "https://github.com/owner/repo.git\n"
        stderr = ""

    def _fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return _Result()

    monkeypatch.setattr("subprocess.run", _fake_run)
    monkeypatch.setattr(
        ToolNode, "_analyze_cloned_repo", lambda self, *a, **k: {
            "action_type": "clone_repo",
            "repo": "owner/repo",
            "success": True,
            "content": "ok",
        }
    )

    node = ToolNode(
        "t",
        action_type="clone_repo",
        repo="owner/repo",
        target_dir=str(target),
    )
    result = node.execute(AgentContext({}))

    assert result["success"] is True
    # 只允许只读的 remote get-url，不允许 fetch / merge / clone
    flat = " ".join(" ".join(c) for c in calls)
    assert "remote get-url" in flat
    assert not any("fetch" in c for c in calls)
    assert not any("merge" in c for c in calls)
    assert not any(c[:2] == ["git", "clone"] for c in calls)


def test_clone_repo_params_survive_normalization() -> None:
    params = ToolNode.normalize_params(
        {
            "repo": "o/r",
            "target_dir": "work/repo",
            "pr_title": "t",
            "pr_body": "b",
            "base_branch": "main",
        },
        action_type="clone_repo",
    )
    assert params["target_dir"] == "work/repo"
    assert params["pr_title"] == "t"
    assert params["pr_body"] == "b"
    assert params["base_branch"] == "main"
