"""Pytest hooks making e2e_scratch/plan_verify.txt directly collectible.

The kernel's test transaction records the positional path handed to pytest as
its effect target, so collecting the .txt file itself yields a test_passed
fact whose target is ``e2e_scratch/plan_verify.txt``.
"""

from __future__ import annotations

import pytest

TARGET_NAME = "plan_verify.txt"


def pytest_collect_file(file_path, parent):
    if file_path.name == TARGET_NAME:
        return PlanVerifyFile.from_parent(parent, path=file_path)
    return None


class PlanVerifyFile(pytest.File):
    def collect(self):
        yield PlanVerifyItem.from_parent(self, name="content_is_hello")


class PlanVerifyItem(pytest.Item):
    def runtest(self):
        text = self.path.read_text(encoding="utf-8").strip()
        assert text == "hello", f"unexpected content: {text!r}"
