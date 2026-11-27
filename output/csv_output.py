"""
QDE V1 - output/csv_output.py

CSV Output renderer (Document 07 SS7). One row per setup, for Excel /
manual research. Read-only consumer of list[Result]
(Document 07 SS19).

Dependency level: 2 (depends on models.result, storage.exporter).
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from models.result import Result
from storage.exporter import CSV_COLUMNS, to_csv_rows

_REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"


def render(results: list[Result], scan_summary: dict[str, Any]) -> str:
    """Write the CSV scan export to reports/ and return its path."""
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    run_id = scan_summary.get("run_id", "scan")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filepath = _REPORTS_DIR / f"qde_scan_{timestamp}_{run_id}.csv"

    rows = to_csv_rows(results)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    return str(filepath)
