"""Count non-empty data rows in sales.csv and record the result.

Writes ``count=<n>`` to sales_count.txt, excluding the header row and
blank lines.  Uses the csv module so quoted fields containing commas
are handled correctly.
"""

from __future__ import annotations

import csv
from pathlib import Path

BASE = Path(__file__).resolve().parent
SALES = BASE / "sales.csv"
OUT = BASE / "sales_count.txt"


def count_rows(path: Path) -> int:
    """Return the number of non-empty data rows (header excluded)."""
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = [row for row in csv.reader(fh) if any(cell.strip() for cell in row)]
    return max(len(rows) - 1, 0)


def main() -> None:
    count = count_rows(SALES)
    OUT.write_text(f"count={count}", encoding="utf-8")
    print(f"count={count}")


if __name__ == "__main__":
    main()
