"""Count non-empty data rows in e2e_scratch/sales.csv and write the result.

Result file format: count=<number>
"""

from __future__ import annotations

import csv
from pathlib import Path

HERE = Path(__file__).resolve().parent
CSV_PATH = HERE / "sales.csv"
OUT_PATH = HERE / "sales_count.txt"


def count_rows(path: Path) -> int:
    """Return the number of non-empty data rows, excluding the header."""
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        rows = [row for row in csv.reader(fh)]

    # Drop the header row.
    data_rows = rows[1:]

    # A row counts only if at least one cell has non-whitespace content.
    return sum(1 for row in data_rows if any(cell.strip() for cell in row))


def main() -> None:
    count = count_rows(CSV_PATH)
    OUT_PATH.write_text(f"count={count}\n", encoding="utf-8")
    print(f"count={count}")


if __name__ == "__main__":
    main()
