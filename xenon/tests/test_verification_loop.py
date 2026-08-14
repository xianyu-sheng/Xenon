"""Tests for VerificationLoop — engine-layer cross-round verification loop."""

from __future__ import annotations

from xenon.engine.tool_tracker import ToolExecutionTracker
from xenon.engine.execution_evidence import ExecutionEvidence
from xenon.engine.verification_loop import (
    VerificationLoop,
    _should_verify,
    _extract_failure_summary,
)


# ── Helpers ────────────────────────────────────────────────────


def _make_tracker(
    write_success: bool = False,
    test_success: bool = False,
    test_fail: bool = False,
    write_path: str = "/tmp/test/file.py",
    test_cmd: str = "pytest tests/",
    fail_error: str = "AssertionError: assert 1 == 2",
) -> ToolExecutionTracker:
    tracker = ToolExecutionTracker()
    if write_success:
        tracker.record(
            tool_name="write_file",
            params={"file_path": write_path, "content": "x = 1"},
            success=True,
            result_summary="wrote file.py",
        )
    if test_success:
        tracker.record(
            tool_name="command",
            params={"command": test_cmd},
            success=True,
            result_summary="4 passed",
        )
    if test_fail:
        tracker.record(
            tool_name="command",
            params={"command": test_cmd},
            success=False,
            result_summary=fail_error,
            error=fail_error,
        )
    return tracker


def _make_evidence(write: bool = False, test_pass: bool = False,
                   test_fail: bool = False,
                   fail_error: str = "AssertionError: assert 1 == 2",
                   write_path: str = "/tmp/test/file.py") -> ExecutionEvidence:
    tracker = _make_tracker(
        write_success=write, test_success=test_pass, test_fail=test_fail,
        write_path=write_path, fail_error=fail_error,
    )
    return ExecutionEvidence.capture(tracker, workspace_root=None)


# ── _should_verify ─────────────────────────────────────────────


class TestShouldVerify:
    def test_write_with_failed_test(self):
        ev = _make_evidence(write=True, test_fail=True)
        assert _should_verify(ev, "fix the bug") is True

    def test_write_with_passing_test_skips(self):
        ev = _make_evidence(write=True, test_pass=True)
        assert _should_verify(ev, "fix the bug") is False

    def test_no_write_skips(self):
        ev = _make_evidence(test_fail=True)
        assert _should_verify(ev, "fix the bug") is False

    def test_readonly_task_skips(self):
        ev = _make_evidence(write=True, test_fail=True)
        assert _should_verify(ev, "what is the answer?") is False

    def test_no_failed_test_skips(self):
        ev = _make_evidence(write=True, test_pass=False)
        assert _should_verify(ev, "fix the bug") is False


# ── _extract_failure_summary ──────────────────────────────────


class TestExtractFailureSummary:
    def test_extracts_assertion_error(self):
        ev = _make_evidence(write=True, test_fail=True,
                            fail_error="AssertionError: assert 1 == 2\n  x = 1\n  y = 2")
        summary = _extract_failure_summary(ev)
        assert "AssertionError" in summary
        assert "assert 1" in summary

    def test_multiple_failures_joined(self):
        tracker = ToolExecutionTracker()
        tracker.record(tool_name="write_file", params={"file_path": "a.py"},
                       success=True, result_summary="wrote")
        tracker.record(tool_name="command", params={"command": "pytest t1.py"},
                       success=False, error="Error: assert x", result_summary="")
        tracker.record(tool_name="command", params={"command": "pytest t2.py"},
                       success=False, error="Error: assert y", result_summary="")
        ev = ExecutionEvidence.capture(tracker)
        summary = _extract_failure_summary(ev)
        assert "assert x" in summary
        assert "assert y" in summary

    def test_no_failures_returns_placeholder(self):
        ev = _make_evidence(write=True, test_pass=True)
        summary = _extract_failure_summary(ev)
        assert "no structured" in summary


# ── VerificationLoop (core) ────────────────────────────────────


