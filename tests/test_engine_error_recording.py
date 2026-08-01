"""引擎错误记录契约测试（P2 静默吞异常根因修复）。

引擎抛异常时 user 消息已进 history 但没有对应 assistant 响应。这种 user-only
序列会污染下一轮上下文和缓存前缀。此前 REPL 里有四处处理同一失败模式，只有一处
尝试 ``trim_last_user()`` 恢复，且全部静默 ``pass``——历史损坏却无任何线索。

``REPL._record_engine_error`` 把这四处收敛成一个契约：写占位 → 失败则清理孤立
user → 再失败则留日志。本测试锁定该契约。
"""

from __future__ import annotations

import logging

from xenon.repl.repl import REPL


class _Recorder:
    """最小 ctx_mgr 替身，可按需让任一步失败。"""

    def __init__(self, *, add_fails=False, trim_fails=False) -> None:
        self.add_fails = add_fails
        self.trim_fails = trim_fails
        self.added: list[tuple[str, str | None]] = []
        self.trimmed = 0

    def add_assistant_message(self, content, *, model_used=None):
        if self.add_fails:
            raise RuntimeError("add boom")
        self.added.append((content, model_used))

    def trim_last_user(self):
        self.trimmed += 1
        if self.trim_fails:
            raise RuntimeError("trim boom")
        return "user msg"


def _repl(ctx) -> REPL:
    repl = REPL.__new__(REPL)
    repl.ctx_mgr = ctx
    return repl


class TestRecordEngineError:
    def test_writes_placeholder_on_happy_path(self):
        ctx = _Recorder()
        _repl(ctx)._record_engine_error("[错误] boom", "m1")
        assert ctx.added == [("[错误] boom", "m1")]
        assert ctx.trimmed == 0, "占位成功时不应再清理 user 消息"

    def test_falls_back_to_trim_when_placeholder_fails(self):
        ctx = _Recorder(add_fails=True)
        _repl(ctx)._record_engine_error("[错误] boom", "m1")
        assert ctx.added == []
        assert ctx.trimmed == 1, "占位失败必须回退清理孤立 user 消息"

    def test_logs_warning_when_placeholder_fails(self, caplog):
        ctx = _Recorder(add_fails=True)
        with caplog.at_level(logging.WARNING):
            _repl(ctx)._record_engine_error("[错误] boom", "m1")
        assert any(
            "回退清理孤立 user" in r.getMessage()
            for r in caplog.records
        )

    def test_logs_error_when_both_steps_fail(self, caplog):
        ctx = _Recorder(add_fails=True, trim_fails=True)
        with caplog.at_level(logging.ERROR):
            # 绝不能抛出——这是最后的兜底路径
            _repl(ctx)._record_engine_error("[错误] boom", "m1")
        assert any(r.levelno >= logging.ERROR for r in caplog.records), (
            "两步都失败必须留下 ERROR 日志，而不是静默"
        )

    def test_never_raises_even_when_everything_fails(self):
        ctx = _Recorder(add_fails=True, trim_fails=True)
        # 若这里抛出，会盖掉真正的引擎异常
        _repl(ctx)._record_engine_error("[错误] boom", None)


class TestNoSilentSwallows:
    def test_repl_has_no_bare_except_pass(self):
        """回归门禁：repl.py 不允许出现静默吞异常。

        静默 ``except: pass`` 是「用起来突然不工作但查不出原因」这类问题的根源。
        新增时必须至少留一条日志。
        """
        import pathlib
        import re

        src = pathlib.Path(
            REPL.__module__.replace(".", "/") + ".py"
        )
        if not src.exists():  # pragma: no cover - 包安装布局兜底
            import xenon.repl.repl as mod
            src = pathlib.Path(mod.__file__)
        lines = src.read_text(encoding="utf-8").splitlines()
        offenders = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            if re.match(r"except\b.*:\s*pass\s*$", stripped):
                offenders.append(i + 1)
            elif re.match(r"except\b.*:\s*$", stripped):
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                if j < len(lines) and lines[j].strip() == "pass":
                    offenders.append(i + 1)
        assert not offenders, (
            f"repl.py 出现静默吞异常，行号 {offenders}；"
            "请至少记录一条 logger 日志说明失败原因"
        )


def _repl_source_lines() -> list[str]:
    """读取 repl.py 源码行，供静态门禁使用。"""
    import pathlib

    import xenon.repl.repl as mod

    return pathlib.Path(mod.__file__).read_text(encoding="utf-8").splitlines()


class TestNoDuplicateMethodDefinitions:
    """回归门禁：类内不得重复定义同名方法。

    历史 bug：``_record_engine_error`` 在 repl.py 里定义了两次（``:966`` 三参数
    版与 ``:2073`` 两参数版）。Python 后定义覆盖前定义，于是 5 处按三参数签名
    调用的地方必抛 ``TypeError``——而它们全在引擎的 ``except`` 块里，导致异常
    处理器自己再抛异常，既盖掉原始失败原因，又让 ``_record_engine_error`` 的
    收尾逻辑完全跑不到，留下污染下一轮缓存前缀的孤立 user 消息。

    单测只覆盖了能用的那条路（两参数形式），所以全量绿也没抓到。此处改用静态
    扫描，从结构上防止同类问题。
    """

    def test_repl_defines_no_method_twice(self):
        import ast

        import xenon.repl.repl as mod

        tree = ast.parse("\n".join(_repl_source_lines()))
        duplicates: dict[str, list[tuple[str, list[int]]]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            seen: dict[str, list[int]] = {}
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    seen.setdefault(item.name, []).append(item.lineno)
            dupes = [(n, ls) for n, ls in seen.items() if len(ls) > 1]
            if dupes:
                duplicates[node.name] = dupes

        assert not duplicates, (
            f"{mod.__file__} 中存在重复方法定义（后者覆盖前者，静默生效）: "
            f"{duplicates}"
        )


class TestRecordEngineErrorCallSites:
    """回归门禁：所有调用点必须匹配生效签名。"""

    def test_every_call_site_passes_two_arguments(self):
        import ast

        import xenon.repl.repl as mod

        tree = ast.parse("\n".join(_repl_source_lines()))
        bad: list[tuple[int, int]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr == "_record_engine_error":
                n_args = len(node.args) + len(node.keywords)
                if n_args != 2:
                    bad.append((node.lineno, n_args))

        assert not bad, (
            "_record_engine_error(message, model_used) 只收 2 个参数，"
            f"以下调用点参数个数不符 (行号, 个数): {bad}。"
            f"文件: {mod.__file__}"
        )

    def test_signature_is_message_and_model(self):
        """签名本身也钉住，避免有人改回三参数版又不改调用点。"""
        import inspect

        from xenon.repl.repl import REPL

        params = list(inspect.signature(REPL._record_engine_error).parameters)
        assert params == ["self", "message", "model_used"], (
            f"签名已变更为 {params}；若确需修改，请同步全部调用点并更新本测试"
        )
