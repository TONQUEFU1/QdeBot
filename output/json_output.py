"""
QDE V1 - output/json_output.py

JSON Output renderer (Document 07 SS6). One JSON file per scan, for
dashboard integration, future API, external applications, and
historical analysis. Read-only consumer of list[Result]
(Document 07 SS19).

Dependency level: 2 (depends on models.result, storage.exporter).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from models.result import Result
from storage.exporter import to_json_dict

_REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"


def render(results: list[Result], scan_summary: dict[str, Any]) -> str:
    """Write the full JSON scan report to reports/ and return its path."""
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    run_id = scan_summary.get("run_id", "scan")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filepath = _REPORTS_DIR / f"qde_scan_{timestamp}_{run_id}.json"

    payload = to_json_dict(results, scan_summary)
    filepath.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    return str(filepath)
