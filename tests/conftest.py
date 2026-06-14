"""
Test configuration — disable security path validation for unit tests.
"""
import pytest


@pytest.fixture(autouse=True)
def _disable_security_for_tests():
    """Disable BaseTool path validation for all tests.

    Tests use temp directories and non-existent paths that are outside
    the project directory, which would trigger path validation errors.

    Since tool execution now flows through ToolRegistry → builtin/*.py tool
    classes → BaseTool._validate_path(), we patch BaseTool instead of the
    old ToolNode._validate_path which no longer exists.
    """
    from omniagent.tools.builtin.base import BaseTool

    original = BaseTool._validate_path

    def permissive_validate(self, file_path, *, for_write=False):
        """Skip security checks in tests."""
        if not file_path:
            from pathlib import Path
            return Path(file_path)
        path = __import__("pathlib").Path(file_path)
        if hasattr(self, 'cwd') and self.cwd and not path.is_absolute():
            path = __import__("pathlib").Path(self.cwd) / path
        return path

    BaseTool._validate_path = permissive_validate
    yield
    BaseTool._validate_path = original
