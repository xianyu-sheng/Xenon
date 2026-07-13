"""v0.4.0 Step 12: Shift+Tab tests."""
from __future__ import annotations

from omniagent.repl.repl import _ShiftTabSignal


class TestShiftTabSignal:
    def test_signal_is_exception(self):
        assert issubclass(_ShiftTabSignal, Exception)

    def test_signal_can_be_raised(self):
        try:
            raise _ShiftTabSignal()
        except _ShiftTabSignal:
            pass
        else:
            assert False, "Should have raised _ShiftTabSignal"
