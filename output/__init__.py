"""
QDE V1 - output package

Reporting layer only (Document 07 SS19): renders list[Result] to
terminal, JSON, and CSV; shows scan progress; and writes live scan
state for the streaming dashboard. Never downloads data, calculates
indicators, scores, or alters confidence/research records.
"""

from output import terminal, json_output, csv_output, progress, live_status

__all__ = ["terminal", "json_output", "csv_output", "progress", "live_status"]
