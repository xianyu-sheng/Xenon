#!/usr/bin/env python
"""Thin CLI wrapper around ``xenon.report``.

The implementation lives in the package so ``xenon report`` and this script
cannot drift.  Kept for CI jobs that call the script path directly.
"""

from __future__ import annotations

from xenon.report import main

if __name__ == "__main__":
    raise SystemExit(main())
