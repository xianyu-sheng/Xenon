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