class TestVerificationLoop:
    def test_no_verify_when_inactive(self):
        loop = VerificationLoop(max_rounds=8)
        ev = _make_evidence(write=True, test_fail=True)
        assert loop.feed(ev, "fix it") is None

    def test_verify_returns_repair_prompt(self):
        loop = VerificationLoop(max_rounds=8)
        loop._active = True
        ev = _make_evidence(write=True, test_fail=True)
        prompt = loop.feed(ev, "fix the bug")
        assert prompt is not None
        assert "验证闭环" in prompt
        assert "AssertionError" in prompt

    def test_verify_skips_when_no_failure(self):
        loop = VerificationLoop(max_rounds=8)
        loop._active = True
        ev = _make_evidence(write=True, test_pass=True)
        assert loop.feed(ev, "fix it") is None

    def test_verify_skips_readonly_task(self):
        loop = VerificationLoop(max_rounds=8)
        loop._active = True
        ev = _make_evidence(write=True, test_fail=True)
        assert loop.feed(ev, "what is x?") is None

    def test_max_rounds_enforced(self):
        loop = VerificationLoop(max_rounds=2)
        loop._active = True
        ev = _make_evidence(write=True, test_fail=True)
        # Round 1
        p1 = loop.feed(ev, "fix it")
        assert p1 is not None
        loop.record_outcome(ev, "still_failing")
        assert loop.should_continue
        # Round 2
        p2 = loop.feed(ev, "fix it")
        assert p2 is not None
        loop.record_outcome(ev, "still_failing")
        # Round 3 attempt → should return None (max_rounds=2)
        p3 = loop.feed(ev, "fix it")
        assert p3 is None
        assert not loop.should_continue

    def test_fixed_outcome_stops_loop(self):
        loop = VerificationLoop(max_rounds=8)
        loop._active = True
        ev = _make_evidence(write=True, test_fail=True)
        p = loop.feed(ev, "fix it")
        assert p is not None
        # Record as fixed with passing tests
        fixed_ev = _make_evidence(write=True, test_pass=True)
        loop.record_outcome(fixed_ev, "fixed")
        # fixed + passing tests → _active=False
        assert not loop.should_continue

    def test_stuck_detection(self):
        loop = VerificationLoop(max_rounds=8, stuck_threshold=2)
        loop._active = True
        ev = _make_evidence(write=True, test_fail=True,
                            fail_error="Error: same error")
        # R1: first feed → records baseline, no match yet
        assert loop.feed(ev, "fix it") is not None
        loop.record_outcome(ev, "still_failing")
        assert not loop.is_stuck
        # R2: matches R1 → _stuck_rounds=1, still below threshold
        assert loop.feed(ev, "fix it") is not None
        loop.record_outcome(ev, "still_failing")
        assert not loop.is_stuck
        # R3: matches again → _stuck_rounds=2 → stuck, feed returns None
        assert loop.feed(ev, "fix it") is None
        assert loop.is_stuck
        assert not loop.should_continue

    def test_convergence_not_stuck(self):
        """Different failure summaries = making progress = not stuck."""
        loop = VerificationLoop(max_rounds=8, stuck_threshold=2)
        loop._active = True
        ev1 = _make_evidence(write=True, test_fail=True,
                             fail_error="Error: assert 1 == 2")
        loop.feed(ev1, "fix it")
        loop.record_outcome(ev1, "still_failing")
        ev2 = _make_evidence(write=True, test_fail=True,
                             fail_error="Error: assert 2 == 3")
        result = loop.feed(ev2, "fix it")
        assert result is not None  # different failure = progress
        assert not loop.is_stuck

    def test_success_cache_update(self):
        loop = VerificationLoop(max_rounds=8)
        loop._active = True
        ev = _make_evidence(write=True, test_fail=True)
        loop.feed(ev, "fix it")
        # Record a successful round with passing test
        tracker = _make_tracker(write_success=True, test_success=True)
        fixed_ev = ExecutionEvidence.capture(tracker)
        loop.record_outcome(fixed_ev, "fixed")
        # Should have cached the test pass and file write
        cache = loop.success_cache
        assert len(cache) >= 1
        # At least one entry should be valid
        assert any(e.valid for e in cache.values())

    def test_stale_evidence_invalidation(self):
        loop = VerificationLoop(max_rounds=8)
        loop._active = True
        # Round 1: success → cache file write
        ev1 = _make_evidence(write=True, test_pass=True)
        loop.record_outcome(ev1, "fixed")
        file_key = "file_write:/tmp/test/file.py"
        cache = loop.success_cache
        # Check if the key exists and is valid
        if file_key in cache:
            assert cache[file_key].valid
            # Round 2: same file written again → should invalidate
            ev2 = _make_evidence(write=True, test_fail=True,
                                 write_path="/tmp/test/file.py")
            loop._invalidate_stale_evidence(ev2)
            assert not cache[file_key].valid

    def test_failure_timeline_accumulation(self):
        loop = VerificationLoop(max_rounds=8)
        loop._active = True
        ev = _make_evidence(write=True, test_fail=True,
                            fail_error="Error: round 1 fail")
        loop.feed(ev, "fix it")
        loop.record_outcome(ev, "still_failing")
        ev2 = _make_evidence(write=True, test_fail=True,
                             fail_error="Error: round 2 fail")
        loop.feed(ev2, "fix it")
        loop.record_outcome(ev2, "still_failing")
        assert len(loop.failure_timeline) == 2
        context = loop.build_context_summary()
        assert "R1" in context
        assert "R2" in context
        assert "round 1" in context
        assert "round 2" in context

    def test_context_summary_includes_cache(self):
        loop = VerificationLoop(max_rounds=8)
        loop._active = True
        ev = _make_evidence(write=True, test_pass=True)
        loop.record_outcome(ev, "fixed")
        context = loop.build_context_summary()
        assert "已验证可复用的操作" in context
        assert "无需重做" in context

    def test_reset_clears_state(self):
        loop = VerificationLoop(max_rounds=8)
        loop._active = True
        ev = _make_evidence(write=True, test_fail=True)
        loop.feed(ev, "fix it")
        loop.record_outcome(ev, "still_failing")
        assert loop.round_count > 0
        loop.reset()
        assert loop.round_count == 0
        assert len(loop.failure_timeline) == 0
        assert len(loop.success_cache) == 0
        assert not loop._active