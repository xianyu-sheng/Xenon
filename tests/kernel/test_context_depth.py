"""#408 / #409: the context meter and the receipt content must not lie.

Dogfood found a 20K-character kernel prompt displayed as "context 0.3%" (the
meter was reading the legacy chat history, which a kernel turn never grows),
and a 22 KB README silently clipped to 8 KB with no way for the model to learn
that it had missed two thirds of the file.
"""

from __future__ import annotations

from types import SimpleNamespace

from xenon.kernel.usage import UsageTotals
from xenon.kernel.view.base import _RECEIPT_CONTENT_CHARS, _effect_summary


def test_usage_tracks_the_depth_of_the_last_request() -> None:
    usage = UsageTotals()
    usage.add({"prompt_tokens": 5000, "completion_tokens": 300})
    usage.add({"prompt_tokens": 6100, "completion_tokens": 120})

    assert usage.total == 11520, "the cumulative bill is unchanged"
    assert usage.last_prompt == 6100, "the window depth is the *last* prompt"
    assert usage.last_completion == 120


def test_the_status_snapshot_publishes_context_depth(monkeypatch) -> None:
    from tests.kernel.test_north_star import _runtime
    from xenon.ledger.store import Ledger

    runtime = _runtime(Ledger.in_memory("ctx-depth"))
    runtime.note_usage({"prompt_tokens": 4321, "completion_tokens": 10})

    snapshot = runtime.status_snapshot()

    assert snapshot["context_tokens"] == 4321
    assert snapshot["tokens"] == 4331, "cumulative stays available too"


def test_the_progress_hook_feeds_the_meter_with_depth() -> None:
    """A kernel turn must update the same number the status bar reads."""
    from xenon.repl.runtime_adapter import kernel_progress_reporter

    recorded: list[tuple[int, int, int]] = []
    ctx_mgr = SimpleNamespace(
        record_real_usage=lambda p, c, t=None: recorded.append((p, c, t or 0))
    )
    runtime = SimpleNamespace(
        _usage=SimpleNamespace(last_prompt=9876),
        status_snapshot=lambda: {"phase": "running", "steps": 1},
    )
    repl = SimpleNamespace(
        console=SimpleNamespace(print=lambda *a, **k: None),
        status_bar=SimpleNamespace(set_kernel_status=lambda s: None),
        ctx_mgr=ctx_mgr,
        _active_runtime=runtime,
    )

    kernel_progress_reporter(repl)(0.4, 100)

    assert recorded == [(9876, 0, 9876)], "context %% means window depth"


def test_a_clipped_receipt_says_it_was_clipped() -> None:
    raw = "x" * (_RECEIPT_CONTENT_CHARS + 2500)

    summary = _effect_summary({"type": "network", "content": raw})

    # `_clip` keeps the first N characters and appends an ellipsis.
    assert summary["content"].endswith("...")
    assert len(summary["content"]) == _RECEIPT_CONTENT_CHARS + 3
    assert summary["truncated"] == {
        "shown_chars": _RECEIPT_CONTENT_CHARS,
        "total_chars": len(raw),
        "remaining_chars": len(raw) - _RECEIPT_CONTENT_CHARS,
    }


def test_a_short_receipt_carries_no_truncation_noise() -> None:
    summary = _effect_summary({"type": "read", "content": "small"})

    assert summary["content"] == "small"
    assert "truncated" not in summary


def test_read_advertises_paging() -> None:
    """The tool has supported start_line/max_lines all along; the model was
    never told, so a clipped file had no documented way to be continued."""
    from xenon.catalog.specs import CORE_SPECS

    read = next(spec for spec in CORE_SPECS if spec.name == "read")

    assert "file_path" in read.params_schema
    assert "start_line" in read.params_schema
    assert "max_lines" in read.params_schema
    assert "page" in read.description.lower() or "cut" in read.description.lower()


def _think_step_runtime():
    from tests.kernel.test_north_star import _runtime
    from xenon.ledger.store import Ledger

    return _runtime(Ledger.in_memory("think-step")), Ledger


def test_a_reasoning_step_does_not_block_a_tool_call() -> None:
    """#413: verification runs *after* the work, so nothing may pre-empt it.

    The first fix for #405 refused a tool call aimed at a `kind=think` step.
    That inverted the design: Xenon exists to check what the agent did, and an
    agent that cannot act produces nothing to check.  The plan's step kind is a
    hint for the view (see the next test); it is never a gate.
    """
    import inspect

    from xenon.kernel import runtime as runtime_module

    assert not hasattr(runtime_module.LoopRuntime, "_reasoning_step_guard"), (
        "the dispatch path must not consult the plan's step kind"
    )
    source = inspect.getsource(runtime_module.LoopRuntime._call_tool)
    assert "think" not in source


def test_the_view_tells_the_model_what_a_reasoning_step_expects() -> None:
    from xenon.catalog.catalog import build_catalog
    from xenon.kernel.contracts import StepContract, StepKind, TaskSpec
    from xenon.kernel.view.base import build_model_view
    from xenon.ledger.store import Ledger

    view = build_model_view(
        spec=TaskSpec(task_id="t", objective="summarise", workspace="."),
        catalog=build_catalog("core"),
        ledger=Ledger.in_memory("t"),
        active_step=StepContract(id=9, kind=StepKind.THINK, targets=()),
    )

    assert "reasoning step" in view.system
    assert "Do not call a tool" in view.system
