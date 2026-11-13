"""Clean e2e_scratch/sales.csv and write e2e_scratch/sales_clean.csv.

Rules:
  * drop empty lines / fully-blank rows
  * drop exact duplicate rows (compared after cleaning)
  * customer: strip surrounding whitespace, uppercase
  * date: normalize to YYYY-MM-DD
  * amount: drop "$" and thousands separators, keep 2 decimals
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
SRC = BASE / "sales.csv"
DST = BASE / "sales_clean.csv"
HEADER = ["date", "customer", "amount"]

DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d")


def normalize_date(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty date cell")
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(f"unrecognized date format: {raw!r}")


def normalize_customer(raw: str) -> str:
    return (raw or "").strip().upper()


def normalize_amount(raw: str) -> str:
    text = (raw or "").strip().replace("$", "").replace(",", "").strip()
    if not text:
        raise ValueError("empty amount cell")
    return f"{float(text):.2f}"


def clean_rows(rows: list[list[str]]) -> list[tuple[str, str, str]]:
    """Normalize rows, dropping blank lines and duplicates."""
    cleaned: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        cells = [(c or "").strip() for c in row]
        if not cells or all(not c for c in cells):
            continue
        if cells[:3] == HEADER:
            continue
        if len(cells) < 3:
            raise ValueError(f"row has fewer than 3 columns: {row!r}")
        record = (
            normalize_date(cells[0]),
            normalize_customer(cells[1]),
            normalize_amount(cells[2]),
        )
        if record in seen:
            continue
        seen.add(record)
        cleaned.append(record)
    return cleaned


def main() -> int:
    with SRC.open("r", encoding="utf-8", newline="") as handle:
        raw_rows = list(csv.reader(handle))

    records = clean_rows(raw_rows)

    with DST.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        writer.writerows(records)

    print(f"cleaned rows: {len(records)}")
    print("first 3 rows:")
    for record in records[:3]:
        print(",".join(record))
    print(f"written to: {DST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
