"""The ledger must be importable without the kernel importing first.

A report/CI job reads a ledger before anything else imports the kernel.  That
order used to fail with "partially initialized module" because
``xenon/kernel/__init__`` eagerly imported ``audit`` → ``controller`` → back
into ``ledger.events``.  The lazy export in ``kernel/__init__`` is the fix; this
test keeps it from regressing.
"""

from __future__ import annotations

import subprocess
import sys


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_ledger_store_imports_before_the_kernel() -> None:
    result = _run(
        "import xenon.ledger.store\n"
        "from xenon.kernel import Runtime, VerificationController\n"
        "print('ok')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_ledger_package_imports_before_the_kernel() -> None:
    result = _run(
        "import xenon.ledger\n"
        "from xenon.kernel.stategies import LoopProfile\n"
        "print('ok')\n".replace("stategies", "strategies")
    )
    assert result.returncode == 0, result.stderr
