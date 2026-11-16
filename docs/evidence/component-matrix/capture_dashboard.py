"""Capture real Dashboard screenshots for the release notes.

Starts the shipped front-end under ConPTY with ``--dashboard``, drives two
real turns (read + write) through the keyboard like a user, then points a
headless Chrome at the live URL and saves PNGs.  Nothing is mocked: the
panels show the ledger of the turns that were just run.

    PYTHONIOENCODING=utf-8 python docs/evidence/component-matrix/capture_dashboard.py

Output: ``img/dashboard-grid.png`` (>=1400px, four-panel grid),
``img/dashboard-tabs.png`` (narrow, tab layout), ``img/transcript.txt``.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import run_matrix as matrix  # noqa: E402  (same directory, real PTY helpers)

CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
IMG = HERE / "img"


def screenshot(url: str, out: Path, width: int, height: int) -> bool:
    """Capture the live page after its data has loaded (see ``shot.mjs``)."""

    out.unlink(missing_ok=True)
    result = subprocess.run(
        [
            "node",
            str(HERE / "shot.mjs"),
            url,
            str(out),
            str(width),
            str(height),
            "9000",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    ok = out.is_file() and out.stat().st_size > 20_000
    print(
        f"  {'PASS' if ok else 'FAIL'}  screenshot {out.name}"
        f" [{out.stat().st_size if out.exists() else 0} bytes]"
    )
    if not ok:
        print((result.stderr or result.stdout)[-600:])
    return ok


def dashboard_state(url: str) -> dict:
    """The kernel snapshot behind the panels, over HTTP with the token.

    ``/api/state`` is the *front-end* session state; the kernel facts the
    panels render (turn.running, pending approvals) come from the read-only
    ``dashboard.snapshot`` query.
    """

    return dashboard_query(url, "dashboard.snapshot")


def dashboard_query(url: str, kind: str) -> dict:
    parts = urllib.parse.urlsplit(url)
    token = urllib.parse.parse_qs(parts.query).get("t", [""])[0]
    request = urllib.request.Request(
        f"http://{parts.netloc}/api/query?t={token}"
        f"&kind={urllib.parse.quote(kind)}&params=%7B%7D"
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def settle(ui: "matrix.XenonUI", url: str, seconds: float = 300.0) -> bool:
    """Wait until no turn is running and no approval panel is left waiting.

    ``run_turn`` returns on the first evidence footer, which can be the
    previous turn's while the next one is still asking; a screenshot taken
    then shows a pending approval card instead of a finished turn.  The
    panel's own snapshot is the authority, not the terminal tail.
    """

    end = time.time() + seconds
    while time.time() < end:
        ui.pump(0.6)
        try:
            state = dashboard_state(url)
        except Exception:  # noqa: BLE001 — a slow frame is not a failure
            continue
        if state.get("approvals"):
            ui.pty.write("1")
            time.sleep(1.5)
            continue
        if not (state.get("turn") or {}).get("running"):
            ui.pump(1.2)
            return True
    return False


def main() -> int:
    IMG.mkdir(parents=True, exist_ok=True)
    repo = matrix.fresh_repo("dashboard-shots")
    (repo / "README.md").write_text(
        "# shot project\n\n一行说明：这个仓库用来拍 Dashboard 的真实截图。\n",
        encoding="utf-8",
    )
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True)

    ui = matrix.XenonUI(repo, extra_args=["--dashboard"])
    shots: list[bool] = []
    try:
        ui.wait_text("仪表盘已启动", 30)
        match = re.search(
            r"http://127\.0\.0\.1:(\d+)/\?t=([0-9a-f]{16,})", matrix.strip_ansi(ui.raw)
        )
        if not match:
            print("FAIL: dashboard URL not printed")
            return 1
        url = match.group(0)
        print(f"  url={url}")

        # Turn 1: read-only, so the evidence panel shows a verified chain
        # without any approval prompt.
        ok, _ = ui.run_turn(
            "读 README.md 和 calc.py，告诉我 add 函数有什么 bug，先不要改任何文件。",
            timeout=300,
        )
        shots.append(ok)
        # Turn 2: fix + run the test suite (approval panel), so the diff panel
        # has a receipt-backed change and the cost panel real token counts.
        ok, _ = ui.run_turn(
            "修复 calc.py 里的 add，让它返回 a+b，然后运行 python -m pytest tests -q 确认通过。",
            timeout=300,
        )
        shots.append(ok)

        shots.append(settle(ui, url))
        try:
            diff = dashboard_query(url, "diff.get")
            print(
                "  diff.get files="
                f"{[item.get('path') for item in diff.get('files') or []]}"
                f" error={diff.get('error')}"
            )
        except Exception as exc:  # noqa: BLE001 — a diagnostic, not a check
            print(f"  diff.get failed: {exc}")
        shots.append(screenshot(url, IMG / "dashboard-grid.png", 1600, 1100))
        shots.append(screenshot(url, IMG / "dashboard-tabs.png", 1024, 1100))
        (IMG / "transcript.txt").write_text(
            matrix.condense(ui.raw, keep=1600), encoding="utf-8"
        )
        (IMG / "capture.json").write_text(
            json.dumps(
                {
                    "url": url.split("?")[0],
                    "repo": str(repo),
                    "calc_after": (repo / "calc.py").read_text(encoding="utf-8"),
                    "tests": (repo / "tests" / "test_calc.py").read_text(encoding="utf-8"),
                    "models": matrix.MODEL,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    finally:
        ui.close()

    passed = sum(shots)
    print(f"\n{passed}/{len(shots)} capture steps passed")
    return 0 if passed == len(shots) else 1


if __name__ == "__main__":
    raise SystemExit(main())
