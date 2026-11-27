"""
QDE V1 - storage/exporter.py

Pure serialization helpers turning list[Result] into JSON-ready dicts
or CSV rows. No file I/O happens here - that lives in output/
(Document 07 SS19: Output Engine only renders, storage.exporter only
shapes data). Keeping this separate from output/ lets both
output.json_output and output.csv_output share the same field
extraction logic without duplicating it.

Dependency level: 1 (depends only on models.result).
"""

from __future__ import annotations

from typing import Any

from models.result import Result

# Fixed column order for CSV export (Document 07 SS7).
CSV_COLUMNS = [
    "symbol",
    "score",
    "confidence",
    "risk_grade",
    "uniqueness",
    "market_regime",
    "expected_duration",
    "passed",
    "setup_id",
    "timestamp",
]


def to_json_dict(results: list[Result], scan_summary: dict[str, Any]) -> dict[str, Any]:
    """Build the full structured object written to the JSON report
    (Document 07 SS6).
    """
    return {
        "scan_summary": scan_summary,
        "results": [r.to_dict() for r in results],
    }


def to_csv_rows(results: list[Result]) -> list[dict[str, Any]]:
    """Build one flat row per Result, using CSV_COLUMNS as the field
    whitelist (Document 07 SS7: each row represents one setup).
    """
    rows = []
    for r in results:
        full = r.to_dict()
        rows.append({col: full.get(col) for col in CSV_COLUMNS})
    return rows


def to_explainability_dict(result: Result) -> dict[str, Any]:
    """Build the positive/negative factor breakdown used by both the
    terminal report and the JSON report (Document 07 SS9).
    """
    positives = [name for name, score in result.setup.feature_scores.items() if score > 0]
    negatives = [name for name, score in result.setup.feature_scores.items() if score < 0]
    return {
        "symbol": result.symbol,
        "positive_factors": positives,
        "negative_factors": negatives,
        "penalties": result.setup.penalties,
        "contradictions": result.contradictions,
    }
