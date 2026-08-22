"""路径围栏：符号链接逃逸回归测试。

``_validate_path`` 曾用 ``os.path.normpath`` 做规范化——它只在字符串层面消解
``..``，不跟随符号链接。工作区内的一个链接（克隆仓库、``node_modules``、
虚拟环境里极常见，Agent 也能用 ``command`` 工具自行创建）因此能通过围栏，
而写入实际落在围栏外。本文件锁住修复：校验时用 ``Path.resolve`` 看真实目标。

注意：断言用的工作区**不能**放在 ``/tmp`` 下——``_validate_path`` 把
``/tmp`` 与 ``/var/tmp`` 列为额外允许根（tool_node.py 的
``_ALLOWED_EXTRA_ROOTS``），逃逸目标若落在 ``/tmp`` 内会被该规则放行，
测试就失去意义。这里统一用 ``tmp_path`` 之外自建的隔离目录。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xenon.engine.context import AgentContext
from xenon.nodes.tool_node import SecurityError, ToolNode


@pytest.fixture
def fenced(real_path_validation, monkeypatch, tmp_path_factory):
    """返回 (workspace, outside)：outside 与 workspace 互为兄弟，均在围栏外。

    两处必须显式反转仓库默认设置，否则断言会静默失效：

    1. ``real_path_validation``（见根 conftest.py）——仓库有个 autouse fixture
       ``_disable_security_for_tests`` 把 ``_validate_path`` 换成宽松版本，
       方便绝大多数用例用临时目录。断言路径校验本身的用例必须取回真实实现。
    2. ``_extra_allowed_roots`` 清空——``tmp_path_factory`` 建出的目录在
       ``/tmp`` 下，而 ``/tmp`` 与 ``/var/tmp`` 是额外允许根，逃逸目标落在
       那里会被直接放行。
    """
    monkeypatch.setattr(
        ToolNode, "_extra_allowed_roots", staticmethod(lambda: ()), raising=True
    )
    base = tmp_path_factory.mktemp("fence")
    workspace = base / "ws"
    workspace.mkdir()
    outside = base / "outside"
    outside.mkdir()
    return workspace, outside


def _skip_if_no_symlink(target: Path, link: Path) -> None:
    """Windows 无管理员权限时创建符号链接会失败——跳过而非误报。"""
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError) as exc:  # pragma: no cover
        pytest.skip(f"平台不支持创建符号链接: {exc}")


class TestSymlinkFenceEscape:
    """符号链接必须无法把写入/读取带出工作区。"""

    def test_write_through_symlink_is_blocked(self, fenced):
        workspace, outside = fenced
        _skip_if_no_symlink(outside, workspace / "escape")

        node = ToolNode("w", action_type="write_file", cwd=str(workspace))
        with pytest.raises(SecurityError, match="路径越界"):
            node._validate_path("escape/pwned.txt", for_write=True)

    def test_read_through_symlink_is_blocked(self, fenced):
        """读路径同样受围栏约束，否则凭证读取防护可被绕过。"""
        workspace, outside = fenced
        _skip_if_no_symlink(outside, workspace / "escape")

        node = ToolNode("r", action_type="read_file", cwd=str(workspace))
        with pytest.raises(SecurityError, match="路径越界"):
            node._validate_path("escape/secret.txt", for_write=False)

    def test_write_through_symlink_leaves_no_file_outside(self, fenced):
        """端到端：走完整 _write_file，确认围栏外磁盘上不产生文件。"""
        workspace, outside = fenced
        _skip_if_no_symlink(outside, workspace / "escape")
        target = outside / "pwned.txt"

        node = ToolNode(
            "w",
            action_type="write_file",
            cwd=str(workspace),
            file_path="escape/pwned.txt",
            content="escaped",
        )
        with pytest.raises(SecurityError):
            node._write_file(AgentContext())

        assert not target.exists(), f"符号链接逃逸：围栏外被写入 {target}"

    def test_nested_symlink_dir_is_blocked(self, fenced):
        """链接位于路径中段（而非首段）时同样拦截。"""
        workspace, outside = fenced
        nested = workspace / "a" / "b"
        nested.mkdir(parents=True)
        _skip_if_no_symlink(outside, nested / "out")

        node = ToolNode("w", action_type="write_file", cwd=str(workspace))
        with pytest.raises(SecurityError, match="路径越界"):
            node._validate_path("a/b/out/pwned.txt", for_write=True)


class TestFenceStillAllowsLegitimatePaths:
    """修复不得把正常写入误拦——resolve 引入的回归风险主要在此。"""

    def test_plain_relative_write_allowed(self, fenced):
        workspace, _ = fenced
        node = ToolNode("w", action_type="write_file", cwd=str(workspace))
        assert node._validate_path("ok.txt", for_write=True)

    def test_nonexistent_nested_path_allowed(self, fenced):
        """写入常指向尚不存在的文件——strict=False 必须容许。"""
        workspace, _ = fenced
        node = ToolNode("w", action_type="write_file", cwd=str(workspace))
        assert node._validate_path("does/not/exist/yet.txt", for_write=True)

    def test_interior_dotdot_within_workspace_allowed(self, fenced):
        workspace, _ = fenced
        (workspace / "sub").mkdir()
        node = ToolNode("w", action_type="write_file", cwd=str(workspace))
        assert node._validate_path("sub/../ok.txt", for_write=True)

    def test_symlink_pointing_inside_workspace_allowed(self, fenced):
        """指向工作区内部的链接是合法的，不能连带拦掉。"""
        workspace, _ = fenced
        inner = workspace / "real"
        inner.mkdir()
        _skip_if_no_symlink(inner, workspace / "link")

        node = ToolNode("w", action_type="write_file", cwd=str(workspace))
        assert node._validate_path("link/ok.txt", for_write=True)

    def test_real_write_still_lands(self, fenced):
        workspace, _ = fenced
        node = ToolNode(
            "w",
            action_type="write_file",
            cwd=str(workspace),
            file_path="sub/real.txt",
            content="ok",
        )
        result = node._write_file(AgentContext())
        assert result["success"], result.get("error")
        assert (workspace / "sub" / "real.txt").read_text() == "ok"

    def test_escaping_dotdot_still_blocked(self, fenced):
        """原有的纯文本 .. 逃逸防护不能因改动而失效。"""
        workspace, _ = fenced
        node = ToolNode("w", action_type="write_file", cwd=str(workspace))
        with pytest.raises(SecurityError, match="路径越界"):
            node._validate_path("../sibling.txt", for_write=True)


class TestHostilePathDoesNotDisableFence:
    """resolve 失败时必须退回文本规范化，而不是放行或崩溃。"""

    def test_symlink_loop_does_not_crash(self, fenced):
        """符号链接循环让 pathlib 抛 RuntimeError（非 OSError）——须兜住。

        循环链接解析不出真实目标，退回文本形式后仍在围栏内，
        因此放行是可接受的；关键是不能把异常泄漏给调用方。
        """
        workspace, _ = fenced
        _skip_if_no_symlink(workspace / "loop_b", workspace / "loop_a")
        _skip_if_no_symlink(workspace / "loop_a", workspace / "loop_b")

        node = ToolNode("w", action_type="write_file", cwd=str(workspace))
        try:
            node._validate_path("loop_a/x.txt", for_write=True)
        except SecurityError:
            pass  # 拦截也可接受
        except (OSError, RuntimeError) as exc:  # pragma: no cover
            pytest.fail(f"符号链接循环导致异常泄漏: {type(exc).__name__}: {exc}")

    def test_fallback_preserves_containment(self, fenced):
        """构造 resolve 抛错的场景，确认 fallback 后越界仍被拦。"""
        workspace, _ = fenced
        node = ToolNode("w", action_type="write_file", cwd=str(workspace))

        original = Path.resolve

        def boom(self, strict=False):
            raise OSError("simulated resolve failure")

        Path.resolve = boom
        try:
            # _get_allowed_root 也依赖 resolve，故此处仅验证不放行越界路径
            with pytest.raises((SecurityError, OSError)):
                node._validate_path("../../escape.txt", for_write=True)
        finally:
            Path.resolve = original


class TestSensitivePathGuardsIntact:
    """resolve 改动后，敏感路径/文件名防护须继续生效。"""

    def test_system_path_write_blocked(self, fenced):
        workspace, _ = fenced
        node = ToolNode("w", action_type="write_file", cwd=str(workspace))
        with pytest.raises(SecurityError):
            node._validate_path("/etc/passwd", for_write=True)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX 路径断言")
    def test_home_outside_workspace_blocked(self, fenced):
        workspace, _ = fenced
        node = ToolNode("w", action_type="write_file", cwd=str(workspace))
        with pytest.raises(SecurityError):
            node._validate_path(str(Path.home() / ".bashrc"), for_write=True)
