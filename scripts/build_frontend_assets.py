"""Stage the built TypeScript front-end into the Python package.

The TS build is gitignored and npm is not a Python dependency, so a wheel built
without this step would ship a package whose only UI cannot start.  CI runs
this before ``python -m build``; local release builds must do the same.

Usage:
    python scripts/build_frontend_assets.py            # npm ci && npm run build
    python scripts/build_frontend_assets.py --skip-build
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
DIST = FRONTEND / "dist"
ASSETS = ROOT / "xenon" / "frontend" / "assets"


def _build() -> None:
    npm = shutil.which("npm")
    if npm is None:
        raise SystemExit(
            "npm not found: install Node.js, or pass --skip-build with an "
            "existing frontend/dist"
        )
    subprocess.run([npm, "ci"], cwd=FRONTEND, check=True)
    # ``build`` is tsc (tests/dev); ``bundle`` is the single self-contained
    # file the wheel ships -- it must not depend on frontend/node_modules.
    subprocess.run([npm, "run", "build:release"], cwd=FRONTEND, check=True)


def _stage() -> list[str]:
    built = DIST / "xenon-ui.js"
    if not built.is_file():
        raise SystemExit(
            f"{built} is missing: run `npm run bundle` in frontend/ first"
        )
    ASSETS.mkdir(parents=True, exist_ok=True)
    for stale in ASSETS.glob("*.js"):
        stale.unlink()
    shutil.copy2(built, ASSETS / built.name)
    return [built.name]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="stage an existing frontend/dist without running npm",
    )
    args = parser.parse_args(argv)
    if not args.skip_build:
        _build()
    copied = _stage()
    print(f"staged {len(copied)} file(s) into {ASSETS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
